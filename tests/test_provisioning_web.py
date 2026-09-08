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
"""
import functools
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))
import fleet
import i18n
import permissions
import provisioning
import settings
from permissions_web import create_access
from provisioning_web import create_provisioning_blueprint
from flask import Blueprint, Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"

SECRET = "fleet-enrollment-secret"
CHECKSUM = "s5oWEFPq3ECQxStcWufVzr1o2G5wngEgQ1R7NRKF9Gg"
APK_URL = "https://fleet.example.com/fleethub-agent.apk"


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

        app.register_blueprint(create_provisioning_blueprint(
            db_path, fake_login_required, access,
            hub_url="https://fleet.example.com", enrollment_secret=SECRET))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}

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
              "checksum" in (body.get("error") or "").lower()
              and "apksigner" in (body.get("error") or ""))
        check("...and carries no payload at all",
              "text" not in body and "payload" not in body)

        settings.set_many(db_path, {"provisioning.signature_checksum": CHECKSUM,
                                    "provisioning.apk_url": APK_URL},
                          updated_by="admin@x.com")
        settings.invalidate()

        print("\n== Configured ==")
        r = c.get("/api/provisioning/qr")
        body = r.get_json()
        check("it answers 200", r.status_code == 200)
        check("...with the exact string to encode", bool(body.get("text")))
        check("...which parses as the provisioning JSON",
              json.loads(body["text"])[provisioning.EXTRA_SIGNATURE_CHECKSUM] == CHECKSUM)
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

        print("\n== There is no write surface ==")
        # The two settings behind this are written through the ordinary settings API, by the
        # same capability, with the same validation. A second write path would be a second set
        # of rules for one value.
        for method, path in (("post", "/api/provisioning/qr"),
                             ("delete", "/api/provisioning/qr"),
                             ("get", "/api/provisioning/checksum")):
            r = getattr(c, method)(path, json={})
            check(f"{method.upper()} {path} is not a route", r.status_code == 405)

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
