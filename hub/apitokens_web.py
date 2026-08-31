"""Flask surface for device pairing, device administration, and the Download Client page
(roadmap #11) -- a thin layer over apitokens.py and clientrelease.py.

Three audiences, three gates, in the shape packages_web.py already uses:

  * **A browser, pairing a device** (`/app/pair`, `/app/pair/confirm`) -- the ordinary
    session gate. Pairing is something a signed-in operator does in a browser: that is the
    whole mechanism, since it is the browser sign-in that proves who the device belongs
    to. No capability beyond `view`, because a device can never hold more than its owner
    already holds (permissions_web._narrow_to_device intersects them on every request).

  * **A device, collecting its token** (`/api/tokens/exchange`) -- deliberately
    UNAUTHENTICATED, because the code is the credential and the app has nothing else yet.
    It is safe for exactly three reasons, all enforced in apitokens.py: the code lives
    sixty seconds, it is single-use (claimed by a conditional DELETE, so a race has one
    winner), and it was minted only after a real sign-in and an explicit confirmation.

  * **The console, listing and revoking devices** (`/api/tokens`, `/api/tokens/all`) --
    an operator manages their own devices with no special capability; managing everybody's
    requires `manage_users`, which is already the capability for "administers people".

**The pairing redirect is the sharpest edge in this feature and is validated in one
place**, apitokens.validate_loopback_redirect. It carries a code that becomes a fleet
credential, and it arrives in a query string -- an open redirect here mails somebody's
console access to whoever crafted the link. Do not relax it to "starts with http://
127." and do not add a hostname allow-list setting.

The CSRF note from fleet_web.py applies to everything session-gated here. `/app/pair` is a
GET that changes nothing; the confirmation is a JSON POST like every other write.
"""
import os

from flask import Blueprint, jsonify, render_template, request, send_file

import apitokens
import channels
import clientrelease
import i18n
import permissions
import permissions_web
import refusals
import settings


