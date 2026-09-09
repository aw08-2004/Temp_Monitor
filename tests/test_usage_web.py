"""HTTP-layer test for usage_web.py (roadmap #23 phase E), plus the heartbeat's `usage` ingest.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot -- same
approach as test_apps_web / test_location_web / test_capabilities_web.

**The silent failure this file exists to catch is a scope leak.** Foreground time per app is the
most personal thing this product stores: it is a record of what somebody did with their evening,
by the hour. Every route naming a machine therefore checks capability AND scope, and a reader
scoped to one phone must get a 403 on another rather than a payload -- an information leak here
is a privacy incident, not a listing somebody was not meant to see.

The second is the absence of a fleet-wide endpoint, asserted rather than assumed. The app
inventory has one because a policy author has to pick a package from somewhere. Usage has none
on purpose: the question it would answer at fleet scale is "who spends the most time on their
phone", and this product should not make that easy to ask. A future refactor that adds one for
symmetry would sail through every other test in the suite.

The third is that the window is capped at retention. There is never anything older to fetch, and
an endpoint that accepts `days=3650` invites a caller to believe there might be.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import settings
import usage
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from usage_web import create_usage_blueprint
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"
SECRET = "hub-enroll-secret"


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
        usage.init_usage_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(
            db_path, "Techs", capabilities=[permissions.VIEW],
            machines=["PHONE-1"], members=["tech@x.com"])
        settings.invalidate()

        app.register_blueprint(create_usage_blueprint(db_path, fake_login_required, access))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))
        agent_id, token = fleet.enroll_agent(db_path, "PHONE-1", SECRET, SECRET)
        phone_auth = {"Authorization": f"Bearer {agent_id}:{token}"}

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()

        print("== The heartbeat carries it ==")
        r = c.post("/api/agent/heartbeat", headers=phone_auth, json={
            "config_version": "",
            "usage": {"days": {"2026-09-08": {"com.zhiliaoapp.musically": 3600,
                                              "com.android.chrome": 600}}},
        })
        check("a heartbeat carrying usage is accepted", r.status_code == 200)
        check("...and it was stored",
              usage.day_totals(db_path, "PHONE-1", "2026-09-08")
              == {"com.zhiliaoapp.musically": 3600, "com.android.chrome": 600})

        print("\n== A malformed block never fails the heartbeat ==")
        for junk in ("not-an-object", {"days": None}, {"days": "x"}, [],
                     {"days": {"yesterday": {"com.a": 1}}}, {"days": {"2026-09-08": "x"}}):
            r = c.post("/api/agent/heartbeat", headers=phone_auth,
                       json={"config_version": "", "usage": junk})
            check(f"a malformed usage block does not fail the heartbeat: {junk!r}",
                  r.status_code == 200)
        check("...and none of it disturbed the ledger",
              usage.day_totals(db_path, "PHONE-1", "2026-09-08")
              == {"com.zhiliaoapp.musically": 3600, "com.android.chrome": 600})

        r = c.post("/api/agent/heartbeat", headers=phone_auth, json={"config_version": ""})
        check("a heartbeat with no usage block at all is fine", r.status_code == 200)
        check("...and does not erase the ledger",
              len(usage.days_for(db_path, "PHONE-1")) == 1)

        print("\n== Reading ==")
        r = c.get("/api/usage/machines/PHONE-1")
        body = r.get_json()
        check("a superuser can read a machine's usage", r.status_code == 200)
        check("...naming the machine it is about", body["machine"] == "PHONE-1")
        check("...with the retention window, so the console can say how long it is kept",
              body["retention_days"] == settings.get_int(db_path, "data.usage_retention_days"))
        check("...and the day the device reported", body["days"][0]["day"] == "2026-09-08")

        print("\n== Scope, which is the point of this file ==")
        CURRENT_USER = "tech@x.com"
        r = c.get("/api/usage/machines/PHONE-1")
        check("a scoped reader can read the phone they administer", r.status_code == 200)
        r = c.get("/api/usage/machines/PC-01")
        check("...and is refused one they do not, rather than shown an empty ledger",
              r.status_code == 403)

        CURRENT_USER = "nobody@x.com"
        r = c.get("/api/usage/machines/PHONE-1")
        check("somebody with no capability at all is refused", r.status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== The window is capped at retention ==")
        keep = settings.get_int(db_path, "data.usage_retention_days")
        r = c.get("/api/usage/machines/PHONE-1?days=3650")
        check("asking for ten years answers with the retention window",
              r.status_code == 200 and r.get_json()["retention_days"] == keep)
        r = c.get("/api/usage/machines/PHONE-1?days=not-a-number")
        check("a days parameter that is not a number is not a 500", r.status_code == 200)

        print("\n== No fleet-wide usage endpoint exists, deliberately ==")
        # Asserted rather than assumed: a later change that adds one for symmetry with the app
        # inventory would pass every other test in this suite.
        routes = {str(rule) for rule in app.url_map.iter_rules()}
        fleet_wide = {r for r in routes
                      if r.startswith("/api/usage") and "<machine>" not in r}
        check("there is no route that answers usage across machines", fleet_wide == set())

        print("\n== A device that never reported ==")
        r = c.get("/api/usage/machines/PC-01")
        body = r.get_json()
        check("reads as 'we have not been told', not as a device nobody used",
              r.status_code == 200 and body["reported_at"] is None and body["days"] == [])

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
