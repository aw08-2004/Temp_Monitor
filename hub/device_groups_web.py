"""Device groups -- the HTTP surface over device_groups.py (hub 1.114.0).

Two gates, and the second is the one that matters:

  * `view` reads a group and USES one (its members, a preview). `manage_device_groups` writes.
  * **A writer's scope must cover every PC the group resolves to -- on create, on edit and on
    delete.** Checked on the RESOLVED membership, never on the selector text, for the reason
    rules_web._scope_error gives: "every PC except two" and an OU selector reach machines the
    text never names. Without it, an operator scoped to one site could define "every PC" and
    hand a fleet-wide target to whoever aims a rule or a deployment at it.

Using a group is re-checked where the use happens: rules.scoped_targets narrows a rule to its
author's scope at every evaluation, /api/deployments refuses any out-of-scope machine, and
every member list served here is narrowed to the caller first.

**Definitions are redacted to the caller's scope too.** A `machines` selector is a list of
hostnames, and a plain viewer scoped to one site must not be able to enumerate another site by
reading a group somebody else wrote. The redacted copy is flagged, and such a group cannot be
edited by that caller anyway -- its resolution reaches outside their scope.

Request bodies are read with get_json(silent=True), like rules_web: the hub only reads JSON
bodies, which is what makes a cross-site form POST unable to write here.
"""
import traceback

from flask import Blueprint, jsonify, render_template, request

import device_groups
import fleet
import permissions
import rules

GENERIC_ERROR = "Could not work out which machines this group reaches. The hub log has the detail."
PREVIEW_LIMIT = 500


def _log_generic(context):
    """Log the exception being handled; return a caller-safe constant (see rules_web)."""
    print(f"[device_groups] Failed while {context}:\n{traceback.format_exc()}")
    return GENERIC_ERROR


