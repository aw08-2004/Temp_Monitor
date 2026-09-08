"""HTTP-layer test for policy_web.py (roadmap #23 phase D), plus the heartbeat's policy channel.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot -- same
approach as test_apps_web / test_location_web / test_capabilities_web.

**The silent failure this file exists to catch is a device that stays blocked.** The heartbeat
only sends a `device_policy` document when its version differs from the one the agent reports
holding, which is the right thing for a ten-second heartbeat and exactly one mistake away from a
phone whose camera never comes back: if "no policy applies" resolved to *no document* rather
than an EMPTY one, removing a machine from a policy would leave it enforcing what it had, with
the console showing no policy at all and nothing to explain it. That path is asserted here end
to end, through the real endpoint, both ways.

The second is the capability. Writing a policy is `manage_device_policy` -- reading one is
`view`, and reading how a machine complies is `view` plus scope. Somebody who can read must not
be able to author, and somebody scoped to three phones must not see the ninety a fleet-wide
policy covers.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import apps
import fleet
import permissions
import policy
import settings
from apps_web import create_apps_blueprint
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from policy_web import create_policy_blueprint
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
        policy.init_policy_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        # A reader who cannot author, and an author scoped to one of the two phones. Between
        # them they cover both halves of the gate.
        permissions.create_group(
            db_path, "Readers", capabilities=[permissions.VIEW],
            machines=["PHONE-1"], members=["reader@x.com"])
        permissions.create_group(
            db_path, "Authors",
            capabilities=[permissions.VIEW, permissions.MANAGE_DEVICE_POLICY],
            machines=["PHONE-1"], members=["author@x.com"])
        settings.invalidate()

        app.register_blueprint(create_policy_blueprint(db_path, fake_login_required, access))
        app.register_blueprint(create_apps_blueprint(db_path, fake_login_required, access))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))
        agent_id, token = fleet.enroll_agent(db_path, "PHONE-1", SECRET, SECRET)
        phone_auth = {"Authorization": f"Bearer {agent_id}:{token}"}

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()

        apps.record_inventory(db_path, "PHONE-1", {"apps": [
            app_entry("com.zhiliaoapp.musically", label="TikTok"),
            app_entry("com.example.keep", label="Keep"),
            app_entry("com.android.settings", label="Settings", system=True)]}, now=1000)
        apps.record_inventory(db_path, "PHONE-2",
                              {"apps": [app_entry("com.zhiliaoapp.musically")]}, now=1000)

        print("== Writing needs its own capability ==")
        CURRENT_USER = "reader@x.com"
        body = {"name": "social", "mode": "block",
                "packages": ["com.zhiliaoapp.musically"], "machines": ["PHONE-1"]}
        check("a reader cannot create a policy",
              c.post("/api/policy/apps", json=body).status_code == 403)
        check("...nor preview one, which is a fleet-wide package query",
              c.post("/api/policy/preview", json=body).status_code == 403)
        check("...but can read the list", c.get("/api/policy/apps").status_code == 200)
        check("...and is told they may not write",
              c.get("/api/policy/apps").get_json()["can_manage"] is False)

        print("\n== Preview before save ==")
        CURRENT_USER = "author@x.com"
        r = c.post("/api/policy/preview", json=body)
        preview = r.get_json()
        check("an author can preview", r.status_code == 200)
        check("...and it answers per machine, not as a fleet total",
              [m["machine"] for m in preview["machines"]] == ["PHONE-1"])
        check("...naming only what is actually installed there",
              preview["machines"][0]["would_suspend"] == ["com.zhiliaoapp.musically"])
        check("...with the label, so it is checkable against what was meant",
              preview["machines"][0]["labels"]["com.zhiliaoapp.musically"] == "TikTok")

        protected = dict(body, packages=["com.zhiliaoapp.musically", "com.android.settings"])
        preview = c.post("/api/policy/preview", json=protected).get_json()
        check("a protected package is NAMED as dropped, never silently removed",
              preview["dropped_protected"] == ["com.android.settings"])
        check("...and does not appear in what would be suspended",
              "com.android.settings" not in preview["machines"][0]["would_suspend"])

        r = c.post("/api/policy/preview", json=dict(body, packages=[]))
        check("a blocklist with no packages is refused with a reason", r.status_code == 400)
        check("...that says it would do nothing",
              "do nothing" in (r.get_json() or {}).get("error", ""))

        print("\n== Saving ==")
        r = c.post("/api/policy/apps", json=body)
        check("an author can create a policy", r.status_code == 201)
        created = r.get_json()
        check("...and it comes back with its packages", created["packages"] ==
              ["com.zhiliaoapp.musically"])
        rows = fleet.list_audit(db_path, action="app_policy_create", limit=5)["entries"]
        check("...audited at security level, naming who wrote it",
              len(rows) == 1 and rows[0]["level"] == fleet.LEVEL_SECURITY
              and rows[0]["actor"] == "author@x.com")

        print("\n== Scope on the policy itself ==")
        CURRENT_USER = "super@x.com"
        fleet_wide = c.post("/api/policy/apps", json={
            "name": "fleet", "mode": "block", "packages": ["com.example.keep"],
            "fleet_wide": True}).get_json()
        c.put(f"/api/policy/apps/{created['id']}",
              json=dict(body, machines=["PHONE-1", "PHONE-2"]))
        CURRENT_USER = "author@x.com"
        listed = {p["name"]: p for p in c.get("/api/policy/apps").get_json()["policies"]}
        check("a scoped author sees only their own machines in a policy's targets",
              listed["social"]["machines"] == ["PHONE-1"])
        # The quiet one: without this the console would show three of ninety and read as
        # though that were all of them.
        check("...and is told how many they cannot see",
              listed["social"]["machines_hidden"] == 1)

        print("\n== Compliance ==")
        CURRENT_USER = "reader@x.com"
        r = c.get("/api/policy/machines/PHONE-1")
        report = r.get_json()
        check("a reader in scope can see how a machine is complying", r.status_code == 200)
        check("...naming what is blocked there",
              set(report["blocked"]) == {"com.zhiliaoapp.musically", "com.example.keep"})
        check("...and that the device has not caught up yet",
              report["current"] is False and report["counts"]["enforced"] == 0)
        check("reading a machine outside scope is refused",
              c.get("/api/policy/machines/PHONE-2").status_code == 403)

        print("\n== The heartbeat carries the document ==")
        CURRENT_USER = "super@x.com"
        r = c.post("/api/agent/heartbeat", headers=phone_auth,
                   json={"config_version": "", "policy_version": ""})
        reply = r.get_json()
        check("a device holding no version is sent the current document",
              r.status_code == 200 and "device_policy" in reply)
        check("...as a flat list of packages, never a rule engine",
              set(reply["device_policy"]["blocked"])
              == {"com.zhiliaoapp.musically", "com.example.keep"})
        check("...carrying the dead-man switch, so the device can lift it on its own",
              reply["device_policy"]["max_age_seconds"] == 604800)
        version = reply["device_policy_version"]

        r = c.post("/api/agent/heartbeat", headers=phone_auth,
                   json={"config_version": "", "policy_version": version})
        check("a device already holding that version is sent nothing",
              "device_policy" not in r.get_json())

        print("\n== ...and an EMPTY document when nothing applies ==")
        # The assertion this file exists for. If "no policy" were an ABSENT block rather than
        # an empty one, this device would go on enforcing what it had -- with the console
        # showing no policy and nothing anywhere to explain the app that will not open.
        for row in policy.list_policies(db_path):
            c.delete(f"/api/policy/apps/{row['id']}")
        r = c.post("/api/agent/heartbeat", headers=phone_auth,
                   json={"config_version": "", "policy_version": version})
        reply = r.get_json()
        check("removing every policy sends a new document, not silence",
              "device_policy" in reply)
        check("...whose blocked list is EMPTY, which is what lifts the restrictions",
              reply["device_policy"]["blocked"] == [])
        check("...under a version of its own, so the agent can tell it apart",
              reply["device_policy_version"] != version)

        print("\n== What the device says it did ==")
        r = c.post("/api/agent/heartbeat", headers=phone_auth, json={
            "config_version": "",
            "policy_state": {"version": "abc", "applied_at": 5000,
                             "failed": ["com.zhiliaoapp.musically"], "error": ""}})
        check("a policy state report is accepted", r.status_code == 200)
        state = policy.get_state(db_path, "PHONE-1")
        check("...and the packages it could NOT suspend are kept",
              state["failed"] == ["com.zhiliaoapp.musically"])
        for junk in ("x", [], {"failed": "com.x"}, {"applied_at": "soon"}):
            r = c.post("/api/agent/heartbeat", headers=phone_auth,
                       json={"config_version": "", "policy_state": junk})
            check(f"a malformed state report does not fail the heartbeat: {junk!r}",
                  r.status_code == 200)

        print("\n== Deleting ==")
        CURRENT_USER = "reader@x.com"
        made = policy.create_policy(db_path, name="temp", mode="block",
                                    packages=["com.x"], fleet_wide=True)
        check("a reader cannot delete a policy",
              c.delete(f"/api/policy/apps/{made}").status_code == 403)
        CURRENT_USER = "author@x.com"
        check("an author can", c.delete(f"/api/policy/apps/{made}").status_code == 200)
        check("...and deleting an unknown one is a 404, not a 500",
              c.delete("/api/policy/apps/nope").status_code == 404)

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
