"""The security posture routes and heartbeat ingest (roadmap #25 D).

Wires the fleet and posture blueprints onto a minimal Flask app, the test_bitlocker_web
approach.

**The silent failure this file exists to catch is a posture the hub dropped while the agent
believed it delivered.** The agent acknowledges its payload once the hub says it stored it
and then suppresses it by content hash, so a write that failed here and said nothing would
leave the posture card stale until something on the PC changed -- possibly a firewall that
was switched back on, still shown as off, for weeks. The heartbeat must still answer 200 (a
500 reads the machine offline), so the failure travels as `posture_rejected`, and the flag
must appear ONLY on a failed write or every agent would resend on every heartbeat.

Also pinned: both gates. The per-machine route is `view` + scope and refuses a machine
outside it; the fleet summary counts only the caller's machines, including in the list of
failing machine names it returns.
"""
import functools
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import bitlocker
import fleet
import permissions
import posture
import settings
from flask import Flask, session as flask_session
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from posture_web import create_posture_blueprint

PASS = 0
FAIL = 0
SECRET = "hub-enroll-secret"
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


FIREWALL_OFF = {"firewall": {"profiles": [{"name": "domain", "enabled": True},
                                          {"name": "private", "enabled": True},
                                          {"name": "public", "enabled": False}], "error": ""}}


def main():
    global CURRENT_USER
    # A directory rather than a bare temp file, removed tolerantly: on Windows the Flask app
    # under test keeps its own connection to the file until the process exits, so a plain
    # unlink fails with WinError 32 after every check has already passed.
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "fleet.db")
    try:
        for init in (fleet.init_fleet_db, bitlocker.init_bitlocker_db, posture.init_posture_db,
                     permissions.init_permissions_db, settings.init_settings_db):
            init(db_path)
        settings.invalidate()
        with fleet.get_conn(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS machine_info (machine TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO machine_info(machine) VALUES (?)",
                             [("PC-01",), ("PC-02",), ("PC-03",)])

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(db_path, "Techs", capabilities=[permissions.VIEW],
                                 machines=["PC-01"], members=["tech@x.com"])
        app.register_blueprint(create_fleet_blueprint(db_path, SECRET, fake_login_required, access))
        app.register_blueprint(create_posture_blueprint(db_path, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}

        c = app.test_client()
        auth = {}
        for name in ("PC-01", "PC-02"):
            agent_id, token = fleet.enroll_agent(db_path, name, SECRET, SECRET)
            auth[name] = {"Authorization": f"Bearer {agent_id}:{token}"}

        print("== Never reported ==")
        r = c.get("/api/posture/machines/PC-01")
        check("200 with no checks -- the card stays hidden rather than empty",
              r.status_code == 200 and r.get_json()["checks"] is None)

        print("\n== The heartbeat stores a posture ==")
        r = c.post("/api/agent/heartbeat", headers=auth["PC-01"],
                   json={"config_version": "", "posture": FIREWALL_OFF})
        check("accepted", r.status_code == 200)
        check("...and not flagged", "posture_rejected" not in r.get_json())
        data = c.get("/api/posture/machines/PC-01").get_json()
        checks = {ch["id"]: ch for ch in data["checks"]}
        check("the firewall check fails on the public profile",
              checks["firewall"]["status"] == "fail"
              and checks["firewall"]["params"]["profiles"] == ["public"])
        check("counts add up to every check", sum(data["counts"].values()) == len(posture.CHECKS))
        r = c.post("/api/agent/heartbeat", headers=auth["PC-01"], json={"config_version": ""})
        check("a heartbeat without a posture is not flagged either",
              "posture_rejected" not in r.get_json())

        print("\n== A failed write is still a 200, and says so ==")
        real = posture.record_posture

        def broken(*a, **k):
            raise RuntimeError("database is locked")
        posture.record_posture = broken
        try:
            r = c.post("/api/agent/heartbeat", headers=auth["PC-02"],
                       json={"config_version": "", "posture": FIREWALL_OFF})
        finally:
            posture.record_posture = real
        check("the heartbeat still succeeds", r.status_code == 200)
        check("...and tells the agent to keep the payload",
              r.get_json().get("posture_rejected") is True)
        c.post("/api/agent/heartbeat", headers=auth["PC-02"],
               json={"config_version": "", "posture": FIREWALL_OFF})

        print("\n== The fleet summary, unrestricted ==")
        summary = c.get("/api/posture/fleet").get_json()
        rows = {row["id"]: row for row in summary["checks"]}
        check("two reporting, one not yet",
              summary["reporting"] == 2 and summary["not_reported"] == 1)
        check("...both named as failing the firewall",
              sorted(rows["firewall"]["failing"]) == ["PC-01", "PC-02"])

        print("\n== Scope ==")
        CURRENT_USER = "tech@x.com"
        check("a machine inside scope is readable",
              c.get("/api/posture/machines/PC-01").status_code == 200)
        check("a machine outside scope is refused",
              c.get("/api/posture/machines/PC-02").status_code in (403, 404))
        summary = c.get("/api/posture/fleet").get_json()
        rows = {row["id"]: row for row in summary["checks"]}
        check("the summary counts only the operator's machines",
              summary["reporting"] == 1 and summary["not_reported"] == 0)
        check("...and never names one outside scope", rows["firewall"]["failing"] == ["PC-01"])
        CURRENT_USER = "nobody@x.com"
        check("an identity with no group gets nothing",
              c.get("/api/posture/fleet").status_code in (401, 403))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
