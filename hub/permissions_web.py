"""Flask surface for permission groups: the enforcement glue every other route uses,
plus the admin API behind the Permission Groups page.

Split in two on purpose:

  * `Access` -- the enforcement helpers (`require`, `require_machine`, `can`,
    `in_scope`, `filter_machines`). One instance is built in app.py and handed to
    every blueprint, so there is a single source of truth for "may this caller do
    this to this machine", the same way `login_required` is passed around today.
  * `create_permissions_blueprint` -- CRUD over the groups themselves, gated by
    MANAGE_PERMISSION_GROUPS.

Effective permissions are resolved once per request and cached on `flask.g`. Without
that, a machine list would re-resolve the caller's groups for every row; with it, the
cost is one lookup against permissions.py's process-wide copy-on-write cache.

The CSRF note in fleet_web.py's module docstring applies here verbatim, and with more
force than anywhere else: these endpoints grant capabilities. Bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json -- not
CORS-safelisted, so a cross-origin fetch preflights and fails and an HTML form cannot
produce it. Do not add force=True and do not accept a form-encoded fallback.
"""
import functools

from flask import Blueprint, g, jsonify, render_template, request, session

import permissions
import users

# The member picker shows at most this many matches -- enough to choose from, few enough
# that a broad query can't stream the whole directory into the dropdown.
DIRECTORY_PICKER_LIMIT = 20


def _current_email():
    """The signed-in operator. ALWAYS from the session, never from a request body --
    otherwise one operator could act as another and the audit trail becomes fiction."""
    return permissions.normalize_email((session.get("user") or {}).get("email"))


class Access:
    """Per-request authorization for one hub. Built once in app.py and shared.

    `superusers` is ALLOWED_EMAILS -- the break-glass list that bypasses groups
    entirely. It is captured by reference to the set app.py builds at import time, so
    it never drifts from the login gate.
    """

    def __init__(self, db_path, superusers):
        self.db_path = db_path
        self.superusers = superusers

    # ------------------------------------------------------------------ resolution
    def current(self):
        """This request's effective permissions, resolved once and cached on `g`."""
        cached = getattr(g, "_fleethub_permissions", None)
        if cached is None:
            cached = permissions.effective_permissions(
                self.db_path, _current_email(), self.superusers)
            g._fleethub_permissions = cached
        return cached

    def email(self):
        return _current_email()

    def can(self, capability):
        return permissions.has_capability(self.current(), capability)

    def in_scope(self, machine):
        return permissions.machine_in_scope(self.current(), machine)

    def is_superuser(self):
        return bool(self.current().get("superuser"))

    def login_allowed(self, email):
        """May this identity sign in at all? Break-glass, or a member of any group.

        A valid Google account that belongs to no group is refused rather than being
        let in to an empty dashboard: the hub is not a public service, and "signed in
        but sees nothing" is an invitation to keep poking. Roadmap #4 (Entra) softens
        this for directory identities -- there, login succeeding with zero groups is
        the documented behaviour -- which is why this lives behind one method.
        """
        email = permissions.normalize_email(email)
        if permissions.is_superuser(email, self.superusers):
            return True
        return bool(permissions.groups_for_email(self.db_path, email))

    # ------------------------------------------------------------------ filtering
    def machine_filter(self):
        """A predicate over machine names, or None when the caller is unrestricted."""
        return permissions.visible_machine_filter(self.current())

    def filter_machines(self, names):
        """Narrow an iterable of machine names to the visible ones, preserving order."""
        keep = self.machine_filter()
        if keep is None:
            return list(names)
        return [n for n in names if keep(n)]

    def filter_rows(self, rows, key="machine"):
        """Narrow an iterable of dict-ish rows to those whose machine is in scope."""
        keep = self.machine_filter()
        if keep is None:
            return list(rows)
        return [r for r in rows if keep(r[key])]

    # ------------------------------------------------------------------ decorators
    def _deny(self, message):
        """403 as JSON for the API, as a page for a browser navigation -- mirroring
        how login_required splits on request.path."""
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), 403
        return render_template("denied.html", message=message), 403

    def require(self, capability):
        """Gate a route on holding `capability` at all (no machine involved)."""
        def decorator(view):
            @functools.wraps(view)
            def wrapped(*args, **kwargs):
                if not self.can(capability):
                    return self._deny(
                        f"You do not have the '{capability}' permission.")
                return view(*args, **kwargs)
            return wrapped
        return decorator

    def require_machine(self, capability, arg="machine"):
        """Gate a route on `capability` AND the target machine being in scope.

        The machine is read from the view's `arg` keyword (the URL parameter). Routes
        that take the machine in a body or query string check `in_scope()` inline
        instead -- there is no way to do that generically without the decorator
        guessing at request shape, and guessing is how a gate ends up silently
        passing.

        Out-of-scope machines are refused with the same message as a missing
        capability, deliberately: distinguishing "not allowed" from "no such machine"
        would let an HR tech enumerate Hospital hostnames.
        """
        def decorator(view):
            @functools.wraps(view)
            def wrapped(*args, **kwargs):
                machine = kwargs.get(arg)
                if not self.can(capability) or not self.in_scope(machine):
                    return self._deny(f"You do not have access to {machine!r}.")
                return view(*args, **kwargs)
            return wrapped
        return decorator


