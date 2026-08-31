"""Flask HTTP surface for patch management -- a thin layer over patches.py, in the same
shape as packages_web.py and bios_web.py.

**The read/write split is the whole authorization story here, and it is deliberately
lopsided.** Reading patch inventory and compliance is gated on `view`, like a machine's
disks or its BIOS version: knowing a PC is missing a security update is not a privilege,
and putting it behind the management capability would mean the people most likely to
notice a problem are the ones who cannot see it. Deciding what happens about it --
approving an update, scheduling a window, starting a run -- is gated on `manage_patches`.

**Machine scope is applied on both sides of that split, in opposite directions.** Reads
are narrowed: an operator sees the rows for machines they could have acted on, so a
fleet-wide compliance figure does not leak the Hospital hostnames to HR. Writes are
all-or-nothing: a run naming ten machines, one of which is out of scope, is refused
entirely rather than quietly started on nine. Same rule and same reason as
packages_web._scoped_targets -- a patch night that silently skipped a machine is how
somebody believes forty PCs were updated and thirty were not.

**Approvals are NOT machine-scoped, and that is not an oversight.** An approval is a
statement about an update, keyed on its identity alone (see patches.py's docstring): it
does not name a machine, so there is no scope to check. What an approval can actually
reach is bounded where it is spent -- at run creation, per target, against the operator's
own scope.

Every state-changing endpoint reads its body with `request.get_json(silent=True)`, which
is load-bearing CSRF protection rather than a convenience -- see fleet_web.py's module
docstring, which applies here verbatim. There are no uploads in this feature, so there is
no multipart exception to make.

The agent never talks to this module. Available updates arrive on the ordinary heartbeat
(fleet_web.py) and results come back through the command queue, so there is no
`/api/agent/*` surface here to get wrong.
"""
from flask import Blueprint, jsonify, render_template, request

import fleet
import i18n
import patches
import permissions
import permissions_web
import refusals
import settings


def _lang():
    """The language of the request in flight; English outside one. See i18n.current."""
    return i18n.current()


def _vocabulary():
    """The self-describing half of the API: kinds plus their catalog text.

    Same discipline as /api/permissions/capabilities and packages' detection kinds -- the
    console renders its filters from this, so a classification added to patches.py shows
    up in the UI with its own words and no JS change, and one added without catalog
    entries fails tests/test_i18n.py rather than captioning a filter with its own key.
    """
    return {
        "classifications": [
            {"name": kind,
             "label": i18n.translate(
                 f"{patches.CLASSIFICATION_TEXT_KEY}.{kind}.label", _lang()),
             "description": i18n.translate(
                 f"{patches.CLASSIFICATION_TEXT_KEY}.{kind}.description", _lang())}
            for kind in patches.CLASSIFICATIONS
        ],
        "sources": [
            {"name": kind,
             "label": i18n.translate(f"{patches.SOURCE_TEXT_KEY}.{kind}.label", _lang()),
             "description": i18n.translate(
                 f"{patches.SOURCE_TEXT_KEY}.{kind}.description", _lang())}
            for kind in patches.SOURCE_KINDS
        ],
        "reboot_policies": list(patches.REBOOT_POLICIES),
        "scope_kinds": list(patches.SCOPE_KINDS),
        "selections": list(patches.SELECTIONS),
        "auto_approvable": list(patches.AUTO_APPROVABLE),
        "run_statuses": list(patches.RUN_STATUSES),
        "target_statuses": list(patches.TARGET_STATUSES),
    }


