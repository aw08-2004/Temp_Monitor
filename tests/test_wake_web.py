"""HTTP-layer test for wake_web.py (roadmap #10), plus the heartbeat's `network` ingest.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot --
same approach as test_bios_web / test_remote_web / test_fleet_web.

What is worth stating about the assertions here:

  * **Reading is `view`; waking, preparing and cancelling are `issue_commands`.** There is
    deliberately no new capability -- waking a PC is strictly less dangerous than the
    `shutdown` that gate already covers -- so the test that matters is that a VIEWER can
    answer "why won't this PC wake?" and cannot make anything happen.
  * **A malformed `network` block must not fail a heartbeat.** A 500 there marks the machine
    offline fleet-wide, which is a far worse outcome than a stale Network tab.
  * **Named outcomes must survive the HTTP boundary as 202s, not errors.** `no_relay` and
    `unwakeable` are answers about the fleet, not faults in the call; returning 4xx for them
    would be read as "the console is broken" for what is actually "everything on that subnet
    is switched off".
  * **The relay is a DIFFERENT machine from the target**, and the scope check is on the
    target. That is the one thing about this feature that no other command type does, so it
    is asserted rather than assumed.
"""
import functools
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import settings
import wake
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from wake_web import create_wake_blueprint, DIAGNOSIS_CODES
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


def nic(mac, ipv4="10.4.7.31", prefix=24, kind="wired", **overrides):
    payload = {"mac": mac, "name": "Ethernet", "description": "Intel I219-LM",
               "ipv4": ipv4, "prefix": prefix, "kind": kind, "link_up": True,
               "wake_enabled": True}
    payload.update(overrides)
    return payload