def create_access(db_path, superusers):
    return Access(db_path, superusers)


# ================================
# ADMIN API
# ================================
def create_permissions_blueprint(db_path, login_required, access):
    """CRUD for permission groups. `login_required` is app.py's session gate; `access`
    is the Access instance every route here is additionally gated by."""
    bp = Blueprint("permissions", __name__)
    manage = access.require(permissions.MANAGE_PERMISSION_GROUPS)

    @bp.route("/api/permissions/me", methods=["GET"])
    @login_required
    def permissions_me():
        """The signed-in operator's own effective permissions.

        Every page fetches this to decide what to render -- hiding a button the
        caller cannot use. That is presentation only; the server-side gate on each
        endpoint is the actual control, and this endpoint deliberately reveals
        nothing the caller doesn't already hold.
        """
        current = access.current()
        return jsonify({
            "email": current["email"],
            "superuser": current["superuser"],
            "capabilities": sorted(current["capabilities"],
                                   key=permissions.CAPABILITIES.index),
            # null means "every machine" -- see effective_permissions().
            "machines": (None if current["machines"] is None
                         else sorted(current["machines"])),
            "groups": [{"id": grp["id"], "name": grp["name"]}
                       for grp in current["groups"]],
        }), 200

    @bp.route("/api/permissions/directory", methods=["GET"])
    @login_required
    @manage
    def permissions_directory():
        """A narrow search over the Registered Users directory, for this page's member
        picker (roadmap #6). Deliberately gated on MANAGE_PERMISSION_GROUPS, not
        MANAGE_USERS: choosing who to add to a group is part of managing groups, and it
        returns only the three fields a picker shows (email, name, username) -- never the
        phone/title/department/notes that the Users API guards. An admin who can grant
        capabilities but not edit profiles can still pick a member by name.
        """
        q = request.args.get("q")
        matches = users.list_users(db_path, q=q)[:DIRECTORY_PICKER_LIMIT]
        return jsonify({
            "users": [
                {"email": u["email"], "full_name": u["full_name"],
                 "username": u["username"]}
                for u in matches
            ]
        }), 200

    @bp.route("/api/permissions/capabilities", methods=["GET"])
    @login_required
    @manage
    def permissions_capabilities():
        """The capability vocabulary, so the admin form renders itself from the
        server's list rather than a hardcoded copy in JS that can drift."""
        return jsonify({
            "capabilities": [
                {"name": name,
                 "label": permissions.CAPABILITY_LABELS[name][0],
                 "description": permissions.CAPABILITY_LABELS[name][1]}
                for name in permissions.CAPABILITIES
            ],
            "scope_modes": list(permissions.SCOPE_MODES),
        }), 200

    @bp.route("/api/permissions/groups", methods=["GET"])
    @login_required
    @manage
    def list_permission_groups():
        return jsonify(permissions.list_groups(db_path)), 200

    @bp.route("/api/permissions/groups", methods=["POST"])
    @login_required
    @manage
    def create_permission_group():
        data = request.get_json(silent=True) or {}
        try:
            group_id = permissions.create_group(
                db_path,
                name=data.get("name"),
                description=data.get("description"),
                capabilities=data.get("capabilities"),
                machines=data.get("machines") or [],
                members=data.get("members") or [],
                scope_mode=data.get("scope_mode") or permissions.SCOPE_LIST,
                actor=access.email(),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(permissions.get_group(db_path, group_id)), 201

    @bp.route("/api/permissions/groups/<group_id>", methods=["GET"])
    @login_required
    @manage
    def get_permission_group(group_id):
        group = permissions.get_group(db_path, group_id)
        if group is None:
            return jsonify({"error": "unknown permission group"}), 404
        return jsonify(group), 200

    @bp.route("/api/permissions/groups/<group_id>", methods=["PUT"])
    @login_required
    @manage
    def update_permission_group(group_id):
        data = request.get_json(silent=True) or {}
        try:
            group = permissions.update_group(
                db_path, group_id,
                name=data.get("name"),
                description=data.get("description"),
                capabilities=data.get("capabilities"),
                machines=data.get("machines"),
                members=data.get("members"),
                scope_mode=data.get("scope_mode"),
                actor=access.email(),
            )
        except KeyError:
            return jsonify({"error": "unknown permission group"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(group), 200

    @bp.route("/api/permissions/groups/<group_id>", methods=["DELETE"])
    @login_required
    @manage
    def delete_permission_group(group_id):
        try:
            permissions.delete_group(db_path, group_id, actor=access.email())
        except KeyError:
            return jsonify({"error": "unknown permission group"}), 404
        return jsonify({"status": "deleted"}), 200

    return bp
