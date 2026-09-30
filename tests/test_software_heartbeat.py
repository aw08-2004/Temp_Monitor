"""The heartbeat's `software` ingest (roadmap #25 B), and what it tells the agent when it fails.

Wires the fleet blueprint directly onto a minimal Flask app, the test_apps_web approach.

**The silent failure this file exists to catch is a software list the hub dropped while the
agent believed it delivered.** The Windows agent acknowledges its payload once the heartbeat
answers 200 and then suppresses it by content hash, so it is not sent again until something is
installed or removed. A write that failed here and said nothing would leave the device sheet and
the software catalog stale for as long as the PC stays unchanged -- weeks, on a settled machine
-- with one print line in the hub log as the only trace. The heartbeat must still answer 200
(a 500 reads the machine offline over a report that has nothing to do with being online), so
the failure travels as `software_rejected` in the reply, and the agent keeps the payload pending
when it sees it. Found in review.

The other half is that the flag appears ONLY on a failed write: a reply carrying it on success,
or on a heartbeat without a software block, would make every agent resend its whole inventory
on every heartbeat.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import settings
import software
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from flask import Flask

PASS = 0
FAIL = 0
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
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        software.init_software_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))
        agent_id, token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        auth = {"Authorization": f"Bearer {agent_id}:{token}"}
        c = app.test_client()
        block = {"software": [{"id": "HKLM64:Acro", "name": "Acrobat Reader",
                               "version": "2017"}], "error": ""}

        print("== A stored list is acknowledged ==")
        r = c.post("/api/agent/heartbeat", headers=auth,
                   json={"config_version": "", "software": block})
        check("a heartbeat carrying software is accepted", r.status_code == 200)
        check("...the list was stored",
              [s["name"] for s in software.get_inventory(db_path, "PC-01")["software"]]
              == ["Acrobat Reader"])
        check("...and the reply does not flag it", "software_rejected" not in r.get_json())

        r = c.post("/api/agent/heartbeat", headers=auth, json={"config_version": ""})
        check("a heartbeat without a software block is not flagged either",
              r.status_code == 200 and "software_rejected" not in r.get_json())

        print("\n== A failed write is still a 200, and says so ==")
        real = software.record_inventory

        def broken(*a, **k):
            raise RuntimeError("database is locked")
        software.record_inventory = broken
        try:
            r = c.post("/api/agent/heartbeat", headers=auth,
                       json={"config_version": "", "software": block})
        finally:
            software.record_inventory = real
        check("the heartbeat still succeeds -- the machine must not read offline",
              r.status_code == 200)
        check("...and the reply tells the agent to keep the payload",
              r.get_json().get("software_rejected") is True)
    finally:
        os.unlink(db_path)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
