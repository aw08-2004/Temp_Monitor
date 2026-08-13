"""The agent-facing half of the live-telemetry watch: /api/agent/watch and the heartbeat's
`live_wanted` reply, which are what actually make a machine report every second.

Wires the fleet blueprint onto a minimal Flask app, avoiding app.py's OAuth boot -- same
approach as test_processes_web / test_wake_web / test_fleet_web. (The console's renewal
endpoint lives in app.py rather than a blueprint, so the watch is turned on here the same
way that endpoint turns it on: live.note_watch.)

What is worth stating about the assertions:

  * **A machine nobody has open must be told so.** This feature multiplies one machine's
    telemetry by twelve. Every path that can turn it on is asserted to answer "no" by
    default, because "on unless somebody remembers to turn it off" would be a fleet-wide
    bandwidth bill nobody ordered.

  * **Both watches ride one request.** The agent asks /api/agent/watch every couple of
    seconds; if the process list and the live charts needed separate calls, an idle fleet
    would make twice as many. The route is asserted to answer both.

  * **The old route still answers.** Every agent in the field polls
    /api/agent/processes/wanted and will keep doing so until it self-updates, so that route
    has to keep working exactly as it did.

  * **An agent learns about its own machine only.** The name comes from the bearer token,
    never the request, so one machine cannot discover that an operator is looking at another.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import live
import permissions
import processes
import settings
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0


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
        processes.init_processes_db(db_path)
        live.init_live_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        settings.invalidate()

        agent_id, agent_token = fleet.enroll_agent(db_path, "PC-1", SECRET, SECRET)
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": "super@x.com"}
        c = app.test_client()
        auth = {"Authorization": f"Bearer {agent_id}:{agent_token}"}

        print("\n== A machine nobody has open reports the way it always did ==")
        r = c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("heartbeat -> 200", r.status_code == 200)
        check("...and is told nobody is watching", r.get_json()["live_wanted"] is False)
        r = c.get("/api/agent/watch", headers=auth)
        check("the watch poll -> 200", r.status_code == 200)
        body = r.get_json()
        check("...and answers BOTH watches in one request",
              body["processes"] is False and body["live"] is False)
        check("...and carries the cadence, so the agent isn't guessing at it",
              body["live_interval_seconds"] == live.FAST_INTERVAL_SECONDS)
        r = c.get("/api/agent/watch")
        check("an unauthenticated caller is told nothing -> 401", r.status_code == 401)

        print("\n== Opening the machine page IS the subscription ==")
        live.note_watch(db_path, "PC-1", watcher="tech@x.com")
        r = c.get("/api/agent/watch", headers=auth)
        body = r.get_json()
        check("the machine's very next watch poll says report fast", body["live"] is True)
        check("...and the process list is still nobody's business",
              body["processes"] is False)
        r = c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("...as does its heartbeat, the only path an older agent has",
              r.get_json()["live_wanted"] is True)

        print("\n== The two watches are independent ==")
        processes.note_watch(db_path, "PC-1", watcher="tech@x.com")
        live.clear_watch(db_path, "PC-1")
        body = c.get("/api/agent/watch", headers=auth).get_json()
        check("somebody on the Processes card does not speed up telemetry",
              body["processes"] is True and body["live"] is False)

        print("\n== The route every agent in the field polls still answers ==")
        r = c.get("/api/agent/processes/wanted", headers=auth)
        check("the old process-only route -> 200", r.status_code == 200)
        check("...with the field it has always used", r.get_json()["wanted"] is True)

        print("\n== The watch lapses, and the machine slows back down ==")
        processes.clear_watch(db_path, "PC-1")
        live.note_watch(db_path, "PC-1", now=1000)          # expired long ago
        body = c.get("/api/agent/watch", headers=auth).get_json()
        check("a lapsed watch is not a watch",
              body["live"] is False and body["processes"] is False)
        r = c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("...and the heartbeat says stop too", r.get_json()["live_wanted"] is False)

        print("\n== An agent learns about its own machine and no other ==")
        live.note_watch(db_path, "PC-1", watcher="tech@x.com")
        other_id, other_token = fleet.enroll_agent(db_path, "PC-2", SECRET, SECRET)
        body = c.get("/api/agent/watch",
                     headers={"Authorization": f"Bearer {other_id}:{other_token}"}).get_json()
        check("PC-2's agent is not told that PC-1 is being watched", body["live"] is False)

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