def create_device_groups_blueprint(db_path, login_required, access):
    bp = Blueprint("device_groups", __name__)

    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_DEVICE_GROUPS)

    def _extra():
        return rules.all_extra_variables(db_path)

    def _actor():
        return access.email() or "unknown"

    def _resolve(target):
        """(error, machines) -- machines unscoped; the error is already caller-safe."""
        try:
            return None, rules.resolve_targets(db_path, target)
        except Exception:                                 # noqa: BLE001
            return _log_generic("resolving a device group"), None

    def _scope_error(target):
        error, machines = _resolve(target)
        if error:
            return error
        outside = [m for m in machines if not access.in_scope(m)]
        if outside:
            return ("this group would reach machines outside your access: "
                    + ", ".join(sorted(outside)[:5])
                    + (f" (and {len(outside) - 5} more)" if len(outside) > 5 else ""))
        return None

    def _redacted(target):
        redacted = False
        clean = {"include": [], "exclude": []}
        for side in ("include", "exclude"):
            for selector in (target or {}).get(side) or []:
                if selector.get("kind") == rules.TARGET_MACHINES:
                    names = selector.get("machines") or []
                    visible = [m for m in names if access.in_scope(m)]
                    if len(visible) != len(names):
                        redacted = True
                    selector = {**selector, "machines": visible}
                clean[side].append(selector)
        return clean, redacted

    def _public(group):
        error, machines = _resolve(group["target"])
        target, redacted = _redacted(group["target"])
        return {
            "id": group["id"],
            "name": group["name"],
            "description": group["description"],
            "target": target,
            "redacted": redacted,
            "count": None if error else sum(1 for m in machines if access.in_scope(m)),
            "used_by": rules.rules_using_group(db_path, group["id"]),
            "updated_at": group["updated_at"],
            "updated_by": group["updated_by"],
        }

    def _audit(action, group, detail=None):
        # LEVEL_SECURITY, like a rule edit: what this row records is a change to what every
        # rule and deployment aimed at the group reaches.
        fleet.audit(db_path, actor=_actor(), action=action, level=fleet.LEVEL_SECURITY,
                    target=group["name"],
                    detail={"id": group["id"], "target": group["target"], **(detail or {})})

    # ---------------- Read and use (view) ----------------
    @bp.route("/api/device-groups", methods=["GET"])
    @login_required
    @can_view
    def list_all():
        return jsonify({
            "groups": [_public(g) for g in device_groups.list_groups(db_path)],
            "can_manage": access.can(permissions.MANAGE_DEVICE_GROUPS),
        }), 200

    @bp.route("/api/device-groups/<int:group_id>/members", methods=["GET"])
    @login_required
    @can_view
    def members(group_id):
        """A group's CURRENT members, narrowed to the caller. Feeds the deploy dialog's "Add a
        group" and the Devices filter; neither may learn a name the caller could not see."""
        group = device_groups.get_group(db_path, group_id)
        if group is None:
            return jsonify({"error": "no such group"}), 404
        error, machines = _resolve(group["target"])
        if error:
            return jsonify({"error": error}), 500
        return jsonify({"id": group["id"], "name": group["name"],
                        "machines": [m for m in machines if access.in_scope(m)]}), 200

    @bp.route("/api/device-groups/preview", methods=["POST"])
    @login_required
    @can_view
    def preview():
        """Which machines a draft definition reaches, for the editor's live count."""
        body = request.get_json(silent=True) or {}
        error, target = rules.validate_target(body.get("target"), _extra())
        if not error and device_groups._contains_group_selector(target):
            error = "a device group cannot include or exclude another device group"
        if error:
            return jsonify({"error": error}), 400
        error, machines = _resolve(target)
        if error:
            return jsonify({"error": error}), 500
        visible = [m for m in machines if access.in_scope(m)]
        return jsonify({"count": len(visible), "machines": visible[:PREVIEW_LIMIT],
                        "truncated": len(visible) > PREVIEW_LIMIT}), 200

    # ---------------- Write (manage_device_groups) ----------------
    @bp.route("/api/device-groups", methods=["POST"])
    @login_required
    @can_manage
    def create():
        body = request.get_json(silent=True) or {}
        error, clean = device_groups.validate(body, _extra())
        if not error:
            error = _scope_error(clean["target"])
        if error:
            return jsonify({"error": error}), 400
        error, group = device_groups.save_group(db_path, clean, actor=_actor(), extra=_extra())
        if error:
            return jsonify({"error": error}), 400
        _audit("device_group_created", group)
        return jsonify(_public(group)), 201

    @bp.route("/api/device-groups/<int:group_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update(group_id):
        existing = device_groups.get_group(db_path, group_id)
        if existing is None:
            return jsonify({"error": "no such group"}), 404
        # Both sides of the edit: a scoped operator may neither widen a group past their scope
        # nor take over one that already reaches beyond it.
        error = _scope_error(existing["target"])
        if not error:
            body = request.get_json(silent=True) or {}
            error, clean = device_groups.validate(body, _extra())
            if not error:
                error = _scope_error(clean["target"])
        if error:
            return jsonify({"error": error}), 400
        error, group = device_groups.save_group(db_path, clean, group_id=group_id,
                                                actor=_actor(), extra=_extra())
        if error:
            return jsonify({"error": error}), 400
        _audit("device_group_updated", group, {
            "previous_target": existing["target"],
            # The rules whose reach just changed without anybody editing them.
            "used_by": [r["name"] for r in rules.rules_using_group(db_path, group_id)],
        })
        return jsonify(_public(group)), 200

    @bp.route("/api/device-groups/<int:group_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def remove(group_id):
        existing = device_groups.get_group(db_path, group_id)
        if existing is None:
            return jsonify({"error": "no such group"}), 404
        error = _scope_error(existing["target"])
        if error:
            return jsonify({"error": error}), 400
        error, group = device_groups.delete_group(
            db_path, group_id, in_use=rules.rules_using_group(db_path, group_id))
        if error:
            # 409, not 400: well-formed and permitted, but it conflicts with the rules aimed at
            # this group -- the fix is to edit those rules. Same call as deleting a used script.
            return jsonify({"error": error}), 409
        _audit("device_group_deleted", group)
        return jsonify({"deleted": group["id"]}), 200

    # ---------------- Page ----------------
    @bp.route("/device-groups")
    @login_required
    @can_view
    def device_groups_page():
        return render_template("device_groups.html")

    return bp
