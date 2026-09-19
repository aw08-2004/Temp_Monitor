"""Flask HTTP surface for device push registration (roadmap #11 phase 2) -- a thin layer
over push.py, registered as a Blueprint from app.py.

**Every route here is reachable only by a PAIRED DEVICE, never by a browser session**, and
that is the gate this file exists to draw. The three of them all mean "this device", and
"this device" is a question a browser cannot answer: a console session carries an operator,
not an install of the app. So each route reads the token id off the authenticated identity
(`permissions_web.current_identity()`, which is where apitokens.authenticate stashes it) and
refuses when there is not one. The consequence worth stating is the one the refusal
guarantees: **there is no request shape by which one operator registers a push token against
another operator's device.** The device is never named in a body, so it cannot be named
wrongly.

The capability gate is `view` and nothing more. Push carries a COUNT and no content (see
push.py), so what a registered device learns is "you have N alerts you could see if you
opened the app" -- which is strictly less than the alert list `view` already grants it.
Inventing a `receive_push` capability would mean an operator who can see alerts in the
console being silently unable to be told about them on their phone, with nothing on screen
explaining why.

None of these is a machine route: they name a device, which is scoped by WHOSE it is rather
than by which machines it can reach, and the machine scoping happens inside push.scan where
the alert list is built. `access.require_machine` would have nothing to be handed.

The CSRF note from fleet_web.py applies with one addition: these callers are BEARER callers
by construction, and CSRF rides an ambient credential, so app.py's check does not apply to
them for the same reason it does not apply to `/api/agent/*`. The content-type requirement
is still enforced, because a body read with `request.get_json(silent=True)` from a caller
who did not declare JSON is a body that silently arrives as None.
"""
from flask import Blueprint, jsonify, request

import fleet
import permissions
import permissions_web
import push


def create_push_blueprint(db_path, login_required, access):
    bp = Blueprint("push", __name__)
    can_view = access.require(permissions.VIEW)

    def _device_token_id():
        """The device this request IS, or None when it is a browser session.

        From the authenticated identity, never from the body -- see the module docstring.
        """
        return permissions_web.current_identity().get("token_id")

    def _require_device():
        token_id = _device_token_id()
        if not token_id:
            return None, (jsonify({
                "error": "push registration is for a paired device, not a browser session",
            }), 403)
        return token_id, None

    def _require_json():
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    # ---------------- Status ----------------
    @bp.route("/api/push/status", methods=["GET"])
    @login_required
    @can_view
    def status():
        """Whether this hub can push, and whether this device is registered.

        **The app asks this before it asks the operating system for a notification
        permission.** A phone that prompts for notifications and then never sends one
        teaches its owner that the prompt was pointless, and the honest reason -- this hub
        has no Firebase credentials in its .env -- is one the app can only show if the hub
        says so. `configured` is therefore about the HUB and `registered` about the device,
        and they are separate fields because they fail separately.
        """
        token_id = _device_token_id()
        return jsonify({
            "configured": push.is_configured(),
            "kinds": list(push.PUSH_KINDS),
            "registration": push.registration(db_path, token_id) if token_id else None,
        }), 200

    # ---------------- Registration ----------------
    @bp.route("/api/push/register", methods=["POST"])
    @login_required
    @can_view
    def register():
        """Register, or re-register, where to push this device.

        Re-registration is the ORDINARY case rather than an error: FCM issues a new
        registration token whenever the app is reinstalled, its data is cleared, or it is
        restored onto a new phone, and the app is expected to call this on every launch. It
        is an upsert for that reason, and push.register leaves the alert cursor alone so
        that re-registering does not deliver a backlog.
        """
        token_id, refusal = _require_device()
        if refusal:
            return refusal
        bad = _require_json()
        if bad:
            return bad
        body = request.get_json(silent=True) or {}
        try:
            landed = push.register(db_path, token_id,
                                   kind=body.get("kind"), push_token=body.get("token"))
        except push.PushError as exc:
            return jsonify({"error": str(exc)}), 400
        if not landed:
            # The token authenticated a moment ago and the row is gone or revoked now --
            # an admin revoking the device mid-request. 409 rather than 404: the device is
            # not missing, its credential stopped being one.
            return jsonify({"error": "this device is no longer paired"}), 409

        fleet.audit(db_path, actor=permissions_web.current_actor(),
                    action="device.push_register", level=fleet.LEVEL_SECURITY,
                    target=token_id, detail={"kind": push.normalize_kind(body.get("kind"))})
        return jsonify({"registered": True,
                        "configured": push.is_configured()}), 200

    @bp.route("/api/push/register", methods=["DELETE"])
    @login_required
    @can_view
    def unregister():
        """Stop pushing to this device -- the operator turned notifications off in the app.

        Not a revoke: the device keeps its token and keeps working. The cursor survives too,
        so turning notifications back on an hour later delivers what is new rather than what
        was missed.
        """
        token_id, refusal = _require_device()
        if refusal:
            return refusal
        changed = push.unregister(db_path, token_id)
        if changed:
            fleet.audit(db_path, actor=permissions_web.current_actor(),
                        action="device.push_unregister", level=fleet.LEVEL_SECURITY,
                        target=token_id)
        return jsonify({"registered": False}), 200

    # ---------------- Acknowledgement ----------------
    @bp.route("/api/push/seen", methods=["POST"])
    @login_required
    @can_view
    def seen():
        """The operator opened the alert list on this device: everything so far is seen.

        **The only forward move of the cursor outside priming, and it belongs to the app**
        rather than to the scan's own timer. A notification that was delivered is not a
        notification that was read -- advancing on delivery is how the fourth alert of the
        night arrives announcing "1", which is precisely the count being wrong at the moment
        the count is the whole message.
        """
        token_id, refusal = _require_device()
        if refusal:
            return refusal
        return jsonify({"seen_through": push.acknowledge(db_path, token_id)}), 200

    return bp