def main():
    global CURRENT_USER, ROSTER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        wake.init_wake_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        # A tech scoped to the sleeping machine and its neighbour -- the relay has to be in
        # scope for the roster to offer it, but the GATE is on the target.
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PC-SLEEP", "PC-AWAKE"], members=["tech@x.com"])
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-SLEEP"], members=["viewer@x.com"])
        settings.invalidate()

        sleeper_id, sleeper_token = fleet.enroll_agent(db_path, "PC-SLEEP", SECRET, SECRET)
        peer_id, peer_token = fleet.enroll_agent(db_path, "PC-AWAKE", SECRET, SECRET)

        app.register_blueprint(create_wake_blueprint(
            db_path, fake_login_required, access, machine_roster=lambda: ROSTER))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        sleeper_auth = {"Authorization": f"Bearer {sleeper_id}:{sleeper_token}"}
        peer_auth = {"Authorization": f"Bearer {peer_id}:{peer_token}"}

        ROSTER = [{"machine": "PC-SLEEP", "online": False, "last_seen": 100},
                  {"machine": "PC-AWAKE", "online": True, "last_seen": 100000000000}]

        print("\n== A machine that has never reported its adapters ==")
        r = c.get("/api/wake/machines/PC-SLEEP")
        check("GET -> 200", r.status_code == 200)
        body = r.get_json()
        check("reported_at is null, not an empty adapter list dressed as a fact",
              body["reported_at"] is None)
        check("...and the diagnosis says exactly that and nothing else",
              body["diagnosis"] == ["no_report"])
        check("...so nothing offers to wake it", body["wakeable"] is False)

        print("\n== The heartbeat carries the network inventory ==")
        r = c.post("/api/agent/heartbeat", json={
            "config_version": 0,
            "network": {"fast_startup": False, "nics": [nic("AA:BB:CC:DD:EE:01")]},
        }, headers=sleeper_auth)
        check("heartbeat -> 200", r.status_code == 200)
        body = c.get("/api/wake/machines/PC-SLEEP").get_json()
        check("the adapter is readable through the console API",
              body["nics"][0]["mac"] == "AA:BB:CC:DD:EE:01")
        check("the subnet is derived for the console, not left to the browser",
              body["nics"][0]["subnet"] == "10.4.7.0/24")
        check("...and surfaced as the machine's wakeable subnet list",
              body["subnets"] == ["10.4.7.0/24"])
        check("a healthy machine has an empty diagnosis", body["diagnosis"] == [])

        print("\n== A malformed report must never fail a heartbeat ==")
        for junk in ("not a dict", 7, {"nics": "nope"}, {"nics": [1, 2]},
                     {"fast_startup": "sure"}):
            r = c.post("/api/agent/heartbeat",
                       json={"config_version": 0, "network": junk}, headers=sleeper_auth)
            check(f"heartbeat with network={junk!r} still 200", r.status_code == 200)
        # A heartbeat with no network block must leave the last good report alone --
        # otherwise every 10 s heartbeat between scans would blank the tab.
        c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=sleeper_auth)
        check("a heartbeat without a network block leaves the adapters intact",
              len(c.get("/api/wake/machines/PC-SLEEP").get_json()["nics"]) == 1)

        print("\n== Waking, and who is actually asked ==")
        c.post("/api/agent/heartbeat", json={
            "config_version": 0,
            "network": {"nics": [nic("AA:BB:CC:DD:EE:02", ipv4="10.4.7.50")]},
        }, headers=peer_auth)

        r = c.post("/api/wake/machines/PC-SLEEP", json={})
        check("wake -> 202", r.status_code == 202)
        body = r.get_json()
        check("the response carries the request, not just an ack",
              body["request"] is not None)
        # The whole feature in one assertion: the command goes to the PEER.
        peer_commands = fleet.claim_commands(db_path, peer_id, "PC-AWAKE")
        check("a wake_machine command was queued at the AWAKE PEER, not the sleeping machine",
              len(peer_commands) == 1 and peer_commands[0]["type"] == "wake_machine")
        check("...naming the sleeping machine as its target",
              peer_commands[0]["params"]["target"] == "PC-SLEEP")
        check("...and nothing at all was queued at the sleeping machine",
              not fleet.claim_commands(db_path, sleeper_id, "PC-SLEEP"))

        print("\n== 'Sent' is not success ==")
        fleet.complete_command(db_path, peer_commands[0]["id"], peer_id, True, "sent")
        body = c.get("/api/wake/machines/PC-SLEEP").get_json()
        # Reconciliation happens on the tick the GET triggers; either way the request must
        # NOT be terminal -- nothing acknowledges a magic packet.
        check("the request is still open after the relay reported the packet went out",
              body["request"] is not None and body["request"]["status"] in
              (wake.STATUS_RELAYING, wake.STATUS_SENT))

        print("\n== Named outcomes come back as answers, not errors ==")
        # Nobody awake anywhere: the honest answer is "still looking", with a 202.
        ROSTER = [{"machine": "PC-SLEEP", "online": False, "last_seen": 100}]
        wake.forget_machine(db_path, "PC-SLEEP")
        c.post("/api/agent/heartbeat", json={
            "config_version": 0, "network": {"nics": [nic("AA:BB:CC:DD:EE:01")]},
        }, headers=sleeper_auth)
        r = c.post("/api/wake/machines/PC-SLEEP", json={})
        check("with no peer awake, waking still answers 202", r.status_code == 202)
        check("...and reports it is still looking for a relay",
              r.get_json()["request"]["status"] == wake.STATUS_PENDING)

        # A Wi-Fi-only machine cannot be woken at all. Still 202: it is a fact about the
        # hardware, not a fault in the request.
        wifi_id, wifi_token = fleet.enroll_agent(db_path, "PC-SLEEP2", SECRET, SECRET)
        permissions.update_group(
            db_path, permissions.groups_for_email(db_path, "tech@x.com")[0]["id"],
            machines=["PC-SLEEP", "PC-AWAKE", "PC-SLEEP2"])
        settings.invalidate()
        c.post("/api/agent/heartbeat", json={
            "config_version": 0,
            "network": {"nics": [nic("AA:BB:CC:DD:EE:09", kind="wireless")]},
        }, headers={"Authorization": f"Bearer {wifi_id}:{wifi_token}"})
        ROSTER = ROSTER + [{"machine": "PC-SLEEP2", "online": False, "last_seen": 1}]
        r = c.post("/api/wake/machines/PC-SLEEP2", json={})
        check("a machine that cannot be woken answers 202, not 4xx", r.status_code == 202)
        body = r.get_json()
        # `request` carries the OPEN one, so a terminal outcome arrives as the newest row of
        # the history. That split is deliberate -- the console renders an in-flight wake
        # differently from a finished one -- and the console falls back to history[0] for
        # exactly this case.
        check("...and `request` is empty, because this outcome is already terminal",
              body["request"] is None)
        check("...with the outcome named as the newest history row",
              body["history"][0]["status"] == wake.STATUS_UNWAKEABLE)
        check("...and the diagnosis names Wi-Fi rather than 'misconfigured'",
              body["diagnosis"] == ["wireless_only"])

        print("\n== Every diagnosis code the model can emit has a declared name ==")
        # DIAGNOSIS_CODES is what tests/test_i18n.py's catalog coverage hangs off, so it
        # must not drift from what wake.diagnose actually produces.
        emitted = set()
        for machine in ("PC-SLEEP", "PC-SLEEP2", "NEVER-SEEN"):
            emitted.update(wake.diagnose(wake.get_network(db_path, machine)))
        check(f"the codes emitted here are all declared ({sorted(emitted)})",
              emitted <= set(DIAGNOSIS_CODES))

        print("\n== Cancel ==")
        pending = wake.open_request_for(db_path, "PC-SLEEP")
        r = c.post(f"/api/wake/requests/{pending['id']}/cancel", json={})
        check("cancelling a wake no relay has seen -> 200", r.status_code == 200)
        check("...and it is terminal",
              wake.get_request(db_path, pending["id"])["status"] == wake.STATUS_CANCELLED)
        check("an unknown request id -> 404",
              c.post("/api/wake/requests/nope/cancel", json={}).status_code == 404)

        print("\n== Fleet-wide ==")
        r = c.post("/api/wake/fleet", json={})
        check("fleet wake -> 202", r.status_code == 202)
        counts = r.get_json()["counts"]
        check("machines that cannot be woken are counted, not dropped",
              counts.get(wake.STATUS_UNWAKEABLE, 0) >= 1)
        check("...as are the ones a packet will actually be sent for",
              counts.get(wake.STATUS_PENDING, 0) >= 1)

        print("\n== Capability + scope ==")
        CURRENT_USER = "viewer@x.com"
        check("a viewer may READ the network inventory and the diagnosis",
              c.get("/api/wake/machines/PC-SLEEP").status_code == 200)
        check("a viewer may NOT wake -> 403",
              c.post("/api/wake/machines/PC-SLEEP", json={}).status_code == 403)
        check("a viewer may NOT fix the wake settings -> 403",
              c.post("/api/wake/machines/PC-SLEEP/prepare", json={}).status_code == 403)
        check("a viewer may NOT wake the fleet -> 403",
              c.post("/api/wake/fleet", json={}).status_code == 403)

        CURRENT_USER = "tech@x.com"
        check("out-of-scope read -> 403",
              c.get("/api/wake/machines/PC-ELSEWHERE").status_code == 403)
        check("out-of-scope wake -> 403",
              c.post("/api/wake/machines/PC-ELSEWHERE", json={}).status_code == 403)
        check("out-of-scope prepare -> 403",
              c.post("/api/wake/machines/PC-ELSEWHERE/prepare", json={}).status_code == 403)

        print("\n== wake_machine cannot be hand-rolled through the command channel ==")
        CURRENT_USER = "super@x.com"
        r = c.post("/api/fleet/commands", json={
            "machine": "PC-AWAKE", "type": "wake_machine",
            "params": {"macs": ["AA:BB:CC:DD:EE:FF"], "broadcast": "10.99.99.255"}})
        check("a hand-rolled relay command -> 400: the hub picks the relay, not the caller",
              r.status_code == 400)
        r = c.post("/api/fleet/commands", json={"machine": "PC-AWAKE", "type": "prepare_wake",
                                                "params": {}})
        check("prepare_wake IS issuable there -- it is favoritable, and a favorite runs "
              "through this endpoint", r.status_code == 201)

        print("\n== CSRF: state-changing routes require a JSON content type ==")
        for path in ("/api/wake/machines/PC-SLEEP", "/api/wake/machines/PC-SLEEP/prepare",
                     "/api/wake/fleet"):
            r = c.post(path, data="machines=all",
                       content_type="application/x-www-form-urlencoded")
            check(f"form-encoded POST to {path} -> 415", r.status_code == 415)

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