def create_apitokens_blueprint(db_path, login_required, access, code_dir=""):
    """Build the device-token Blueprint.

    `code_dir` is the hub's own code directory -- where the signed client manifest ships,
    for the reason clientrelease.py's docstring gives. Passed in rather than imported from
    app.py, to avoid a circular import and because the tests re-point it.
    """
    bp = Blueprint("apitokens", __name__)
    can_view = access.require(permissions.VIEW)
    manage_users = access.require(permissions.MANAGE_USERS)

    def _actor():
        return permissions_web.current_actor()

    def _lifetime_days():
        return settings.get_int(db_path, "hub.device_token_lifetime_days") \
            or apitokens.DEFAULT_LIFETIME_DAYS

    def _channel():
        """Which client build this hub offers (roadmap #21).

        Read per request rather than captured when the blueprint is built, so switching the
        channel in Settings takes effect on the next page load instead of at the next hub
        restart. A hub with no beta client published simply keeps serving stable's manifest
        until one exists -- load_manifest reports "nothing published yet" for the channel it
        was asked about, which is the honest answer rather than a silent fallback.
        """
        return channels.normalize(settings.get(db_path, "hub.client_update_channel"))

    def _grantable_names():
        """Which capabilities THIS operator could put on a device: what a device may ever
        hold, narrowed to what they themselves hold. Offering a tick box for something the
        intersection would strip at every request is offering a lie."""
        mine = set(access.current().get("capabilities") or ())
        return [c for c in apitokens.DEVICE_CAPABILITIES if c in mine]

    def _described(names):
        """Capability names with their catalog text, resolved in the CALLER's language.

        Server-supplied UI text, resolved server-side -- the same rule packages_web.py
        follows for detection and step kinds. The consent page must not build a catalog
        key by concatenation: a computed key is invisible to test_i18n.py's typo scan,
        which is the only thing that notices a mistyped key before an operator does.
        """
        lang = i18n.current()
        return [{
            "name": name,
            "label": i18n.translate(
                f"{permissions.CAPABILITY_TEXT_KEY}.{name}.label", lang),
            "description": i18n.translate(
                f"{permissions.CAPABILITY_TEXT_KEY}.{name}.description", lang),
        } for name in names]

    # ================================
    # PAIRING (browser)
    # ================================
    @bp.route("/app/pair", methods=["GET"])
    @login_required
    @can_view
    def pair_page():
        """The consent page a native client opens in the system browser.

        Everything the app asked for arrives in the query string and is shown back to the
        operator BEFORE anything is minted: which device, which capabilities, and where
        the code will be sent. A pairing page that just says "allow?" is a page nobody can
        answer honestly.
        """
        redirect_uri = request.args.get("redirect", "")
        redirect_error = ""
        if redirect_uri:
            try:
                apitokens.validate_loopback_redirect(redirect_uri)
            except apitokens.PairingError as e:
                # Shown, not silently dropped: an app author hitting this needs to know
                # which string to change, and an operator needs to know why the page is
                # refusing to finish. The page still renders, offering the copy-paste
                # path, so a device that cannot listen locally is not stranded.
                redirect_error = str(e)

        grantable = _grantable_names()
        return render_template(
            "pair.html",
            device_name=request.args.get("name", "")[:apitokens.MAX_DEVICE_NAME_CHARS],
            platform=request.args.get("platform", "")[:apitokens.MAX_PLATFORM_CHARS],
            state=request.args.get("state", "")[:128],
            redirect_uri=redirect_uri,
            redirect_error=redirect_error,
            grantable=_described(grantable),
            defaults=[c for c in apitokens.DEFAULT_DEVICE_CAPABILITIES
                      if c in set(grantable)],
            lifetime_days=_lifetime_days(),
        )

    @bp.route("/app/pair/confirm", methods=["POST"])
    @login_required
    @can_view
    def pair_confirm():
        """Record the consented grant and hand back the one-time code.

        Answers with the code AND, when the app gave a loopback redirect, the URL to send
        the browser to. The page decides which to use; both paths end at the same
        single-use exchange, so a device that cannot listen locally is never a second
        mechanism with its own rules.
        """
        body = request.get_json(silent=True) or {}
        wanted = body.get("capabilities") or []

        # The intersection is applied here as well as at every later request, so an
        # operator cannot pair a device with a capability they do not hold -- the token
        # would be inert, and a device listing capabilities that never work is worse than
        # one that refused at pairing.
        mine = set(access.current().get("capabilities") or ())
        over_grant = sorted(set(wanted) - mine - apitokens.DEVICE_FORBIDDEN_CAPABILITIES)
        if over_grant:
            return jsonify({"error": "You cannot grant a device a capability you do not "
                                     f"hold: {', '.join(over_grant)}."}), 403

        redirect_uri = str(body.get("redirect") or "").strip()
        if redirect_uri:
            try:
                apitokens.validate_loopback_redirect(redirect_uri)
            except apitokens.PairingError as e:
                return refusals.refuse(e)

        try:
            code = apitokens.create_grant(
                db_path,
                email=permissions_web.current_identity().get("email"),
                device_name=body.get("device_name"),
                platform=body.get("platform"),
                capabilities=wanted,
                directory_groups=permissions_web.current_identity().get(
                    "directory_groups") or (),
                lifetime_days=_lifetime_days(),
            )
        except apitokens.PairingError as e:
            return refusals.refuse(e)

        answer = {"code": code, "expires_in": apitokens.PAIRING_CODE_TTL_SECONDS}
        if redirect_uri:
            from urllib.parse import urlencode
            state = str(body.get("state") or "")
            joiner = "&" if "?" in redirect_uri else "?"
            params = {"code": code}
            if state:
                params["state"] = state
            answer["redirect"] = f"{redirect_uri}{joiner}{urlencode(params)}"
        return jsonify(answer), 200

    @bp.route("/api/tokens/exchange", methods=["POST"])
    def exchange():
        """Code -> token, once. No session and no capability: see the module docstring.

        Every failure answers 400 with the same message the model produced, and none of
        them distinguishes "no such code" from "already used" beyond what a human needs --
        there is nothing to enumerate here, because a code is one-shot and expires in a
        minute either way.
        """
        body = request.get_json(silent=True) or {}
        try:
            token, row = apitokens.redeem_grant(db_path, body.get("code"))
        except apitokens.PairingError as e:
            return refusals.refuse(e)
        return jsonify({
            # The only time this value exists outside the device. It is not stored, and
            # there is no endpoint that can show it again.
            "token": token,
            "token_id": row["token_id"],
            "email": row["email"],
            "device_name": row["device_name"],
            "capabilities": row["capabilities"],
            "expires_at": row["expires_at"],
        }), 200

    # ================================
    # DEVICE ADMINISTRATION (console)
    # ================================
    @bp.route("/api/tokens", methods=["GET"])
    @login_required
    @can_view
    def my_devices():
        """The caller's own paired devices. No capability beyond `view`: seeing which
        devices you yourself paired is not an administrative act, and requiring one would
        mean an operator could hold a credential they cannot see or revoke."""
        email = permissions_web.current_identity().get("email")
        return jsonify({
            "devices": apitokens.list_tokens(db_path, email=email),
            "grantable": _described(_grantable_names()),
            "lifetime_days": _lifetime_days(),
        }), 200

    @bp.route("/api/tokens/all", methods=["GET"])
    @login_required
    @manage_users
    def all_devices():
        """Every paired device on the hub. `manage_users` because this is the list an
        admin needs when somebody leaves, and it names other people's equipment."""
        email = str(request.args.get("email") or "").strip()
        return jsonify({
            "devices": apitokens.list_tokens(db_path, email=email or None,
                                             include_revoked=True),
        }), 200

    @bp.route("/api/tokens/<token_id>", methods=["DELETE"])
    @login_required
    @can_view
    def revoke_device(token_id):
        """Revoke a device. Scoped to the caller's own devices unless they hold
        `manage_users`.

        The scoping is passed into the UPDATE rather than checked first, so it cannot be
        raced, and a token id belonging to somebody else is a plain 404 -- distinguishing
        "not yours" from "does not exist" would let anyone enumerate the fleet's devices
        one guess at a time.
        """
        owner = None if access.can(permissions.MANAGE_USERS) else \
            permissions_web.current_identity().get("email")
        if not apitokens.revoke_token(db_path, token_id, actor=_actor(), email=owner):
            return jsonify({"error": "No such device."}), 404
        return jsonify({"revoked": token_id}), 200

    @bp.route("/api/tokens/user/<path:email>", methods=["DELETE"])
    @login_required
    @manage_users
    def revoke_user_devices(email):
        """Revoke every device belonging to one operator -- the lever for a departure.

        Removing them from every permission group already makes their tokens inert (the
        capability intersection is live), but that leaves a working credential pointed at
        this hub. Both should happen, and this is the half that is about the credential.
        """
        count = apitokens.revoke_all_for_email(db_path, email, actor=_actor())
        return jsonify({"revoked": count}), 200

    # ================================
    # DOWNLOAD CLIENT
    # ================================
    @bp.route("/download")
    @login_required
    @can_view
    def download_page():
        """The Download Client page. `view` only: anyone allowed into the console may
        install the client, because the client can do nothing on its own -- the token it
        is later paired with is what decides that."""
        return render_template("download.html")

    @bp.route("/api/app/manifest", methods=["GET"])
    @login_required
    @can_view
    def app_manifest():
        """What is downloadable, straight from the signed manifest.

        A verification failure is a 503 carrying the reason, not an empty list: "no client
        has been published" and "the manifest is not signed by this hub's release key" are
        completely different situations, and an empty page would render them identically.
        """
        try:
            return jsonify(clientrelease.load_manifest(code_dir, channel=_channel())), 200
        except clientrelease.ManifestError as e:
            return refusals.refuse(e, 503)

    @bp.route("/download/manifest.json", methods=["GET"])
    def raw_manifest():
        """The manifest bytes and nothing else, for a client checking for its own update.

        Unauthenticated on purpose: it is a signed, public document that says which
        versions exist, an installed client needs it before it has been paired with
        anything, and refusing it would only mean the app could never tell the user it was
        out of date. It is served as the EXACT bytes on disk so the detached signature
        beside it still verifies -- jsonify would re-serialise them and break that.
        """
        manifest_path, _ = clientrelease.manifest_paths(code_dir, _channel())
        if not os.path.exists(manifest_path):
            return jsonify({"error": "No client release has been published."}), 404
        return send_file(manifest_path, mimetype="application/json")

    @bp.route("/download/manifest.json.sig", methods=["GET"])
    def raw_manifest_sig():
        """The detached signature for the above. Public for the same reason, and useless
        without the bytes it signs."""
        _, sig_path = clientrelease.manifest_paths(code_dir, _channel())
        if not os.path.exists(sig_path):
            return jsonify({"error": "No client release has been published."}), 404
        return send_file(sig_path, mimetype="text/plain")

    return bp
