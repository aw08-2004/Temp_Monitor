"""HTTP-layer test for discovery_web.py (roadmap #18).

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot --
same approach as test_wake_web / test_files_web.

What is worth stating about the assertions here, because each is a way this feature would
be quietly wrong rather than visibly broken:

  * **Reading is `view`; sweeping is `issue_commands`.** No new capability, on the argument
    that an operator who can open a SYSTEM shell on that PC can already run `arp -a` on it.
    So what the test has to pin is that a VIEWER can read the last sweep and cannot start
    one.

  * **A sweep cannot be aimed off-segment through the HTTP layer either.** The refusal is in
    the model, and this is the test that it is actually reached rather than bypassed by a
    body the route forwards unchecked. An unaimable scanner is the whole reason this feature
    is safe to ship.

  * **One agent must not be able to answer another's scan.** The endpoint authenticates the
    machine; a report from the wrong one has to 404 and store nothing, or PC-3 could
    attribute a fabricated network to a site it has never been on.

  * **A refusal that arrives with no scan row is not a refusal an operator can read.** The
    scan is written before the command is queued, so a command that will not queue leaves a
    row carrying the reason.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import discovery
import fleet
import permissions
import settings
import wake
from discovery_web import create_discovery_blueprint
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"
ROSTER = []


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


def nic(mac, ipv4="10.4.7.31", prefix=24, kind="wired"):
    return {"mac": mac, "name": "Ethernet", "description": "Intel I219-LM",
            "ipv4": ipv4, "prefix": prefix, "kind": kind, "link_up": True,
            "wake_enabled": True}


def main():
    global CURRENT_USER, ROSTER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        wake.init_wake_db(db_path)
        discovery.init_discovery_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PC-01"], members=["tech@x.com"])
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-01"], members=["viewer@x.com"])
        settings.invalidate()

        pc1_id, pc1_token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        pc2_id, pc2_token = fleet.enroll_agent(db_path, "PC-02", SECRET, SECRET)

        app.register_blueprint(create_discovery_blueprint(
            db_path, fake_login_required, access, machine_roster=lambda: ROSTER))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        pc1_auth = {"Authorization": f"Bearer {pc1_id}:{pc1_token}"}
        pc2_auth = {"Authorization": f"Bearer {pc2_id}:{pc2_token}"}

        ROSTER = [{"machine": "PC-01", "online": True, "last_seen": 100000000000},
                  {"machine": "PC-02", "online": True, "last_seen": 100000000000}]

        print("\n== A machine that has never reported its adapters ==")
        r = c.get("/api/discovery/machines/PC-01")
        check("GET -> 200", r.status_code == 200)
        check("it has nothing it could sweep", r.get_json()["subnets"] == [])
        check("...and no scan to show", r.get_json()["scan"] is None)

        print("\n== The heartbeat's adapters are what fills the subnet picker ==")
        c.post("/api/agent/heartbeat", json={
            "config_version": 0,
            "network": {"nics": [nic("AA:BB:CC:DD:EE:01")]},
        }, headers=pc1_auth)
        body = c.get("/api/discovery/machines/PC-01").get_json()
        check("the machine's own subnet is offered", body["subnets"] == ["10.4.7.0/24"])

        print("\n== A sweep cannot be aimed off this machine's own segment ==")
        r = c.post("/api/discovery/machines/PC-01/scan", json={"subnet": "192.168.50.0/24"})
        check("an off-segment subnet is refused", r.status_code == 400)
        check("...naming the subnets that ARE possible",
              "10.4.7.0/24" in (r.get_json().get("error") or ""))
        check("...and no scan row was left behind",
              c.get("/api/discovery/machines/PC-01").get_json()["scan"] is None)

        print("\n== Sweeping ==")
        r = c.post("/api/discovery/machines/PC-01/scan", json={"subnet": "10.4.7.0/24"})
        check("sweep -> 202", r.status_code == 202)
        scan = r.get_json()["scan"]
        check("the response carries the scan, not just an ack", scan is not None)
        queued = fleet.claim_commands(db_path, pc1_id, "PC-01")
        check("a network_sweep command was queued at the machine itself",
              len(queued) == 1 and queued[0]["type"] == "network_sweep")
        check("...carrying the scan to report against and the subnet to look at",
              queued[0]["params"]["scan_id"] == scan["id"]
              and queued[0]["params"]["subnet"] == "10.4.7.0/24")

        print("\n== An agent may answer only for itself ==")
        r = c.post(f"/api/agent/discovery/scan/{scan['id']}",
                   json={"hosts": [{"ip": "10.4.7.77", "mac": "11:22:33:44:55:66"}]},
                   headers=pc2_auth)
        check("a report from the wrong machine -> 404", r.status_code == 404)
        check("...and stored nothing", discovery.scan_hosts(db_path, scan["id"]) == [])
        r = c.post(f"/api/agent/discovery/scan/{scan['id']}", json={"hosts": []})
        check("a report with no agent credentials -> 401", r.status_code == 401)

        r = c.post(f"/api/agent/discovery/scan/{scan['id']}", json={
            "probed": 254,
            "hosts": [{"ip": "10.4.7.31", "mac": "AA:BB:CC:DD:EE:01", "hostname": "pc-01"},
                      {"ip": "10.4.7.77", "mac": "11:22:33:44:55:66", "hostname": ""}],
        }, headers=pc1_auth)
        check("the owning machine's report -> 200", r.status_code == 200)

        body = c.get("/api/discovery/machines/PC-01").get_json()
        check("the scan is done", body["scan"]["status"] == "done")
        classes = {h["ip"]: h["classification"] for h in body["scan"]["hosts"]}
        check("the sweeping machine is not counted as a find",
              classes["10.4.7.31"] == "relay")
        check("an unrecognised MAC is the finding this card exists for",
              classes["10.4.7.77"] == "unmanaged")
        check("the counts the status pill reads are in the same answer",
              body["scan"]["counts"]["unmanaged"] == 1)

        print("\n== Reading is `view`; sweeping is `issue_commands` ==")
        CURRENT_USER = "viewer@x.com"
        check("a viewer can read the last sweep",
              c.get("/api/discovery/machines/PC-01").status_code == 200)
        check("...and cannot start one",
              c.post("/api/discovery/machines/PC-01/scan",
                     json={"subnet": "10.4.7.0/24"}).status_code == 403)

        CURRENT_USER = "tech@x.com"
        check("a tech in scope can read", c.get("/api/discovery/machines/PC-01").status_code == 200)
        check("...and is out of scope for a machine not in their groups",
              c.get("/api/discovery/machines/PC-02").status_code == 403)
        check("...and can read a scan by id on a machine that IS in scope",
              c.get(f"/api/discovery/scans/{scan['id']}").status_code == 200)

        CURRENT_USER = "super@x.com"
        r = c.get("/api/discovery/scans")
        check("the fleet-wide list answers 200", r.status_code == 200)
        check("...and carries this machine's scan", any(
            s["machine"] == "PC-01" for s in r.get_json()["scans"]))
        check("an unknown scan id is a 404, not an oracle",
              c.get("/api/discovery/scans/nope").status_code == 404)

        print("\n== A relay that could not sweep says so ==")
        r = c.post("/api/discovery/machines/PC-01/scan", json={"subnet": "10.4.7.0/24"})
        second = r.get_json()["scan"]
        r = c.post(f"/api/agent/discovery/scan/{second['id']}",
                   json={"error": "unknown command type: network_sweep"}, headers=pc1_auth)
        check("a reported failure -> 200", r.status_code == 200)
        body = c.get("/api/discovery/machines/PC-01").get_json()
        check("the scan is failed, not silently empty", body["scan"]["status"] == "failed")
        check("...carrying the agent's own sentence for the console to render verbatim",
              body["scan"]["error"] == "unknown command type: network_sweep")

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
