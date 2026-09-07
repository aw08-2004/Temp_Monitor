"""Flask HTTP surface for Android Device Owner provisioning (roadmap #23 phase A) -- a thin
layer over provisioning.py, registered as a Blueprint from app.py.

**One gate, and it is the widest thing this endpoint could reasonably be behind:
`manage_settings`.** What comes back carries the fleet's shared enrollment secret, because a
scan has to both provision the device AND enrol it or somebody types a secret into forty phones
by hand. That makes this endpoint a way to read `AGENT_ENROLLMENT_SECRET` out of the hub, so it
belongs with the capability that can already read and write the rest of the hub's configuration
-- not with `view`, and not with `issue_commands`. `manage_settings` is also exactly who edits
the two fields the payload is built from, so the page and its inputs sit behind one gate.

**Every fetch is audited, at security level.** Not because generating a QR is dangerous -- it
is not, the device is what does the work -- but because the answer contains the enrollment
secret, and "who took a copy of the fleet's enrollment secret, and when" is a question the trail
should be able to answer. `provisioning.redact` is what keeps the secret itself out of the row.

**Nothing here writes.** The two settings behind it are written through the ordinary settings
API, by the same capability, with the same audit -- a second write path would be a second set
of validation rules for one value.

The CSRF note from fleet_web.py applies verbatim to the POST below: the body is read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, render_template, request

import fleet
import permissions
import permissions_web
import provisioning
import refusals


def create_provisioning_blueprint(db_path, login_required, access, *, hub_url="",
                                  enrollment_secret=""):
    """Build the provisioning Blueprint.

    `hub_url` and `enrollment_secret` are passed in from app.py rather than read here, for the
    two reasons those values always are: the URL must be the hub's resolved public address and
    not whatever Host header reached it (a provisioned device gets one chance to be pointed at
    the right hub), and the secret lives in `.env`, which is app.py's to read.
    """
    bp = Blueprint("provisioning", __name__)
    can_manage = access.require(permissions.MANAGE_SETTINGS)

    @bp.route("/provisioning", methods=["GET"])
    @login_required
    @can_manage
    def provisioning_page():
        """The page an operator prints a QR from. Gated identically to the API behind it, so a
        reader never gets a page whose only content is a 403."""
        return render_template("provisioning.html")

    @bp.route("/api/provisioning/qr", methods=["GET"])
    @login_required
    @can_manage
    def provisioning_qr():
        """The payload for the QR, plus the JSON string to encode.

        A configuration that is not finished answers **409 with the reason**, not 200 with a
        half-built object. That is the whole point of the endpoint: a partial payload still
        scans and still starts provisioning, and it fails several minutes later on a device that
        has already been factory reset -- so refusing here, before anything is drawn, is the
        last moment the mistake is cheap. 409 rather than 400 because nothing is wrong with the
        REQUEST; the hub is not configured yet, and the console renders that as instructions.
        """
        try:
            payload = provisioning.build_payload(
                db_path, hub_url=hub_url, enrollment_secret=enrollment_secret)
        except provisioning.ProvisioningIncomplete as e:
            return jsonify({"error": str(e), "configured": False}), 409

        fleet.audit(db_path, actor=permissions_web.current_actor(),
                    action="provisioning_qr", level=fleet.LEVEL_SECURITY,
                    detail={"payload": provisioning.redact(payload),
                            # Recorded because it is the fact that makes the row worth having:
                            # a QR carrying the enrollment secret was handed to somebody.
                            "carries_enrollment_secret": bool(
                                (payload.get(provisioning.EXTRA_ADMIN_EXTRAS) or {})
                                .get(provisioning.BUNDLE_ENROLLMENT_SECRET))})
        return jsonify({
            "configured": True,
            # Both shapes, deliberately. `text` is what goes into the QR and must not be
            # rebuilt in JavaScript -- a browser's JSON.stringify would order keys differently
            # and produce a code that differs from the one the hub audited. `payload` is for
            # the human-readable table beside it.
            "text": provisioning.payload_json(payload),
            "payload": provisioning.redact(payload),
            "component": provisioning.ADMIN_COMPONENT,
        }), 200

    @bp.route("/api/provisioning/checksum", methods=["POST"])
    @login_required
    @can_manage
    def convert_checksum():
        """Convert the hex SHA-256 `apksigner` prints into the base64url the QR needs.

        A route for two lines of arithmetic, because this is the single most likely thing for
        an operator to get wrong and the consequence is not an error message. `apksigner verify
        --print-certs` prints a hex digest; the provisioning extra wants the same 32 bytes as
        URL-safe base64. Pasting the hex gives a 64-character string in the right alphabet that
        looks entirely plausible, saves without complaint, and fails on a device that has
        already been wiped. Offering the conversion next to the field is cheaper than any
        amount of help text.
        """
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        body = request.get_json(silent=True) or {}
        try:
            return jsonify({
                "checksum": provisioning.checksum_from_hex(body.get("digest")),
            }), 200
        except provisioning.ProvisioningIncomplete as e:
            return refusals.refuse(e)

    return bp
