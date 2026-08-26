"""Flask surface for editing the sign-in providers (Settings -> Sign-in).

Lets the break-glass admins configure Google OAuth and a generic OIDC issuer from the
console instead of editing `.env` on the hub host and restarting the service.

**Gated on `ALLOWED_EMAILS` membership, not on any capability -- including
`manage_settings`.** This is the one page where that distinction matters: whoever can
point the hub at an OIDC issuer can point it at one they control, assert any email they
like, and sign in as anyone. `manage_settings` is meant to be delegable ("let the senior
tech tune retention"), and this must not ride along with it. Break-glass admins already
hold every capability over every machine, so for them it is not an escalation.

Two things this module is careful about, both because the thing being edited is the way
back in:

  * **it never leaves the hub with a broken provider.** The new config is validated, then
    applied, and if Authlib refuses to register it the previous configuration is put back
    -- in `.env`, in `os.environ`, and in the live clients -- before the error is returned.
  * **it never reads a secret back out.** The editor renders a placeholder for a secret
    that is set; sending that placeholder back means "leave it alone". Audit rows name the
    fields that changed and never their values, or the client secret would end up in the
    database and from there in the hub-database backup.
"""
from flask import Blueprint, jsonify, request

import authconfig
import fleet
import refusals


def create_auth_blueprint(db_path, login_required, access, env_path, reconfigure):
    """`reconfigure(config)` re-registers the live OAuth clients -- app.configure_oauth.
    Injected rather than imported so this module stays testable without booting app.py."""
    bp = Blueprint("authconfig", __name__)

    def _deny():
        return jsonify({"error":
            "Only the break-glass administrators listed in ALLOWED_EMAILS may change "
            "sign-in settings. This is the hub's perimeter: whoever configures the "
            "identity provider decides who this hub believes you are."}), 403

    @bp.route("/api/auth/providers", methods=["GET"])
    @login_required
    def get_providers():
        if not access.is_superuser():
            return _deny()
        current = authconfig.load_current()
        doc = authconfig.redacted(current)
        # The break-glass list itself, so the page can say who else can undo a change --
        # and so the admin can see they are editing the perimeter, not a preference.
        doc["superusers"] = sorted(access.superusers)
        doc["env_path_writable"] = bool(env_path)
        return jsonify(doc), 200

    @bp.route("/api/auth/providers", methods=["PUT"])
    @login_required
    def put_providers():
        if not access.is_superuser():
            return _deny()
        if not env_path:
            return jsonify({"error":
                "This deployment cannot write .env from the hub. Edit the file on the "
                "server and restart."}), 400

        data = request.get_json(silent=True) or {}
        before = authconfig.load_current()
        try:
            after = authconfig.validate(authconfig.merge(before, data))
        except authconfig.AuthConfigError as e:
            return refusals.refuse(e)

        changed = authconfig.describe_changes(before, after)
        if not changed:
            return jsonify(dict(authconfig.redacted(after), changed=[])), 200

        authconfig.save(env_path, after)
        try:
            reconfigure(after)
        except Exception as e:
            # Authlib refused the new configuration -- almost always an unreachable or
            # malformed discovery URL, which it only finds out at registration. Put
            # everything back before answering, so the failure costs an error message
            # rather than the ability to sign in.
            authconfig.save(env_path, before)
            try:
                reconfigure(before)
            except Exception as rollback_error:
                # Both failed. Say so loudly and specifically: this is the one state an
                # operator cannot diagnose from the console, because the console may be
                # about to stop letting them in.
                print(f"[auth] ROLLBACK FAILED after a bad provider change: "
                      f"{rollback_error}. .env has been restored; RESTART THE HUB.")
                return jsonify({"error":
                    f"The new sign-in configuration was rejected ({e}), and restoring the "
                    f"previous one also failed. The previous settings have been written "
                    f"back to .env -- restart the hub to load them."}), 500
            return jsonify({"error":
                f"That sign-in configuration was rejected: {e}. Nothing was changed -- "
                f"check the issuer URL is reachable from the hub and serves a discovery "
                f"document."}), 400

        # Field NAMES only. A client secret in an audit row would be a credential in the
        # database, and from there in every hub-database backup.
        fleet.audit(db_path, access.email(), "auth.providers.update", "sign-in", {
            "changed_fields": changed,
            "google_enabled": authconfig.google_enabled(after),
            "oidc_enabled": authconfig.oidc_enabled(after),
            "oidc_metadata_url": after.get("oidc_metadata_url") or None,
        }, level=fleet.LEVEL_SECURITY)

        return jsonify(dict(authconfig.redacted(after), changed=changed)), 200

    return bp
