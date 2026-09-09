"""Flask HTTP surface for Android Device Owner provisioning (roadmap #23 phase A) -- a thin
layer over provisioning.py and apkhost.py, registered as a Blueprint from app.py.

**Every operator-facing route here is `manage_settings`, and it is the widest thing they could
reasonably be behind.** The QR carries the fleet's shared enrollment secret, because a scan has
to both provision the device AND enrol it or somebody types a secret into forty phones by hand.
That makes it a way to read `AGENT_ENROLLMENT_SECRET` out of the hub, so it belongs with the
capability that can already read and write the rest of the hub's configuration -- not with
`view`, and not with `issue_commands`. Uploading the APK sits behind the same gate for a
different reason of the same size: it decides what a factory-reset device installs as its
device owner.

**Every fetch and every write is audited, at security level.** Not because generating a QR is
dangerous -- it is not, the device is what does the work -- but because the answer contains the
enrollment secret, and "who took a copy of it, and when" is a question the trail should be able
to answer. `provisioning.redact` keeps the secret itself out of the row, and the download token
is never audited at all: it is the URL's only unguessable part, and `view_audit_log` is a much
wider audience than `manage_settings`.

**One route here has no gate at all, and cannot have one.** The APK download is fetched by the
setup wizard of a device that has just been factory reset: no session cookie, no agent bearer
token, no device token, nothing to authenticate with -- enrollment happens FROM the app it is
downloading. What stands in for a gate is that the URL carries a random token which is looked up
rather than joined to a path, so the only file this hub can ever serve through it is the one
digest recorded in `provisioning_apk`. See apkhost.py on what that token is and is not.

The CSRF note from fleet_web.py applies verbatim to the JSON POST below. The multipart upload is
exempt from the content-type rule through app.py's CSRF_UPLOAD_ENDPOINTS, and unlike the other
three entries there it is not inert -- the reasoning is written out beside it.
"""
import os

from flask import Blueprint, jsonify, render_template, request, send_file

import apkhost
import fleet
import permissions
import permissions_web
import provisioning
import refusals
import settings