def create_patches_blueprint(db_path, login_required, access):
    """Build the patches Blueprint.

    `db_path` is passed in rather than imported from app.py, to avoid a circular import
    and because the test suite re-points it.
    """
    bp = Blueprint("patches", __name__)
    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_PATCHES)

    def _current_email():
        """The signed-in operator. ALWAYS from the authenticated identity -- never a
        request body, or the audit trail becomes fiction."""
        return permissions_web.current_actor()

    def _read_scope():
        """The machine list a scoped read should be narrowed to, or None if unrestricted.

        None and [] mean different things all the way down into patches.py: None is "no
        scope", [] is "a scope that matched nothing" and must return nothing. Collapsing
        the two is how a fleet-wide list gets shown to somebody entitled to none of it.
        """
        if access.machine_filter() is None:
            return None
        return access.filter_machines(patches.known_machines(db_path))

    def _scoped_targets(machines):
        """Validate every requested target against the caller's scope.

        All-or-nothing -- see the module docstring. An empty selection is a 400 (a
        malformed request); an out-of-scope machine is a 403 (well-formed, refused).
        """
        names = [str(m or "").strip() for m in (machines or [])]
        names = [n for n in names if n]
        if not names:
            return None, "Select at least one machine.", 400
        denied = [n for n in names if not access.in_scope(n)]
        if denied:
            return None, (f"You do not have access to {denied[0]!r}"
                          + (f" and {len(denied) - 1} other machine(s)."
                             if len(denied) > 1 else ".")), 403
        return names, None, 200

    def _scoped_run(run_id):
        """Check a whole run is within the caller's scope before acting on it.

        Returns (run, error, status). ALL-OR-NOTHING, exactly like _scoped_targets and for
        the sharper version of the same reason: cancel and retry act on every target at once,
        so an operator who may reach nine of ten machines must not be able to cancel a run
        that also touches the tenth. Without this, a `manage_patches` holder scoped to one
        machine could enumerate run ids and then cancel a patch night -- or force a
        reboot-retry -- on machines they cannot see anywhere else in the product.

        Refused rather than narrowed, unlike the reads: there is no partial cancel here, and
        silently applying a whole-run action to only part of it would be a worse answer than
        saying no.
        """
        run = patches.get_run(db_path, run_id, with_targets=False)
        if run is None:
            return None, "That patch run no longer exists.", 404
        denied = [m for m in patches.run_machines(db_path, run_id)
                  if not access.in_scope(m)]
        if denied:
            return None, (f"This run also targets {denied[0]!r}"
                          + (f" and {len(denied) - 1} other machine(s)"
                             if len(denied) > 1 else "")
                          + ", which you do not have access to."), 403
        return run, None, 200

    def _scoped_window_machines(scope_kind, machines):
        """Check a maintenance window only reaches machines the caller can act on.

        A window decides WHEN patches install and when machines restart, so an unscoped one
        is a fleet-wide write. `all` is therefore refused for a scoped operator outright --
        there is no honest way to narrow "every machine" to a subset and still call it the
        same window -- and a named list is checked host by host.

        Returns (error, status) or (None, 200).
        """
        if access.machine_filter() is None:
            return None, 200          # unrestricted: every window is within reach
        if scope_kind == patches.SCOPE_ALL:
            return ("A window covering every machine can only be created by an operator "
                    "whose access is not limited to specific machines. Name the machines "
                    "instead."), 403
        denied = [str(m or "").strip() for m in (machines or ())
                  if str(m or "").strip() and not access.in_scope(str(m).strip())]
        if denied:
            return (f"You do not have access to {denied[0]!r}"
                    + (f" and {len(denied) - 1} other machine(s)."
                       if len(denied) > 1 else ".")), 403
        return None, 200

    def _body():
        return request.get_json(silent=True) or {}

    # ---------------- Pages ----------------
    @bp.route("/patches")
    @login_required
    @can_view
    def patches_page():
        return render_template("patches.html")

    # ---------------- Console: inventory and compliance (view) ----------------
    @bp.route("/api/patches", methods=["GET"])
    @login_required
    @can_view
    def patches_overview():
        scope = _read_scope()
        return jsonify({
            "updates": patches.list_fleet_patches(db_path, machines=scope),
            "summary": patches.compliance_summary(db_path, machines=scope),
            "vocabulary": _vocabulary(),
            "can_manage": access.can(permissions.MANAGE_PATCHES),
            "auto_approve": settings.get_list(
                db_path, "patches.auto_approve_classifications"),
            "defaults": {
                "max_attempts": settings.get_int(db_path, "patches.default_max_attempts"),
                "retry_backoff_seconds": settings.get_int(
                    db_path, "patches.default_retry_backoff_seconds"),
                "confirm_timeout_seconds": settings.get_int(
                    db_path, "patches.confirm_timeout_seconds"),
                "reboot_policy": patches.REBOOT_IF_REQUIRED,
            },
        }), 200

    @bp.route("/api/patches/machine/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_patches(machine):
        available = patches.list_machine_patches(db_path, machine)
        decisions = patches.approvals_map(db_path, [p["uid"] for p in available])
        for row in available:
            row["decision"] = decisions.get(row["uid"], "")
        return jsonify({
            "machine": machine,
            "updates": available,
            "runs": patches.list_runs(db_path, limit=20, machine=machine),
            "vocabulary": _vocabulary(),
            "can_manage": access.can(permissions.MANAGE_PATCHES),
        }), 200

    # ---------------- Console: approvals (manage) ----------------
    @bp.route("/api/patches/approvals", methods=["GET"])
    @login_required
    @can_view
    def list_approvals():
        return jsonify({"approvals": patches.list_approvals(db_path)}), 200

    @bp.route("/api/patches/approvals", methods=["POST"])
    @login_required
    @can_manage
    def set_approval():
        """Approve, decline, or return an update to undecided.

        Not machine-scoped -- see the module docstring. What it can reach is bounded at
        run creation, per target.
        """
        data = _body()
        uid = data.get("uid")
        decision = str(data.get("decision") or "").strip()
        try:
            if decision == "":
                key = patches.clear_approval(db_path, uid)
                action = "clear_patch_approval"
            else:
                key = patches.set_approval(
                    db_path, uid, decision, actor=_current_email(),
                    title=data.get("title") or "", note=data.get("note") or "")
                action = "set_patch_approval"
        except patches.PatchError as e:
            return refusals.refuse(e)
        fleet.audit(db_path, actor=_current_email(), action=action,
                    level=fleet.LEVEL_NOTICE,
                    detail={"uid": key, "decision": decision or "undecided"})
        return jsonify({"ok": True, "uid": key, "decision": decision}), 200

    # ---------------- Console: maintenance windows (manage) ----------------
    @bp.route("/api/patches/windows", methods=["GET"])
    @login_required
    @can_view
    def list_windows():
        return jsonify({"windows": patches.list_windows(db_path)}), 200

    @bp.route("/api/patches/windows", methods=["POST"])
    @login_required
    @can_manage
    def create_window():
        data = _body()
        error, status = _scoped_window_machines(
            data.get("scope_kind") or patches.SCOPE_ALL, data.get("machines"))
        if error:
            return jsonify({"error": error}), status
        try:
            window_id = patches.create_window(
                db_path, actor=_current_email(),
                name=data.get("name"), days_mask=data.get("days_mask"),
                start_minute=data.get("start_minute"),
                duration_minutes=data.get("duration_minutes"),
                scope_kind=data.get("scope_kind") or patches.SCOPE_ALL,
                machines=data.get("machines") or (),
                reboot_policy=data.get("reboot_policy") or patches.REBOOT_IF_REQUIRED)
        except patches.PatchError as e:
            return refusals.refuse(e)
        fleet.audit(db_path, actor=_current_email(), action="create_maintenance_window",
                    level=fleet.LEVEL_NOTICE,
                    detail={"window_id": window_id, "name": data.get("name")})
        return jsonify({"ok": True, "window": patches.get_window(db_path, window_id)}), 201

    @bp.route("/api/patches/windows/<window_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update_window(window_id):
        data = _body()
        existing = patches.get_window(db_path, window_id)
        if existing is None:
            return jsonify({"error": "That maintenance window no longer exists."}), 404
        # Both ends are checked: the window as it will BE, and the window as it IS. Checking
        # only the new values would let a scoped operator narrow somebody else's fleet-wide
        # window down to their own machines -- an edit they could not have made from scratch.
        for kind, hosts in ((data.get("scope_kind") or existing["scope_kind"],
                             data.get("machines", existing["machines"])),
                            (existing["scope_kind"], existing["machines"])):
            error, status = _scoped_window_machines(kind, hosts)
            if error:
                return jsonify({"error": error}), status
        fields = {k: data[k] for k in
                  ("name", "days_mask", "start_minute", "duration_minutes",
                   "scope_kind", "machines", "reboot_policy") if k in data}
        try:
            patches.update_window(db_path, window_id, actor=_current_email(),
                                  enabled=data.get("enabled"), **fields)
        except patches.PatchError as e:
            return refusals.refuse(e)
        fleet.audit(db_path, actor=_current_email(), action="update_maintenance_window",
                    level=fleet.LEVEL_NOTICE, detail={"window_id": window_id})
        return jsonify({"ok": True, "window": patches.get_window(db_path, window_id)}), 200

    @bp.route("/api/patches/windows/<window_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_window(window_id):
        existing = patches.get_window(db_path, window_id)
        if existing is None:
            return jsonify({"error": "That maintenance window no longer exists."}), 404
        # Deleting a window changes when patches install for every machine it covers, so it
        # needs the same reach as creating one. Checked BEFORE the delete, not after.
        error, status = _scoped_window_machines(existing["scope_kind"], existing["machines"])
        if error:
            return jsonify({"error": error}), status
        if not patches.delete_window(db_path, window_id):
            return jsonify({"error": "That maintenance window no longer exists."}), 404
        fleet.audit(db_path, actor=_current_email(), action="delete_maintenance_window",
                    level=fleet.LEVEL_NOTICE, detail={"window_id": window_id})
        return jsonify({"ok": True}), 200

    # ---------------- Console: runs (manage) ----------------
    @bp.route("/api/patches/runs", methods=["GET"])
    @login_required
    @can_view
    def list_runs():
        # Narrowed to runs touching a machine the caller could have acted on, matching what
        # the module docstring promises and what get_run already does to its target rows.
        # Unnarrowed this is the enumeration surface a scoped operator would use to find run
        # ids for machines they cannot see.
        runs = patches.list_runs(db_path, limit=int(request.args.get("limit", 100)),
                                 machines=_read_scope())
        return jsonify({"runs": runs}), 200

    @bp.route("/api/patches/runs/<run_id>", methods=["GET"])
    @login_required
    @can_view
    def get_run(run_id):
        run = patches.get_run(db_path, run_id)
        if run is None:
            return jsonify({"error": "That patch run no longer exists."}), 404
        # Narrowed the read way, not the write way: an operator sees the target rows they
        # could have created, and a run touching machines outside their scope simply shows
        # fewer of them rather than 403ing a page they are entitled to most of.
        run["targets"] = access.filter_rows(run["targets"])
        run["items"] = access.filter_rows(patches.list_run_items(db_path, run_id))
        return jsonify({"run": run}), 200

    @bp.route("/api/patches/runs", methods=["POST"])
    @login_required
    @can_manage
    def create_run():
        """Start a patch run.

        `emergency` is the one flag here that changes what a maintenance window means: an
        emergency run dispatches as soon as a machine is reachable, ignoring every window.
        It rides in the audit details rather than splitting the row, because a run is
        security-level either way -- see fleet.ACTION_LEVELS -- and an auditor asking "who
        rebooted the fleet on Tuesday" wants both kinds in one place.
        """
        data = _body()
        targets, error, status = _scoped_targets(data.get("machines"))
        if error:
            return jsonify({"error": error}), status
        emergency = bool(data.get("emergency"))
        try:
            run_id = patches.create_run(
                db_path, machines=targets, created_by=_current_email(),
                selection=data.get("selection") or patches.SELECTION_APPROVED,
                uids=data.get("uids") or (), note=data.get("note") or "",
                window_id=data.get("window_id") or "", emergency=emergency,
                reboot_policy=data.get("reboot_policy") or patches.REBOOT_IF_REQUIRED,
                window_start=data.get("window_start"),
                window_end=data.get("window_end"),
                max_attempts=data.get("max_attempts") or settings.get_int(
                    db_path, "patches.default_max_attempts"),
                retry_backoff_seconds=data.get("retry_backoff_seconds")
                or settings.get_int(db_path, "patches.default_retry_backoff_seconds"),
                confirm_timeout_seconds=data.get("confirm_timeout_seconds")
                or settings.get_int(db_path, "patches.confirm_timeout_seconds"))
        except patches.PatchError as e:
            return refusals.refuse(e)
        fleet.audit(
            db_path, actor=_current_email(), action="create_patch_run",
            level=fleet.LEVEL_SECURITY,
            detail={"run_id": run_id, "machines": len(targets),
                     "emergency": emergency,
                     "selection": data.get("selection") or patches.SELECTION_APPROVED})
        return jsonify({"ok": True, "run": patches.get_run(db_path, run_id)}), 201

    @bp.route("/api/patches/runs/<run_id>/cancel", methods=["POST"])
    @login_required
    @can_manage
    def cancel_run(run_id):
        _, error, status = _scoped_run(run_id)
        if error:
            return jsonify({"error": error}), status
        recalled = patches.cancel_run(db_path, run_id, actor=_current_email())
        fleet.audit(db_path, actor=_current_email(), action="cancel_patch_run",
                    level=fleet.LEVEL_NOTICE,
                    detail={"run_id": run_id, "recalled": recalled})
        # `recalled` is deliberately reported: machines already restarting are NOT recalled
        # (the patches are on them), so "cancelled" and "cancelled 8 of 10" are different
        # facts and the console should be able to say which happened.
        return jsonify({"ok": True, "recalled": recalled,
                        "run": patches.get_run(db_path, run_id)}), 200

    @bp.route("/api/patches/runs/<run_id>/retry", methods=["POST"])
    @login_required
    @can_manage
    def retry_run(run_id):
        _, error, status = _scoped_run(run_id)
        if error:
            return jsonify({"error": error}), status
        rearmed = patches.retry_failures(db_path, run_id, actor=_current_email())
        fleet.audit(db_path, actor=_current_email(), action="retry_patch_run",
                    level=fleet.LEVEL_SECURITY,
                    detail={"run_id": run_id, "rearmed": rearmed})
        return jsonify({"ok": True, "rearmed": rearmed,
                        "run": patches.get_run(db_path, run_id)}), 200

    return bp
