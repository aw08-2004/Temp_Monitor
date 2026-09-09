"""HTTP-layer test for provisioning_web.py (roadmap #23 phase A).

Wires the blueprint directly onto a minimal Flask app, avoiding app.py's OAuth boot -- same
approach as test_capabilities_web / test_wake_web / test_bios_web.

**The silent failure this file exists to catch is the fleet's enrollment secret leaking
sideways.** The provisioning QR has to carry `AGENT_ENROLLMENT_SECRET`, because a scan that
provisions a device without enrolling it leaves a managed machine that silently accepts no
commands. That makes this endpoint a way to read the secret out of the hub -- so it must sit
behind `manage_settings` and nothing narrower, and the audit row it writes must NOT contain the
secret it just handed out. A row that did would put the fleet's shared secret in front of
everyone holding `view_audit_log`, which is a much wider audience than the gate on the endpoint.
Neither of those is visible from the outside: the page looks identical either way.

The second thing asserted is that an unconfigured hub answers with instructions rather than a
payload. A partial payload still encodes, still scans, and still fails minutes later on a
device that has already been factory reset.

The third arrived with the hub hosting the APK itself: **one route here has no gate at all, and
that is the requirement rather than an oversight.** The setup wizard of a factory-reset device
has no session, no agent token and nothing to enrol with -- it is downloading the app that would
later enrol it. So the download must answer an anonymous request, and what stands in for a gate
is a token that is looked UP rather than joined to a path. Asserted directly, in both
directions: it answers with no session, and it answers nothing at all for a token that was never
issued, with the same body a malformed one gets so the route says nothing about whether an APK
is hosted.
"""
import functools
import hashlib
import io
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))
import apkhost
import fleet
import i18n
import permissions
import provisioning
import settings
from permissions_web import create_access
from provisioning_web import create_provisioning_blueprint
# The signing-block builder lives beside the parser's own tests. Imported rather than
# duplicated: two copies of a binary format description is how they come to disagree.
from test_apkhost import block_value, signer, synth_apk
from flask import Blueprint, Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"

