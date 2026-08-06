"""HTTP-layer test for processes_web.py, plus the heartbeat's `processes` ingest and the
`processes_wanted` reply that turns a machine's sampling on and off.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot --
same approach as test_wake_web / test_bios_web / test_remote_web / test_fleet_web.

What is worth stating about the assertions here:

  * **Reading is `view`; ending and restarting are `issue_commands`.** There is deliberately
    no new capability -- what is running on a PC is inventory in the same sense its disks and
    its sensor tree are, and ending a process is strictly less dangerous than the `shutdown`
    that gate already covers. So the test that matters is that a VIEWER can see what is
    eating a machine's CPU and cannot end it.

  * **The console's READ is the subscription.** No machine samples its processes until
    somebody opens the card, so the assertion that a GET flips `processes_wanted` on the
    heartbeat is the assertion that this feature costs an unwatched fleet nothing.

  * **A malformed `processes` block must not fail a heartbeat.** A 500 there marks the
    machine offline fleet-wide, which is a far worse outcome than a stale card.

  * **There is exactly one door.** The (name, pid) pairing that survives PID reuse and the
    refusal of critical Windows processes both live in processes_web, so the generic command
    endpoint must turn these types away -- otherwise both guards are optional.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import processes
import settings
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from processes_web import create_processes_blueprint
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


def proc(pid, name, cpu=1.0, mem=100.0, **overrides):
    payload = {"pid": pid, "name": name, "cpu_pct": cpu, "mem_mb": mem,
               "user": "CORP\\alice", "session": 1, "path": f"C:\\apps\\{name}.exe"}
    payload.update(overrides)
    return payload


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        processes.init_processes_db(db_path)
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
            machines=["PC-1"], members=["tech@x.com"])
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-1"], members=["viewer@x.com"])
        settings.invalidate()

        agent_id, agent_token = fleet.enroll_agent(db_path, "PC-1", SECRET, SECRET)

        app.register_blueprint(create_processes_blueprint(db_path, fake_login_required, access))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        auth = {"Authorization": f"Bearer {agent_id}:{agent_token}"}

        print("\n== A machine nobody has looked at does no process work ==")
        r = c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("heartbeat -> 200", r.status_code == 200)
        check("...and is told nobody is watching, so it samples nothing",
              r.get_json()["processes_wanted"] is False)

        print("\n== Opening the card IS the subscription ==")
        r = c.get("/api/machines/PC-1/processes")
        check("GET -> 200", r.status_code == 200)
        body = r.get_json()
        check("a machine that has not reported yet says so rather than erroring",
              body["reported_at"] is None and body["processes"] == [])
        check("the cadence is served, not hardcoded in the browser",
              body["poll_interval"] == processes.POLL_INTERVAL_SECONDS
              and body["watch_ttl"] == processes.WATCH_TTL_SECONDS)
        r = c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("...and the machine's next heartbeat is told to start sampling",
              r.get_json()["processes_wanted"] is True)

        print("\n== The heartbeat carries the process list ==")
        r = c.post("/api/agent/heartbeat", json={
            "config_version": 0,
            "processes": {
                "captured_at": 1754000000, "cpu_cores": 8, "mem_total_mb": 16384.0,
                "sample_ms": 5000, "truncated": 0,
                "processes": [proc(4812, "chrome", cpu=42.5),
                              proc(900, "svchost", services=["Spooler"])],
            },
        }, headers=auth)
        check("heartbeat -> 200", r.status_code == 200)
        body = c.get("/api/machines/PC-1/processes").get_json()
        check("the list is readable through the console API", len(body["processes"]) == 2)
        by_name = {p["name"]: p for p in body["processes"]}
        check("usage came with it", by_name["chrome"]["cpu_pct"] == 42.5)
        check("so did the services a process hosts",
              by_name["svchost"]["services"] == ["Spooler"])
        check("...and the hub, not the agent, decides what may not be ended",
              by_name["svchost"]["protected"] is True)
        check("a fresh report is not stale", body["stale"] is False)

        print("\n== A malformed report must never fail a heartbeat ==")
        for junk in ("not a dict", 7, {"processes": "nope"}, {"processes": [1, 2]},
                     {"processes": [{"pid": "x", "name": None}]}, {"truncated": "lots"}):
            r = c.post("/api/agent/heartbeat",
                       json={"config_version": 0, "processes": junk}, headers=auth)
            check(f"heartbeat with processes={str(junk)[:32]!r} still 200",
                  r.status_code == 200)
        check("...and the last good list survived every one of them",
              len(c.get("/api/machines/PC-1/processes").get_json()["processes"]) == 2)
        # A heartbeat with no process block must leave the last list alone -- otherwise the
        # ordinary 10s heartbeat between samples would blank the card twice a minute.
        c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("a heartbeat without a process block leaves the list intact",
              len(c.get("/api/machines/PC-1/processes").get_json()["processes"]) == 2)

        print("\n== Reading is `view`; ending is `issue_commands` ==")
        CURRENT_USER = "viewer@x.com"
        r = c.get("/api/machines/PC-1/processes")
        check("a viewer can see what is eating the machine", r.status_code == 200)
        r = c.post("/api/machines/PC-1/processes/kill",
                   json={"name": "chrome", "pids": [4812]})
        check("...and cannot end it", r.status_code == 403)
        r = c.post("/api/machines/PC-1/processes/restart",
                   json={"name": "chrome", "pid": 4812})
        check("...or restart it", r.status_code == 403)

        CURRENT_USER = "tech@x.com"
        r = c.post("/api/machines/PC-1/processes/kill",
                   json={"name": "chrome", "pids": [4812, 4907], "tree": True})
        check("a tech can end it -> 201", r.status_code == 201)
        command_id = r.get_json()["command_id"]
        command = fleet.get_command(db_path, command_id)
        check("a kill_process command was queued", command["type"] == "kill_process")
        check("...carrying BOTH the name and the pids, which is what survives PID reuse",
              command["params"] == {"name": "chrome", "pids": [4812, 4907], "tree": True})

        r = c.post("/api/machines/PC-1/processes/restart",
                   json={"name": "spoolsv.exe", "pid": 900})
        check("a tech can restart one -> 201", r.status_code == 201)
        check("...and restart names exactly one process",
              fleet.get_command(db_path, r.get_json()["command_id"])["params"]
              == {"name": "spoolsv.exe", "pid": 900})

        print("\n== Refusals happen before anything is queued ==")
        before = len(fleet.list_commands(db_path, "PC-1"))
        for payload, label in (
            ({"name": "lsass.exe", "pids": [700]}, "ending lsass"),
            ({"name": "svchost", "pids": [900]}, "ending a service host"),
            ({"pids": [4812]}, "a kill with no name"),
            ({"name": "chrome"}, "a kill with no pid"),
            ({"name": "chrome", "pids": [0]}, "a kill aimed at pid 0"),
        ):
            r = c.post("/api/machines/PC-1/processes/kill", json=payload)
            check(f"{label} -> 400", r.status_code == 400)
        check("...and none of them queued anything",
              len(fleet.list_commands(db_path, "PC-1")) == before)

        print("\n== Out of scope is out of reach, for reads as well as writes ==")
        r = c.get("/api/machines/PC-OTHER/processes")
        check("reading a machine outside the operator's scope -> 403", r.status_code == 403)
        r = c.post("/api/machines/PC-OTHER/processes/kill",
                   json={"name": "chrome", "pids": [1]})
        check("...and so is ending something on it", r.status_code == 403)
        check("...with nothing queued there either",
              not fleet.list_commands(db_path, "PC-OTHER"))

        print("\n== One door: the command channel refuses these types ==")
        for kind, params in (("kill_process", {"name": "chrome", "pids": [1]}),
                             ("restart_process", {"name": "chrome", "pid": 1})):
            r = c.post("/api/fleet/commands",
                       json={"machine": "PC-1", "type": kind, "params": params})
            check(f"a hand-rolled {kind} through /api/fleet/commands -> 400",
                  r.status_code == 400)
        for kind in ("kill_process", "restart_process"):
            r = c.post("/api/fleet/favorites",
                       json={"name": f"save {kind}", "type": kind,
                             "params": {"name": "chrome", "pids": [1], "pid": 1}})
            check(f"...and {kind} cannot be saved as a favorite either -- a pid is recycled "
                  f"within minutes", r.status_code == 400)

        print("\n== CSRF: state-changing routes require a JSON content type ==")
        for path in ("/api/machines/PC-1/processes/kill",
                     "/api/machines/PC-1/processes/restart"):
            r = c.post(path, data="name=chrome&pids=1",
                       content_type="application/x-www-form-urlencoded")
            check(f"form-encoded POST to {path} -> 415", r.status_code == 415)

        print("\n== The watch lapses, and the machine stops sampling ==")
        processes.clear_watch(db_path, "PC-1")
        r = c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("with nobody looking, the machine is told to stop",
              r.get_json()["processes_wanted"] is False)

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
