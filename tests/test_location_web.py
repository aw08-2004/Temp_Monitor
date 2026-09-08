"""HTTP-layer test for location_web.py (roadmap #23 phase B).

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot -- same
approach as test_capabilities_web / test_wake_web / test_bios_web.

**The silent failure this file exists to catch is `issue_commands` quietly becoming a tracking
permission.** Every other command-issuing feature in this product reuses that capability, and
the argument each time is that the action is less dangerous than the SYSTEM shell it already
grants. That argument holds because those actions are about a MACHINE. Locating is about a
person, so it has its own capability -- and a capability is only real if there is no other door.
There are two other doors and both are tested here:

  * `POST /api/fleet/commands` with `type: locate_device`, which is gated on `issue_commands`.
  * A saved favorite, which is replayed through that same endpoint.

Neither refusal is visible from the outside: a hub with either hole looks and behaves exactly
like this one until somebody notices a helpdesk technician has been locating phones.

The second thing asserted is scope. Location is one of the two payloads in this product where a
scope leak is a privacy incident rather than an information leak, so every route naming a
machine checks capability AND scope, and the fleet listing is filtered the same way -- it
answers 200 either way, and the only difference is how many devices come back.
"""
import functools
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))
import capabilities
import fleet
import i18n
import location
import permissions
import settings
from fleet_web import create_fleet_blueprint
from location_web import create_location_blueprint
from permissions_web import create_access
from flask import Blueprint, Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"
SECRET = "hub-enroll-secret"
LAT, LON = -22.3489, -60.0331


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
    both the allowed page and the refused one raise a BuildError, so the gate under test would
    read as a 500 either way. Same helper as test_mobile_nav.py."""
    for endpoint in ("index", "inventory_page", "alerts_page", "tools_page",
                     "remote_page", "settings_page", "permissions_page", "logout"):
        app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "", methods=["GET"])
    for name, endpoint in (("packages", "packages_page"), ("backups", "backups_page"),
                           ("invites", "invites_page"), ("users", "users_page"),
                           ("audit", "audit_page"), ("bios", "firmware_page"),
                           ("rules", "rules_page"), ("patches", "patches_page"),
                           ("apitokens", "download_page"), ("sharing", "sharing_page"),
                           ("provisioning", "provisioning_page")):
        bp = Blueprint(name, __name__)
        bp.add_url_rule(f"/_stub/{name}", endpoint, lambda: "", methods=["GET"])
        app.register_blueprint(bp)


def fix(**overrides):
    payload = {"lat": LAT, "lon": LON, "accuracy_m": 12.5, "provider": "gps",
               "fixed_at": 1_900_000_000, "stale": False}
    payload.update(overrides)
    return payload


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        location.init_location_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()

        app = Flask(__name__,
                    template_folder=os.path.join(ROOT, "hub", "templates"),
                    static_folder=os.path.join(ROOT, "hub", "static"))
        app.secret_key = "test"
        _register_sidebar_stubs(app)
        access = create_access(db_path, {"super@x.com"})
        # A technician with the fleet-wide command button and NOT the locate capability. This
        # group is the whole point of the file: it is what the helpdesk actually holds.
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PHONE-1", "PHONE-2"], members=["tech@x.com"])
        # Someone who may locate, scoped to ONE of the two phones.
        permissions.create_group(
            db_path, "Finders",
            capabilities=[permissions.VIEW, permissions.LOCATE_DEVICE],
            machines=["PHONE-1"], members=["finder@x.com"])
        settings.invalidate()

        app.register_blueprint(create_location_blueprint(db_path, fake_login_required, access))
        # The result hook wired the way app.py wires it, so the answer travels the real path:
        # the agent posts to /api/agent/commands/<id>/result, fleet_web commits the result and
        # THEN calls this. Calling location.handle_result directly would test the parse and
        # skip the plumbing, which is where a feature with no scheduler of its own breaks.
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access,
            on_command_result=lambda cid, m, ok, res, out=None: location.handle_result(
                db_path, cid, success=ok, result=res, output=out)))
        agent_id, agent_token = fleet.enroll_agent(db_path, "PHONE-1", SECRET, SECRET)
        phone_auth = {"Authorization": f"Bearer {agent_id}:{agent_token}"}

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}

        # Mirrors app.py's inject_nav_context, so base.html and denied.html render. Every
        # capability is granted to the SIDEBAR because nav rendering is not under test here;
        # the gate on the route reads the real groups above.
        @app.context_processor
        def _nav_context():
            context = {"cap": permissions, "hub_version": "test",
                       "user_capabilities": set(permissions.CAPABILITIES),
                       "open_alert_count": 0, "is_superuser": True,
                       "latest_agent_version": "8.8.8"}
            context.update(i18n.template_context("en"))
            return context

        c = app.test_client()

        print("== The capability is real, and there is no other door ==")
        CURRENT_USER = "tech@x.com"
        r = c.post("/api/location/machines/PHONE-1", json={})
        check("issue_commands alone cannot ask a device where it is", r.status_code == 403)
        # Door two: the generic command endpoint, which IS gated on issue_commands.
        r = c.post("/api/fleet/commands", json={"machine": "PHONE-1",
                                                "type": "locate_device", "params": {}})
        check("...nor through the generic command endpoint", r.status_code == 400)
        check("...which says where locating actually happens",
              "locate_device" in (r.get_json() or {}).get("error", ""))
        check("...and queued nothing", fleet.list_commands(db_path, "PHONE-1") == [])
        # Door three: a favorite is replayed through that same endpoint.
        try:
            fleet.create_favorite(db_path, "tech@x.com", "Find it", "locate_device", {})
            check("...nor by saving it as a favorite first", False)
        except ValueError as e:
            check("...nor by saving it as a favorite first", True)
            check("...with a reason naming the capability", "locate_device" in str(e))

        print("\n== Asking ==")
        CURRENT_USER = "finder@x.com"
        r = c.post("/api/location/machines/PHONE-1", json={})
        check("locate_device plus scope can ask", r.status_code == 202)
        body = r.get_json()
        check("...and gets the in-flight command back, not a position",
              body["pending"] is not None and body["latest"] is None)
        queued = fleet.list_commands(db_path, "PHONE-1")
        check("...having queued exactly one locate",
              len(queued) == 1 and queued[0]["type"] == "locate_device")
        # list_commands does not carry params, so this reads the full row -- and the params
        # are worth asserting: WHICH provider to ask is deliberately not in them, because the
        # device is the only thing that knows what it actually has.
        check("...carrying the device's time budget and nothing else",
              fleet.get_command(db_path, queued[0]["id"])["params"]
              == {"timeout_seconds": 45})
        check("...issued by the operator, from the session and not the body",
              queued[0]["issued_by"] == "finder@x.com")

        r = c.post("/api/location/machines/PHONE-1", json={})
        check("a second ask returns the first request rather than queueing another",
              r.status_code == 202 and len(fleet.list_commands(db_path, "PHONE-1")) == 1)

        check("...and asking outside scope is refused even with the capability",
              c.post("/api/location/machines/PHONE-2", json={}).status_code == 403)
        check("a form-encoded body is refused, which is what keeps this CSRF-proof",
              c.post("/api/location/machines/PHONE-1", data="x=1",
                     content_type="application/x-www-form-urlencoded").status_code == 415)

        print("\n== The audit trail names who asked ==")
        rows = fleet.list_audit(db_path, action="locate_device", limit=10)["entries"]
        check("a locate is audited under its own action", len(rows) == 1)
        check("...at security level", rows[0]["level"] == fleet.LEVEL_SECURITY)
        check("...naming the operator and the device",
              rows[0]["actor"] == "finder@x.com" and rows[0]["target"] == "PHONE-1")

        print("\n== The answer comes back, along the path it really travels ==")
        claimed = c.get("/api/agent/commands", headers=phone_auth).get_json()["commands"]
        check("the device claims the locate", len(claimed) == 1)
        check("...and is told who asked, so it can say so on screen",
              claimed[0]["issued_by"] == "finder@x.com")
        r = c.post(f"/api/agent/commands/{claimed[0]['id']}/result", headers=phone_auth,
                   json={"success": True, "output": json.dumps(fix())})
        check("...and posts its answer as any other command result", r.status_code == 200)

        body = c.get("/api/location/machines/PHONE-1").get_json()
        check("the fix reaches the machine payload", body["latest"]["lat"] == LAT)
        check("...with the negative longitude intact",
              body["latest"]["lon"] == LON and body["latest"]["lon"] < 0)
        check("...and the operator who asked for it, taken from the command",
              body["latest"]["requested_by"] == "finder@x.com")
        check("...and nothing is left in flight", body["pending"] is None)
        check("...and the history shows the one attempt", len(body["history"]) == 1)

        print("\n== Reading is `view`, not `locate_device` ==")
        CURRENT_USER = "tech@x.com"
        r = c.get("/api/location/machines/PHONE-1")
        check("a viewer in scope can see the last known position", r.status_code == 200)
        check("...and is told they may not ask for a new one",
              r.get_json()["can_locate"] is False)
        CURRENT_USER = "finder@x.com"
        check("...while somebody who may ask is told so",
              c.get("/api/location/machines/PHONE-1").get_json()["can_locate"] is True)
        check("reading outside scope is refused",
              c.get("/api/location/machines/PHONE-2").status_code == 403)
        CURRENT_USER = "nobody@x.com"
        check("someone with no capability at all sees nothing",
              c.get("/api/location/machines/PHONE-1").status_code == 403)

        print("\n== Whether the device can answer at all ==")
        CURRENT_USER = "super@x.com"
        check("a machine that has reported no capabilities is NOT shown as locatable",
              c.get("/api/location/machines/PHONE-1").get_json()["supported"] is False)
        capabilities.record_capabilities(db_path, "PHONE-1", {
            "platform": "android", "commands": ["rename", "locate_device"],
            "features": [capabilities.FEATURE_LOCATE]})
        check("...and one that reported the feature is",
              c.get("/api/location/machines/PHONE-1").get_json()["supported"] is True)
        # F.1's capability check, reached through this route: a device that says it cannot run
        # a locate is refused at creation rather than queued and left to expire.
        capabilities.record_capabilities(db_path, "PC-01", {
            "platform": "windows", "commands": ["restart", "run_script"], "features": []})
        r = c.post("/api/location/machines/PC-01", json={})
        check("a device that says it cannot locate is refused, not queued",
              r.status_code == 400)
        check("...with a reason naming the machine and the command",
              "PC-01" in (r.get_json() or {}).get("error", "")
              and "locate_device" in (r.get_json() or {}).get("error", ""))
        check("...and nothing was queued at it",
              fleet.list_commands(db_path, "PC-01") == [])

        print("\n== The fleet listing is scoped ==")
        location.record_fix(db_path, "PHONE-2", fix(lat=-25.3, lon=-57.6),
                            command_id="other", now=2000)
        listed = c.get("/api/location/fleet").get_json()["fixes"]
        check("a superuser sees every device with a fix",
              {f["machine"] for f in listed} == {"PHONE-1", "PHONE-2"})
        CURRENT_USER = "finder@x.com"
        listed = c.get("/api/location/fleet").get_json()["fixes"]
        # The quiet leak: this route answers 200 either way, and the only difference is how
        # many people's whereabouts come back in it.
        check("a scoped operator sees only their own machines",
              [f["machine"] for f in listed] == ["PHONE-1"])
        CURRENT_USER = "nobody@x.com"
        check("someone with no capability sees nothing at all",
              c.get("/api/location/fleet").status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== The map's tile source travels with the fixes ==")
        # Served here rather than read from /api/settings, and that is a GATE decision: the
        # settings API needs manage_settings, while a map needs only view. An operator who may
        # see where a device is has to be able to see it on something.
        config = c.get("/api/location/fleet").get_json()["map"]
        check("the fleet payload carries the tile configuration",
              config["tile_url"].startswith("https://")
              and config["attribution"] and config["zoom"] == 16)
        check("...and so does a machine's own payload",
              c.get("/api/location/machines/PHONE-1").get_json()["map"] == config)
        settings.set_many(db_path, {"map.tile_url": "", "map.tile_attribution": ""},
                          updated_by="admin@x.com")
        settings.invalidate()
        # Blank is a supported configuration, not a broken one: an air-gapped site sets it
        # deliberately and the map then draws points on an empty ground and says so. A default
        # substituted here would silently send that site's browsers to OpenStreetMap.
        check("a blank tile URL survives to the browser rather than being defaulted back",
              c.get("/api/location/fleet").get_json()["map"]["tile_url"] == "")
        settings.set_many(db_path,
                          {"map.tile_url": "https://tiles.example.com/{z}/{x}/{y}.png"},
                          updated_by="admin@x.com")
        settings.invalidate()
        check("...and a self-hosted one reaches it verbatim",
              c.get("/api/location/fleet").get_json()["map"]["tile_url"]
              == "https://tiles.example.com/{z}/{x}/{y}.png")

        print("\n== The map page ==")
        CURRENT_USER = "finder@x.com"
        check("an operator with view can open it",
              c.get("/map").status_code == 200)
        CURRENT_USER = "nobody@x.com"
        check("...and one with no capability cannot", c.get("/map").status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== There is no way to write a position ==")
        # A machine reports its own position through the command RESULT endpoint, with its own
        # bearer token. A console-facing write would be a way to put a device anywhere.
        for method, path in (("put", "/api/location/machines/PHONE-1"),
                             ("delete", "/api/location/machines/PHONE-1"),
                             ("post", "/api/location/fleet")):
            check(f"{method.upper()} {path} is not a route",
                  getattr(c, method)(path, json={}).status_code == 405)

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
