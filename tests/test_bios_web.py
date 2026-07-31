"""HTTP-layer test for bios_web.py (roadmap #9), plus the heartbeat's `bios` ingest.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot --
same approach as test_remote_web / test_fleet_web.

Two things are worth stating about what is asserted here:

  * **Reading is `view`, forcing a re-read is `issue_commands`.** A viewer must be able to
    answer "is Wake-on-LAN on?" without an admin, and must not be able to make a machine do
    anything. Both halves are checked, in both directions.
  * **A malformed `bios` block must not fail a heartbeat.** A 500 there marks the machine
    offline fleet-wide, which is a far worse outcome than a stale Firmware tab.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import bios
import fleet
import permissions
import settings
from bios_web import create_bios_blueprint
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
        bios.init_bios_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        # A tech who may act on PC-01 only.
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PC-01"], members=["tech@x.com"])
        # A viewer scoped to PC-01 with no command rights at all.
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-01"], members=["viewer@x.com"])
        settings.invalidate()

        agent_id, token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        app.register_blueprint(create_bios_blueprint(db_path, fake_login_required, access))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        auth = {"Authorization": f"Bearer {agent_id}:{token}"}

        print("\n== A machine that has never reported ==")
        r = c.get("/api/bios/PC-01")
        check("GET -> 200", r.status_code == 200)
        check("support is null, not 'unsupported'", r.get_json()["support"] is None)

        print("\n== The heartbeat carries the inventory ==")
        r = c.post("/api/agent/heartbeat", json={
            "config_version": 0,
            "bios": {
                "support": "supported", "vendor": "Dell",
                "interface": r"root\dcim\sysman", "bios_version": "1.29.0",
                "password_set": False,
                "settings": [{"name": "WakeOnLan", "value": "LanOnly", "kind": "enum",
                              "possible_values": ["Disabled", "LanOnly"],
                              "read_only": False, "display_name": "Wake on LAN"}],
            },
        }, headers=auth)
        check("heartbeat -> 200", r.status_code == 200)
        body = c.get("/api/bios/PC-01").get_json()
        check("the report is readable through the console API",
              body["support"] == "supported" and body["vendor"] == "Dell")
        check("the attribute survived the round trip",
              body["settings"][0]["name"] == "WakeOnLan")
        check("password_set false is distinct from unknown", body["password_set"] is False)

        print("\n== A malformed report must never fail a heartbeat ==")
        for junk in ("not a dict", 7, {"support": "nonsense"}, {"settings": [1, 2, 3]}):
            r = c.post("/api/agent/heartbeat",
                       json={"config_version": 0, "bios": junk}, headers=auth)
            check(f"heartbeat with bios={junk!r} still 200", r.status_code == 200)
        # A heartbeat with no bios block at all must leave the last good report alone --
        # otherwise every 10 s heartbeat from an agent between scans would blank the tab.
        c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("a heartbeat without a bios block leaves the stored report intact",
              c.get("/api/bios/PC-01").get_json()["support"] in ("supported", "error"))

        print("\n== Re-read is a real command ==")
        r = c.post("/api/bios/PC-01/refresh", json={})
        check("refresh -> 202", r.status_code == 202)
        check("a refresh_bios_inventory command was queued",
              any(cmd["type"] == "refresh_bios_inventory"
                  for cmd in fleet.claim_commands(db_path, agent_id, "PC-01")))

        print("\n== Capability + scope ==")
        CURRENT_USER = "viewer@x.com"
        check("a viewer may READ the firmware inventory",
              c.get("/api/bios/PC-01").status_code == 200)
        check("a viewer may NOT force a re-read -> 403",
              c.post("/api/bios/PC-01/refresh", json={}).status_code == 403)

        CURRENT_USER = "tech@x.com"
        check("a scoped tech may re-read their own machine",
              c.post("/api/bios/PC-01/refresh", json={}).status_code == 202)
        check("out-of-scope read -> 403", c.get("/api/bios/PC-09").status_code == 403)
        check("out-of-scope re-read -> 403",
              c.post("/api/bios/PC-09/refresh", json={}).status_code == 403)

        CURRENT_USER = "nobody@x.com"
        check("a stranger cannot read the inventory",
              c.get("/api/bios/PC-01").status_code == 403)
        CURRENT_USER = "super@x.com"

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