SECRET = "fleet-enrollment-secret"
CHECKSUM = "s5oWEFPq3ECQxStcWufVzr1o2G5wngEgQ1R7NRKF9Gg"
CERT = b"pretend-certificate-bytes"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def _register_sidebar_stubs(app):
    """base.html includes the shared sidebar, which url_for()s every other page, and the
    permission gate's own denial renders denied.html through the same base. Without these
    stubs BOTH the allowed page and the refused one raise a BuildError, and the gate under
    test would read as a 500 either way. Same helper as test_mobile_nav.py."""
    for endpoint in ("index", "inventory_page", "alerts_page", "tools_page",
                     "remote_page", "settings_page", "permissions_page", "logout"):
        app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "", methods=["GET"])
    for name, endpoint in (("packages", "packages_page"), ("backups", "backups_page"),
                           ("invites", "invites_page"), ("users", "users_page"),
                           ("audit", "audit_page"), ("bios", "firmware_page"),
                           ("rules", "rules_page"), ("patches", "patches_page"),
                           ("apitokens", "download_page"), ("sharing", "sharing_page"),
                           ("location", "fleet_map_page"),
                           ("policy", "policy_page")):
        bp = Blueprint(name, __name__)
        bp.add_url_rule(f"/_stub/{name}", endpoint, lambda: "", methods=["GET"])
        app.register_blueprint(bp)


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        apkhost.init_apkhost_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()

        app = Flask(__name__,
                    template_folder=os.path.join(ROOT, "hub", "templates"),
                    static_folder=os.path.join(ROOT, "hub", "static"))
        app.secret_key = "test"
        _register_sidebar_stubs(app)
        access = create_access(db_path, {"super@x.com"})
        # An operator who can do everything EXCEPT manage settings. The point of this group is
        # that `issue_commands` is the capability somebody would reach for by reflex here --
        # provisioning a device feels like a fleet action -- and it must not be enough.
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS,
                          permissions.DEPLOY_PACKAGES],
            machines=[], members=["tech@x.com"])
        permissions.create_group(
            db_path, "Admins", capabilities=[permissions.VIEW, permissions.MANAGE_SETTINGS],
            machines=[], members=["admin@x.com"])
        settings.invalidate()

        # A stand-in for LOG_DIR. apkhost puts its blobs in a `provisioning`
        # directory under it, which is why the assertions below go through
        # apkhost.blob_root rather than joining a path by hand.
        state_dir = tempfile.mkdtemp(prefix="prov-web-")
        app.register_blueprint(create_provisioning_blueprint(
            db_path, state_dir, fake_login_required, access,
            hub_url="https://fleet.example.com", enrollment_secret=SECRET))

        @app.before_request
        def _seed_session():
            # None means genuinely signed out, which is the state the APK download has to work
            # in -- a factory-reset device has no session to seed.
            if CURRENT_USER:
                flask_session["user"] = {"email": CURRENT_USER}
            else:
                flask_session.pop("user", None)

        # Mirrors app.py's inject_nav_context, so base.html and denied.html render. Every
        # capability is granted to the SIDEBAR because nav rendering is not what is under
        # test; the gate on the route reads the real groups above.
        @app.context_processor
        def _nav_context():
            context = {"cap": permissions, "hub_version": "test",
                       "user_capabilities": set(permissions.CAPABILITIES),
                       "open_alert_count": 0, "is_superuser": True,
                       "latest_agent_version": "8.8.8"}
            context.update(i18n.template_context("en"))
            return context

        c = app.test_client()

        print("== An unconfigured hub answers with instructions, not a payload ==")
        r = c.get("/api/provisioning/qr")
        body = r.get_json()
        check("it answers 409, not 200 with a half-built payload", r.status_code == 409)
        check("...and says so plainly", body.get("configured") is False)
        check("...naming what is missing and how to produce it",
              "upload" in (body.get("error") or "").lower())
        check("...and carries no payload at all",
              "text" not in body and "payload" not in body)

        print("\n== Uploading the APK is what configures it ==")
        apk = synth_apk({0x7109871A: block_value([signer([CERT])])})
        r = c.post("/api/provisioning/apk",
                   data={"file": (io.BytesIO(apk), "fleethub-agent.apk")},
                   content_type="multipart/form-data")
        hosted = r.get_json()
        check("an APK uploads", r.status_code == 201)
        check("...and the hub derived the checksum, so nobody typed one",
              hosted["checksum"] == provisioning.checksum_from_hex(
                  hashlib.sha256(CERT).hexdigest()))
        check("...naming the file it came from", hosted["file_name"] == "fleethub-agent.apk")
        check("...and answering with a download URL under this hub's own address",
              hosted["download_url"].startswith(
                  "https://fleet.example.com/provisioning/apk/"))
        # The token is the URL's only unguessable part, so it is not shipped as a field of its
        # own -- there is one place to copy from, and one place it can leak from.
        check("the raw token is not a field of its own", "token" not in hosted)

        r = c.post("/api/provisioning/apk",
                   data={"file": (io.BytesIO(b"not an apk at all"), "junk.apk")},
                   content_type="multipart/form-data")
        check("a file that is not an APK is refused with a reason", r.status_code == 400)
        check("...and the hosted APK is unchanged",
              c.get("/api/provisioning/apk").get_json()["sha256"] == hosted["sha256"])
        r = c.post("/api/provisioning/apk", content_type="multipart/form-data")
        check("an upload with no file at all is a 400, not a 500", r.status_code == 400)

        print("\n== Configured ==")
        r = c.get("/api/provisioning/qr")
        body = r.get_json()
        check("it answers 200", r.status_code == 200)
        check("...with the exact string to encode", bool(body.get("text")))
        check("...which parses as the provisioning JSON",
              json.loads(body["text"])[provisioning.EXTRA_SIGNATURE_CHECKSUM]
              == hosted["checksum"])
        check("...whose download location is this hub, not somewhere an operator typed",
              json.loads(body["text"])[provisioning.EXTRA_DOWNLOAD_LOCATION]
              == hosted["download_url"])
        check("...and names the admin component separately for the page to show",
              body.get("component") == provisioning.ADMIN_COMPONENT)
        # The one thing the browser must NOT do: rebuild the string itself. Asserted here
        # because the endpoint is what makes it unnecessary.
        check("the encoded string carries the real enrollment secret",
              SECRET in body["text"])
        check("...while the payload rendered on screen does not",
              SECRET not in json.dumps(body["payload"]))

        print("\n== The audit row must not carry what the QR carries ==")
        rows = fleet.list_audit(db_path, action="provisioning_qr", limit=20)["entries"]
        issued = list(rows)
        check("fetching the QR is audited", len(issued) >= 1)
        row = issued[0]
        check("...at security level, like issuing a command",
              row["level"] == fleet.LEVEL_SECURITY)
        check("...naming who asked", row["actor"] == "super@x.com")
        serialized = json.dumps(row)
        check("...and the enrollment secret is NOT in the row", SECRET not in serialized)
        check("...but the fact that the code carries one is",
              "carries_enrollment_secret" in serialized and "(set)" in serialized)

        print("\n== The checksum converter ==")
        r = c.post("/api/provisioning/checksum", json={
            "digest": "b39a161053eadc4090c52b5c5ae7d5cebd68d86e709e012043547b351285f468"})
        check("a hex digest converts", r.status_code == 200
              and r.get_json().get("checksum") == CHECKSUM)
        r = c.post("/api/provisioning/checksum", json={"digest": "not-a-digest"})
        check("garbage is refused with a reason, not a 500", r.status_code == 400)
        check("...and the reason names apksigner, so somebody knows where to look",
              "apksigner" in (r.get_json().get("error") or ""))
        r = c.post("/api/provisioning/checksum", data="digest=abc",
                   content_type="application/x-www-form-urlencoded")
        # The CSRF shape fleet_web.py documents: a form-encoded POST is the one cross-site
        # request that needs no preflight, so every console endpoint must refuse it.
        check("a form-encoded body is refused, which is what keeps this CSRF-proof",
              r.status_code == 415)

        print("\n== Gating ==")
        CURRENT_USER = "admin@x.com"
        check("manage_settings can read the payload",
              c.get("/api/provisioning/qr").status_code == 200)
        page = c.get("/provisioning")
        check("...and the page", page.status_code == 200)
        html = page.get_data(as_text=True)
        check("...which carries the canvas provisioning.js paints into",
              'id="qr-canvas"' in html)
        # Every id the script reaches for. A rename in one file and not the other is silent:
        # getElementById returns null, the listener is never attached, and the card renders as
        # a button that does nothing on the one page whose mistakes cost a factory reset.
        script = open(os.path.join(ROOT, "hub", "static", "js", "provisioning.js"),
                      encoding="utf-8").read()
        wanted = set(re.findall(r"getElementById\('([^']+)'\)", script))
        missing = sorted(i for i in wanted if f'id="{i}"' not in html)
        check(f"the page carries every id the script looks up ({len(wanted)} of them)",
              not missing)
        check("...loads the vendored encoder before the page script, and in that order",
              html.index("vendor/qrcode.js") < html.index("vendor/qrcode_UTF8.js")
              < html.index("js/provisioning.js"))
        check("...and warns about the factory reset above the code, not below it",
              "banner--warn" in html
              and html.index("banner--warn") < html.index('id="qr-canvas"'))
        check("the page never renders the payload server-side, so no secret is in the HTML",
              SECRET not in html)
        CURRENT_USER = "tech@x.com"
        # issue_commands is the capability somebody reaches for by reflex. It is not enough,
        # and that is the whole gating decision: this endpoint hands out a credential.
        check("issue_commands is NOT enough to read the payload",
              c.get("/api/provisioning/qr").status_code == 403)
        check("...nor to open the page", c.get("/provisioning").status_code == 403)
        check("...nor to use the converter",
              c.post("/api/provisioning/checksum", json={"digest": "ab"}).status_code == 403)
        CURRENT_USER = "nobody@x.com"
        check("someone with no capability at all is refused",
              c.get("/api/provisioning/qr").status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== The download answers an anonymous request, because it must ==")
        # The assertion this section exists for. The caller is the setup wizard of a device
        # that has just been factory reset: no session, no agent token, nothing to present.
        url = hosted["download_url"]
        path = url[len("https://fleet.example.com"):]
        CURRENT_USER = None
        check("a signed-out caller cannot read what is hosted, which is the control case",
              c.get("/api/provisioning/apk").status_code == 403)
        r = c.get(path)
        check("...but the APK downloads anyway, because it has to", r.status_code == 200)
        check("...as an APK, so a browser used to test the URL offers to save it",
              r.headers.get("Content-Type") == apkhost.CONTENT_TYPE)
        check("...and it is the bytes that were uploaded", r.get_data() == apk)

        r = c.get("/provisioning/apk/" + "b" * 43 + "/fleethub-agent.apk")
        check("a well-formed token that was never issued answers 404", r.status_code == 404)
        unissued = r.get_json()
        r = c.get("/provisioning/apk/not-a-token/fleethub-agent.apk")
        check("...and so does a malformed one", r.status_code == 404)
        # Identical bodies on purpose: the route must not become a way to ask whether this hub
        # is hosting anything at all.
        check("...with the same body, so the route says nothing about what is hosted",
              r.get_json() == unissued)

        print("\n== A row that outlives its blob says so ==")
        # What a database restored without its state directory looks like. 404 would send
        # somebody looking for a wrong token; this is a different problem and gets a different
        # answer.
        os.remove(apkhost.blob_path(apkhost.blob_root(state_dir), hosted["sha256"]))
        r = c.get(path)
        check("a missing file answers 410 rather than 404", r.status_code == 410)
        check("...and says the APK has to be uploaded again",
              "upload" in (r.get_json().get("error") or "").lower())

        print("\n== Uploading and removing need manage_settings ==")
        CURRENT_USER = "tech@x.com"
        check("issue_commands cannot upload an APK",
              c.post("/api/provisioning/apk",
                     data={"file": (io.BytesIO(apk), "a.apk")},
                     content_type="multipart/form-data").status_code == 403)
        check("...nor remove one", c.delete("/api/provisioning/apk").status_code == 403)
        check("...nor read what is hosted",
              c.get("/api/provisioning/apk").status_code == 403)
        CURRENT_USER = "admin@x.com"
        check("manage_settings can remove it",
              c.delete("/api/provisioning/apk").status_code == 200)
        check("...and afterwards the token is dead", c.get(path).status_code == 404)
        check("...and no QR can be built at all",
              c.get("/api/provisioning/qr").status_code == 409)
        check("removing again is a 404, not a 500",
              c.delete("/api/provisioning/apk").status_code == 404)
        CURRENT_USER = "super@x.com"

        print("\n== The write surface is exactly the APK ==")
        # The QR and the converter still have none: what they render is derived, and a second
        # write path would be a second set of validation rules for one value.
        for method, route in (("post", "/api/provisioning/qr"),
                              ("delete", "/api/provisioning/qr"),
                              ("get", "/api/provisioning/checksum")):
            r = getattr(c, method)(route, json={})
            check(f"{method.upper()} {route} is not a route", r.status_code == 405)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        settings.invalidate()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
