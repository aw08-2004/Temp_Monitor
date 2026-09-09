"""HTTP-layer test for apps_web.py (roadmap #23 phase D), plus the heartbeat's `apps` ingest.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot -- same
approach as test_location_web / test_capabilities_web / test_wake_web.

**The silent failure this file exists to catch is the `is not None` gate.** The agent sends its
app inventory as an OBJECT (`{"apps": [...]}`) rather than as a bare array, and the hub tests
that key with `is not None` rather than for truthiness, precisely so a device reporting an EMPTY
list is still heard. That transition -- a device that has had everything uninstalled, or whose
policy suspended the lot -- is a real report, and a truthiness check anywhere along the path
makes it the one report that can never arrive. Nothing about that is visible from the outside:
the console shows the previous list, forever, with no error.

The second is scope. An app list is about the person carrying the device more than about the
device, so every route naming a machine checks capability AND scope, and the fleet-wide package
listing is scoped too -- it answers 200 either way, and the only difference is how much of
somebody else's fleet comes back in it.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import apps
import fleet
import permissions
import settings
from apps_web import create_apps_blueprint
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
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


def app_entry(package, **overrides):
    payload = {"package": package, "label": package.split(".")[-1].title(),
               "version": "1.0", "system": False, "enabled": True, "suspended": False}
    payload.update(overrides)
    return payload


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        apps.init_apps_db(db_path)
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

        app.register_blueprint(create_apps_blueprint(db_path, fake_login_required, access))
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
            "apps": {"apps": [app_entry("com.zhiliaoapp.musically", label="TikTok"),
                              app_entry("com.android.chrome", label="Chrome", system=True)]},
        })
        check("a heartbeat carrying an app inventory is accepted", r.status_code == 200)
        check("...and the packages were stored",
              len(apps.list_apps(db_path, "PHONE-1")) == 2)

        print("\n== An EMPTY list must survive the whole path ==")
        # The assertion this file exists for. A truthiness check anywhere between the agent and
        # the table makes this the one report that can never arrive.
        r = c.post("/api/agent/heartbeat", headers=phone_auth,
                   json={"config_version": "", "apps": {"apps": []}})
        check("a heartbeat reporting NO apps is accepted", r.status_code == 200)
        check("...and actually empties the inventory",
              apps.list_apps(db_path, "PHONE-1") == [])

        # ...while a malformed block leaves the last good reading alone, which is the other
        # half of the same decision.
        c.post("/api/agent/heartbeat", headers=phone_auth,
               json={"config_version": "",
                     "apps": {"apps": [app_entry("com.example.keep")]}})
        for junk in ("not-an-object", {"apps": None}, {"apps": "x"}, [], {"apps": [1, 2]}):
            r = c.post("/api/agent/heartbeat", headers=phone_auth,
                       json={"config_version": "", "apps": junk})
            check(f"a malformed app block does not fail the heartbeat: {junk!r}",
                  r.status_code == 200)
        check("...and none of it emptied the inventory",
              [a["package"] for a in apps.list_apps(db_path, "PHONE-1")]
              == ["com.example.keep"])

        r = c.post("/api/agent/heartbeat", headers=phone_auth, json={"config_version": ""})
        check("a heartbeat with no app block at all is fine", r.status_code == 200)
        check("...and does not erase what the device reported before",
              len(apps.list_apps(db_path, "PHONE-1")) == 1)

        print("\n== Reading ==")
        r = c.get("/api/apps/machines/PHONE-1")
        body = r.get_json()
        check("a superuser can read a machine's apps", r.status_code == 200)
        check("...with counts the console renders without recomputing",
              body["counts"]["total"] == 1 and body["counts"]["user"] == 1)
        check("...and the machine it is about", body["machine"] == "PHONE-1")

        r = c.get("/api/apps/machines/PC-01")
        body = r.get_json()
        check("a machine that never reported answers 200, not 404", r.status_code == 200)
        check("...as 'we have not been told' rather than 'no apps'",
              body["reported_at"] is None and body["apps"] == [])

        print("\n== Scope ==")
        apps.record_inventory(db_path, "PHONE-9",
                              {"apps": [app_entry("com.example.secret")]}, now=5000)
        CURRENT_USER = "tech@x.com"
        check("a scoped operator can read the machine in their scope",
              c.get("/api/apps/machines/PHONE-1").status_code == 200)
        check("...and is refused one outside it",
              c.get("/api/apps/machines/PHONE-9").status_code == 403)
        # The quiet one: this answers 200 either way, and the difference is only how much of
        # somebody else's fleet comes back in it.
        listed = c.get("/api/apps/packages").get_json()["packages"]
        check("the fleet package list is scoped to the caller's machines",
              [p["package"] for p in listed] == ["com.example.keep"])
        check("...and so is 'which machines have this package'",
              c.get("/api/apps/packages/com.example.secret/machines").get_json()["machines"]
              == [])
        CURRENT_USER = "super@x.com"
        listed = c.get("/api/apps/packages").get_json()["packages"]
        check("a superuser sees the whole fleet's packages",
              {p["package"] for p in listed} == {"com.example.keep", "com.example.secret"})
        check("...and the machine list behind one of them",
              c.get("/api/apps/packages/com.example.secret/machines").get_json()["machines"]
              == ["PHONE-9"])

        CURRENT_USER = "nobody@x.com"
        check("someone with no capability sees nothing",
              c.get("/api/apps/machines/PHONE-1").status_code == 403)
        check("...nor the package list",
              c.get("/api/apps/packages").status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== There is no write surface ==")
        # An inventory is what the DEVICE says is installed. A console-facing write would be a
        # way to tell the hub a device has an app it does not have -- which is exactly the
        # mismatch the suspended/enabled flags exist to make visible.
        for method, path in (("post", "/api/apps/machines/PHONE-1"),
                             ("put", "/api/apps/machines/PHONE-1"),
                             ("delete", "/api/apps/machines/PHONE-1"),
                             ("post", "/api/apps/packages")):
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
