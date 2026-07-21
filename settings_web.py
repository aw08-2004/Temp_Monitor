"""Flask HTTP surface for the Settings tab -- a thin layer over settings.py, split out
of app.py for the same reason fleet_web.py was.

Three endpoints, all console-facing and all behind the same Google sign-in as the rest
of the dashboard, via the login_required passed in from app.py.

The CSRF note in fleet_web.py's module docstring applies here verbatim and for the same
reason: reading the body with request.get_json(silent=True) requires
Content-Type: application/json, which is not CORS-safelisted, so a cross-origin fetch
preflights and fails and an HTML form cannot produce that content type. That matters
here because a settings POST can flip hub.auto_update -- i.e. turn on automatic
execution of code pulled from main. Do not add force=True and do not accept a
form-encoded fallback.

Every change is written to the existing fleet audit_log. Settings are not cosmetic:
data.retention_days permanently deletes history, and it does so asynchronously in a
background thread, so without an audit row "where did last month's data go?" has no
answer.
"""
import fleet
import permissions
import settings
from flask import Blueprint, jsonify, request, session


def create_settings_blueprint(db_path, login_required, access):
    """Build the settings Blueprint. `login_required` (app.py's session gate) and
    `access` (the permission-group layer) are both passed in, to avoid a circular
    import and to keep one source of truth for each."""
    bp = Blueprint("settings", __name__)
    # Settings are fleet-wide, so there is no machine scope to apply here -- the
    # capability IS the whole gate. That makes manage_settings a genuinely powerful
    # grant: data.retention_days deletes history for every machine, including ones
    # the holder cannot otherwise see, and hub.auto_update turns on execution of code
    # pulled from main. Do not hand it out as "can tweak the dashboard".
    manage = access.require(permissions.MANAGE_SETTINGS)

    def _current_email():
        """The signed-in operator. Always the source of attribution -- never take an
        email from the request body, or the audit trail becomes fiction."""
        return (session.get("user") or {}).get("email", "unknown")

    def _audit_changes(before, after, action):
        for key, value in after.items():
            if before.get(key) != value:
                fleet.audit(db_path, _current_email(), action, key,
                            {"from": before.get(key), "to": value})

    @bp.route("/api/settings", methods=["GET"])
    @login_required
    @manage
    def get_settings():
        """Schema + current values + defaults, grouped into sections. The Settings tab
        renders its whole form from this, which is what lets a new registry entry show
        up with no JS or HTML change."""
        return jsonify(settings.schema(db_path)), 200

    @bp.route("/api/settings", methods=["POST"])
    @login_required
    @manage
    def update_settings():
        # silent=True, never force=True -- see the module docstring.
        data = request.get_json(silent=True) or {}
        updates = data.get("updates")
        if not isinstance(updates, dict):
            return jsonify({"error": "updates must be an object of key -> value"}), 400
        if not updates:
            return jsonify({"status": "saved", "settings": settings.schema(db_path)}), 200

        before = settings.as_dict(db_path)
        try:
            applied = settings.set_many(db_path, updates, updated_by=_current_email())
        except ValueError as e:
            # settings.set_many validates everything before writing anything, so a 400
            # here means nothing changed. The message names the offending field and is
            # shown verbatim next to it in the UI.
            return jsonify({"error": str(e)}), 400

        _audit_changes(before, applied, "settings.update")
        return jsonify({"status": "saved", "settings": settings.schema(db_path)}), 200

    @bp.route("/api/settings/reset", methods=["POST"])
    @login_required
    @manage
    def reset_settings():
        """Drop the override rows for `keys` so they fall back to registry defaults."""
        data = request.get_json(silent=True) or {}
        keys = data.get("keys")
        if not isinstance(keys, list):
            return jsonify({"error": "keys must be a list"}), 400

        before = settings.as_dict(db_path)
        removed = settings.reset(db_path, keys, updated_by=_current_email())
        after = settings.as_dict(db_path)
        _audit_changes(before, {k: after[k] for k in removed if k in after},
                       "settings.reset")
        return jsonify({"status": "reset", "keys": removed,
                        "settings": settings.schema(db_path)}), 200

    return bp
