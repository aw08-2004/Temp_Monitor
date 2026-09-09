"""HTTP-layer test for capabilities_web.py (roadmap #23), plus the heartbeat's `capabilities`
ingest and the refusal reaching an operator through the fleet API.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot -- same
approach as test_wake_web / test_bios_web / test_fleet_web.

**The silent failure this file exists to catch is a scope leak dressed as a lookup.** What a
machine can do is read behind `view` PLUS machine scope, and the fleet listing is filtered the
same way. An unscoped listing would not look like a leak -- it returns machine names and the
word "android" -- but it is a complete inventory of a fleet the caller was never shown, which
is exactly the thing permission groups exist to withhold. There is no write surface here at
all, and the test asserts that too: an operator override would be a way to tell the hub that a
phone can run a script.

The second thing asserted is the one the whole feature is for: a scheduled command that a
device cannot answer must be refused at the hub with a reason an operator can act on, and a
malformed capability block must not fail a heartbeat -- a 500 there marks the machine offline
fleet-wide, which is far worse than a stale capability row.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import capabilities
import fleet
import permissions
import settings
from capabilities_web import create_capabilities_blueprint
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"


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


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        capabilities.init_capabilities_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(
            db_path, "Phone techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PHONE-1"], members=["tech@x.com"])
        settings.invalidate()

        phone_id, phone_token = fleet.enroll_agent(db_path, "PHONE-1", SECRET, SECRET)
        fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)

        app.register_blueprint(create_capabilities_blueprint(
            db_path, fake_login_required, access))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        phone_auth = {"Authorization": f"Bearer {phone_id}:{phone_token}"}

        print("== The heartbeat carries it ==")
        r = c.post("/api/agent/heartbeat", headers=phone_auth, json={
            "config_version": "",
            "capabilities": {"platform": "android", "commands": ["rename"],
                             "features": ["locate"]},
        })
        check("a heartbeat carrying capabilities is accepted", r.status_code == 200)
        stored = capabilities.get_capabilities(db_path, "PHONE-1")
        check("...and the block was stored",
              stored["platform"] == "android" and stored["commands"] == ["rename"])
        check("...and the heartbeat's own answer is unchanged",
              r.get_json().get("status") == "ok")

        for junk in ("not-an-object", {"platform": {"x": 1}}, {"commands": 7}, []):
            r = c.post("/api/agent/heartbeat", headers=phone_auth,
                       json={"config_version": "", "capabilities": junk})
            check(f"a malformed capability block does not fail the heartbeat: {junk!r}",
                  r.status_code == 200)
        check("...and the last good report survived the junk",
              capabilities.get_capabilities(db_path, "PHONE-1")["commands"] == ["rename"])

        r = c.post("/api/agent/heartbeat", headers=phone_auth, json={"config_version": ""})
        check("a heartbeat with no capabilities block at all is still fine",
              r.status_code == 200)
        check("...and does not erase what the machine reported before",
              capabilities.get_capabilities(db_path, "PHONE-1")["commands"] == ["rename"])

        print("\n== Reading ==")
        r = c.get("/api/capabilities/machines/PHONE-1")
        body = r.get_json()
        check("a superuser can read a machine's capabilities", r.status_code == 200)
        check("...and it reports what the device said",
              body["platform"] == "android" and body["features"] == ["locate"])
        check("...and says plainly that it HAS reported", body["reported"] is True)

        r = c.get("/api/capabilities/machines/PC-01")
        body = r.get_json()
        check("a machine that never reported answers 200, not 404", r.status_code == 200)
        check("...as unknown rather than incapable",
              body["reported"] is False and body["commands"] is None
              and body["platform"] == "")

        print("\n== Scope ==")
        CURRENT_USER = "tech@x.com"
        check("a scoped operator can read the machine in their scope",
              c.get("/api/capabilities/machines/PHONE-1").status_code == 200)
        check("...and is refused one outside it",
              c.get("/api/capabilities/machines/PC-01").status_code == 403)
        # The listing is the one that would leak quietly: it answers 200 either way, and the
        # difference is only in how many machines come back.
        capabilities.record_capabilities(db_path, "PC-01", {"platform": "windows"})
        listed = c.get("/api/capabilities/platforms").get_json()
        check("the fleet listing is filtered to the caller's scope",
              listed["platforms"] == {"PHONE-1": "android"})
        check("...and names the platforms it knows, so the console need not hardcode them",
              "android" in listed["known"])
        CURRENT_USER = "super@x.com"
        listed = c.get("/api/capabilities/platforms").get_json()
        check("a superuser sees the whole fleet",
              listed["platforms"] == {"PHONE-1": "android", "PC-01": "windows"})

        CURRENT_USER = "nobody@x.com"
        check("someone with no capability at all cannot read a machine",
              c.get("/api/capabilities/machines/PHONE-1").status_code == 403)
        check("...nor the listing",
              c.get("/api/capabilities/platforms").status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== There is no write surface ==")
        for method, path in (("post", "/api/capabilities/machines/PHONE-1"),
                             ("put", "/api/capabilities/machines/PHONE-1"),
                             ("delete", "/api/capabilities/machines/PHONE-1"),
                             ("post", "/api/capabilities/platforms")):
            r = getattr(c, method)(path, json={"commands": ["run_script"]})
            check(f"{method.upper()} {path} is not a route", r.status_code == 405)

        print("\n== The refusal reaches an operator ==")
        r = c.post("/api/fleet/commands", json={"machine": "PHONE-1", "type": "run_script",
                                                "params": {"script": "whoami"}})
        check("issuing a command the device cannot run is refused, not queued",
              r.status_code == 400)
        told = (r.get_json() or {}).get("error") or ""
        check("...with a reason naming the command and the platform",
              "run_script" in told and "android" in told)
        check("...and nothing was queued", fleet.list_commands(db_path, "PHONE-1") == [])
        r = c.post("/api/fleet/commands", json={"machine": "PHONE-1", "type": "rename",
                                                "params": {"new_name": "PHONE-9"}})
        check("a command it CAN run still goes through", r.status_code == 201)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