def create_provisioning_blueprint(db_path, log_dir, login_required, access, *, hub_url="",
                                  enrollment_secret=""):
    """Build the provisioning Blueprint.

    `hub_url` and `enrollment_secret` are passed in from app.py rather than read here, for the
    two reasons those values always are: the URL must be the hub's resolved public address and
    not whatever Host header reached it (a provisioned device gets one chance to be pointed at
    the right hub), and the secret lives in `.env`, which is app.py's to read. `log_dir` comes
    in for the third: where state lives is app.py's layout decision, and a blueprint that
    derived it would be a second copy of that decision.
    """
    bp = Blueprint("provisioning", __name__)
    can_manage = access.require(permissions.MANAGE_SETTINGS)
    root = apkhost.blob_root(log_dir)

    def _hosted():
        """The hosted APK as build_payload wants it, or None."""
        record = apkhost.get_hosted(db_path)
        if record is None:
            return None
        return {"url": apkhost.download_url(hub_url, record["token"]),
                "checksum": record["checksum"]}

    def _apk_view(record):
        """The record as the console renders it. **The raw token is not in it** -- the download
        URL is, which is the one thing anybody needs to copy, and shipping the token separately
        would put it in one more place."""
        if record is None:
            return {"hosted": False}
        return {
            "hosted": True,
            "sha256": record["sha256"],
            "size_bytes": record["size_bytes"],
            "file_name": record["file_name"],
            "checksum": record["checksum"],
            "uploaded_by": record["uploaded_by"],
            "uploaded_at": record["uploaded_at"],
            "download_url": apkhost.download_url(hub_url, record["token"]),
        }

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
                db_path, hub_url=hub_url, enrollment_secret=enrollment_secret,
                hosted=_hosted())
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

    # ---------------- The hosted APK ----------------
    @bp.route("/api/provisioning/apk", methods=["GET"])
    @login_required
    @can_manage
    def hosted_apk():
        """What this hub is currently handing out, if anything."""
        return jsonify(_apk_view(apkhost.get_hosted(db_path))), 200

    @bp.route("/api/provisioning/apk", methods=["POST"])
    @login_required
    @can_manage
    def upload_provisioning_apk():
        """Upload the signed APK and make it the hosted one.

        Multipart, like the package and firmware uploads it borrows its storage from. The
        checksum is derived from the file rather than asked for -- that derivation is the whole
        point of this endpoint, and it is done before anything is recorded so an APK this hub
        cannot read is refused while the operator is still standing here.

        Replaces whatever was hosted, and mints a new download token, so codes printed from the
        previous APK stop resolving rather than pointing at a file that is no longer there.
        """
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "Choose a signed APK to upload."}), 400

        max_bytes = settings.get_int(db_path, "provisioning.max_apk_mb") * 1024 * 1024
        actor = permissions_web.current_actor()
        previous = apkhost.get_hosted(db_path)
        try:
            record = apkhost.store_upload(
                db_path, root, upload.stream, max_bytes,
                file_name=os.path.basename(upload.filename), actor=actor)
        except provisioning.ProvisioningIncomplete as e:
            # The parser names the exact cause -- a v1-only signature, two signers, a v2 and v3
            # block that disagree -- and a generic message would throw away the only part that
            # tells somebody what to do about it.
            return refusals.refuse(e)
        except ValueError as e:
            return refusals.refuse(e)          # too large, or empty
        except OSError as e:
            return jsonify({"error": f"Could not store the APK: {e}"}), 500

        fleet.audit(db_path, actor=actor, action="provisioning_apk_upload",
                    level=fleet.LEVEL_SECURITY, target=record["file_name"],
                    # No token. See the module docstring.
                    detail={"sha256": record["sha256"], "bytes": record["size_bytes"],
                            "checksum": record["checksum"],
                            "replaced_sha256": (previous or {}).get("sha256", "")})
        return jsonify(_apk_view(record)), 201

    @bp.route("/api/provisioning/apk", methods=["DELETE"])
    @login_required
    @can_manage
    def remove_provisioning_apk():
        """Stop hosting the APK. The off switch for the one unauthenticated route in this hub:
        afterwards the download answers 404 and no QR can be built."""
        record = apkhost.get_hosted(db_path)
        if not apkhost.remove_hosted(db_path, root):
            return jsonify({"error": "No APK is hosted on this hub."}), 404
        fleet.audit(db_path, actor=permissions_web.current_actor(),
                    action="provisioning_apk_remove", level=fleet.LEVEL_SECURITY,
                    target=(record or {}).get("file_name", ""),
                    detail={"sha256": (record or {}).get("sha256", "")})
        return jsonify({"status": "removed", "hosted": False}), 200

    @bp.route("/provisioning/apk/<token>/" + apkhost.DOWNLOAD_NAME, methods=["GET"])
    def download_provisioning_apk(token):
        """The APK itself, to whoever holds the token.

        **No decorator, and there is nothing this could be gated on.** The caller is the setup
        wizard of a device that has just been factory reset: it has no session, no agent bearer
        token and no device token, and it is downloading the very app that would later enrol it.

        An unknown token and a malformed one answer the same 404, so the route says nothing
        about whether an APK is hosted at all. A record whose file is missing answers **410**
        rather than 404, because those are different problems: the first means the token is
        wrong, the second means this hub's database and its state directory have come apart --
        which is what a restore-from-backup looks like, and which somebody has to be told about
        rather than left to debug from a device that has already been wiped.
        """
        record = apkhost.hosted_by_token(db_path, token)
        if record is None:
            return jsonify({"error": "not found"}), 404

        path = apkhost.blob_path(root, record["sha256"])
        if not os.path.exists(path):
            return jsonify({"error": "The hosted APK is missing on this hub. It has to be "
                                     "uploaded again before a device can be provisioned."}), 410
        return send_file(path, as_attachment=True,
                         download_name=apkhost.DOWNLOAD_NAME,
                         mimetype=apkhost.CONTENT_TYPE)

    @bp.route("/api/provisioning/checksum", methods=["POST"])
    @login_required
    @can_manage
    def convert_checksum():
        """Convert the hex SHA-256 `apksigner` prints into the base64url the QR needs.

        **It is a cross-check now rather than an input.** The hub derives the checksum from the
        uploaded APK itself, so nobody has to convert anything -- but that derivation is code
        this hub wrote, and the checksum it produces is only discovered to be wrong on a device
        that has already been factory reset. Letting an operator paste what `apksigner verify
        --print-certs` prints and watch it match the value on the card above is the cheapest
        reassurance available, and it costs two lines of arithmetic that were already here.
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
