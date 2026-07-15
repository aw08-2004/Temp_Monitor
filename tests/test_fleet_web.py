"""HTTP-layer test for fleet_web.py using a minimal Flask app + test client.
Avoids app.py's Google-OAuth boot requirement by wiring the blueprint directly.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet
from fleet_web import create_fleet_blueprint
from flask import Flask, jsonify

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

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
    # Stand-in for app.py's session gate: always "logged in" as this operator.
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        priv = Ed25519PrivateKey.generate()
        pub_hex = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        ).hex()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(create_fleet_blueprint(db_path, SECRET, pub_hex, fake_login_required))
        # session.get("user") is read by issue-command; seed it.
        @app.before_request
        def _seed_session():
            from flask import session
            session["user"] = {"email": "operator@x.com"}
        c = app.test_client()

        print("\n== Enrollment endpoint ==")
        r = c.post("/api/agent/enroll", json={"machine": "PC-01", "enrollment_secret": SECRET})
        check("enroll 200", r.status_code == 200)
        agent_id = r.get_json()["agent_id"]
        token = r.get_json()["token"]
        check("enroll returns id+token", bool(agent_id) and bool(token))
        r = c.post("/api/agent/enroll", json={"machine": "PC-01", "enrollment_secret": "wrong"})
        check("wrong secret -> 403", r.status_code == 403)
        r = c.post("/api/agent/enroll", json={"enrollment_secret": SECRET})
        check("missing machine -> 400", r.status_code == 400)

        auth = {"Authorization": f"Bearer {agent_id}:{token}"}

        print("\n== Agent auth on protected endpoints ==")
        check("heartbeat without token -> 401", c.post("/api/agent/heartbeat").status_code == 401)
        check("heartbeat with bad token -> 401",
              c.post("/api/agent/heartbeat", headers={"Authorization": f"Bearer {agent_id}:nope"}).status_code == 401)
        check("heartbeat with good token -> 200",
              c.post("/api/agent/heartbeat", headers=auth).status_code == 200)

        print("\n== Console issues low-risk command, agent executes ==")
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "restart"})
        check("issue restart -> 201", r.status_code == 201)
        cid = r.get_json()["command_id"]
        r = c.get("/api/agent/commands", headers=auth)
        cmds = r.get_json()["commands"]
        check("agent pulls the restart command", len(cmds) == 1 and cmds[0]["id"] == cid)
        r = c.post(f"/api/agent/commands/{cid}/result", json={"success": True, "output": "ok"}, headers=auth)
        check("agent posts result -> 200", r.status_code == 200)
        r = c.get(f"/api/fleet/commands/{cid}")
        check("console sees command done", r.get_json()["status"] == fleet.STATUS_DONE)

        print("\n== High-risk command signature gate over HTTP ==")
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "run_script",
                                                "params": {"script": "echo hi"}})
        check("unsigned run_script -> 403", r.status_code == 403)
        sig = priv.sign(fleet.canonical_command_bytes("run_script", "PC-01", {"script": "echo hi"})).hex()
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "run_script",
                                                "params": {"script": "echo hi"}, "signature": sig})
        check("signed run_script -> 201", r.status_code == 201)
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "bogus"})
        check("unknown type -> 400", r.status_code == 400)

        print("\n== Status & result isolation ==")
        r = c.get("/api/fleet/status")
        check("status shows PC-01 online",
              any(s["machine"] == "PC-01" and s["status"] == "online" for s in r.get_json()))
        # A second agent must not close the first agent's freshly-claimed command.
        r = c.post("/api/agent/enroll", json={"machine": "PC-02", "enrollment_secret": SECRET})
        a2, t2 = r.get_json()["agent_id"], r.get_json()["token"]
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "shutdown"})
        cid2 = r.get_json()["command_id"]
        c.get("/api/agent/commands", headers=auth)  # PC-01 agent claims it
        r = c.post(f"/api/agent/commands/{cid2}/result", json={"success": True},
                   headers={"Authorization": f"Bearer {a2}:{t2}"})
        check("foreign agent completing -> 403", r.status_code == 403)

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
