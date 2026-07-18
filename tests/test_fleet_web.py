"""HTTP-layer test for fleet_web.py using a minimal Flask app + test client.
Avoids app.py's Google-OAuth boot requirement by wiring the blueprint directly.
"""
import functools
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet
import settings
from fleet_web import create_fleet_blueprint
from flask import Flask, jsonify

PASS = 0
FAIL = 0

# Which operator the fake session gate reports. Mutable so a test can switch identity
# mid-run (the audit trail is the only accountability control now, so "which operator
# did the hub record?" needs to be assertable).
CURRENT_USER = "operator@x.com"


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
        # fleet_web reads fleet.offline_after_seconds / fleet.command_ttl_seconds from
        # here and passes them into fleet.py, which stays settings-free.
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(create_fleet_blueprint(db_path, SECRET, fake_login_required))
        # session.get("user") is read by issue-command; seed it.
        @app.before_request
        def _seed_session():
            from flask import session
            session["user"] = {"email": CURRENT_USER}
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

        print("\n== Console issues a command, agent executes ==")
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

        print("\n== Agent reports shell cwd on the result ==")
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "run_script",
                                                "params": {"script": "cd C:\\Windows"}})
        wcid = r.get_json()["command_id"]
        c.get("/api/agent/commands", headers=auth)   # claim it
        r = c.post(f"/api/agent/commands/{wcid}/result",
                   json={"success": True, "output": "", "cwd": "C:\\Windows"}, headers=auth)
        check("agent posts result with cwd -> 200", r.status_code == 200)
        body = c.get(f"/api/fleet/commands/{wcid}/output").get_json()
        check("cwd round-trips to the console for the prompt", body["result"]["cwd"] == "C:\\Windows")

        print("\n== run_script needs no signature over HTTP ==")
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "run_script",
                                                "params": {"script": "echo hi"}})
        check("run_script -> 201", r.status_code == 201)
        rcid = r.get_json()["command_id"]
        # A leftover 'signature' from an old client must not resurrect the gate or 400.
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "run_script",
                                                "params": {"script": "echo hi"},
                                                "signature": "deadbeef"})
        check("stray signature field ignored, not rejected", r.status_code == 201)
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "bogus"})
        check("unknown type -> 400", r.status_code == 400)
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "run_script",
                                                "params": "not-an-object"})
        check("non-object params -> 400", r.status_code == 400)

        print("\n== Audit attributes the command to the session, not the body ==")
        global CURRENT_USER
        CURRENT_USER = "someone.else@x.com"
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "run_script",
                                                "params": {"script": "whoami"},
                                                "issued_by": "spoofed@evil.example"})
        check("issue as second operator -> 201", r.status_code == 201)
        CURRENT_USER = "operator@x.com"
        with fleet.get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT actor, detail_json FROM audit_log WHERE action = 'issue_command' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        check("audit records the SESSION email", row["actor"] == "someone.else@x.com")
        check("body-supplied issued_by cannot spoof the actor",
              "spoofed@evil.example" not in row["actor"])
        check("audit captured the script text", "whoami" in (row["detail_json"] or ""))

        print("\n== CSRF: the JSON content-type requirement is the control ==")
        # With no signature gate, a CSRF on a signed-in operator would be fleet-wide RCE.
        # What blocks it is that these endpoints only read application/json bodies: that
        # type is not CORS-safelisted (so a cross-origin fetch preflights and fails), and
        # an HTML form -- the one cross-site POST needing no preflight -- cannot produce
        # it. Pin that here so nobody "helpfully" adds force=True or a form fallback.
        r = c.post("/api/fleet/commands",
                   data={"machine": "PC-01", "type": "restart", "params": "{}"},
                   headers={"Origin": "https://evil.example"})
        check("cross-site form-encoded POST rejected", r.status_code == 400)
        r = c.post("/api/fleet/commands",
                   data='{"machine":"PC-01","type":"restart","params":{}}',
                   content_type="text/plain",
                   headers={"Origin": "https://evil.example"})
        check("cross-site text/plain JSON smuggling rejected", r.status_code == 400)
        r = c.options("/api/fleet/commands", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        check("no CORS grant on the command route (preflight cannot succeed)",
              r.headers.get("Access-Control-Allow-Origin") is None)

        print("\n== Agent executes an unsigned run_script end to end ==")
        r = c.get("/api/agent/commands", headers=auth)
        claimed = r.get_json()["commands"]
        rc = [x for x in claimed if x["id"] == rcid]
        check("agent claims the run_script", len(rc) == 1)
        check("claim carries params, no signature fields",
              rc and rc[0]["params"] == {"script": "echo hi"}
              and "signature" not in rc[0] and "requires_signature" not in rc[0])
        r = c.post(f"/api/agent/commands/{rcid}/result",
                   json={"success": True, "output": "hi"}, headers=auth)
        check("agent reports run_script result -> 200", r.status_code == 200)
        check("console sees run_script done",
              c.get(f"/api/fleet/commands/{rcid}").get_json()["status"] == fleet.STATUS_DONE)

        print("\n== Live output streaming over HTTP ==")
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "run_script",
                                                "params": {"script": "loop"}})
        scid = r.get_json()["command_id"]
        c.get("/api/agent/commands", headers=auth)  # claim it

        r = c.post(f"/api/agent/commands/{scid}/output",
                   json={"seq": 0, "chunk": "step 1\n"}, headers=auth)
        check("agent posts output -> 200", r.status_code == 200)
        check("not truncated yet", r.get_json()["truncated"] is False)
        c.post(f"/api/agent/commands/{scid}/output",
               json={"seq": 1, "chunk": "step 2\n"}, headers=auth)

        r = c.get(f"/api/fleet/commands/{scid}/output")
        body = r.get_json()
        check("console reads chunks -> 200", r.status_code == 200)
        check("chunks in order", [x["text"] for x in body["chunks"]] == ["step 1\n", "step 2\n"])
        check("next_seq is the resume cursor", body["next_seq"] == 2)
        check("status bundled with output (one request per poll tick)",
              body["status"] == fleet.STATUS_CLAIMED)

        r = c.get(f"/api/fleet/commands/{scid}/output?after_seq=0")
        check("after_seq fetches only what's new",
              [x["seq"] for x in r.get_json()["chunks"]] == [1])
        r = c.get(f"/api/fleet/commands/{scid}/output?after_seq=notanumber")
        check("bad after_seq -> 400", r.status_code == 400)
        r = c.get("/api/fleet/commands/nope/output")
        check("output for unknown command -> 404", r.status_code == 404)

        r = c.post("/api/agent/enroll", json={"machine": "PC-09", "enrollment_secret": SECRET})
        other = r.get_json()
        r = c.post(f"/api/agent/commands/{scid}/output",
                   json={"seq": 5, "chunk": "evil"},
                   headers={"Authorization": f"Bearer {other['agent_id']}:{other['token']}"})
        check("foreign agent posting output -> 403", r.status_code == 403)
        r = c.post("/api/agent/commands/nope/output", json={"seq": 0, "chunk": "x"}, headers=auth)
        check("output for unknown command -> 404", r.status_code == 404)
        r = c.post(f"/api/agent/commands/{scid}/output",
                   json={"seq": 0, "chunk": "x" * (fleet.STREAM_MAX_CHUNK_CHARS + 1)},
                   headers=auth)
        check("oversized chunk -> 400", r.status_code == 400)
        r = c.post(f"/api/agent/commands/{scid}/output", json={"seq": "abc", "chunk": "x"}, headers=auth)
        check("non-integer seq -> 400", r.status_code == 400)
        check("output endpoint needs agent auth",
              c.post(f"/api/agent/commands/{scid}/output", json={"seq": 9, "chunk": "x"}).status_code == 401)

        c.post(f"/api/agent/commands/{scid}/result",
               json={"success": True, "output": "step 1\nstep 2\n"}, headers=auth)
        r = c.get(f"/api/fleet/commands/{scid}/output")
        body = r.get_json()
        check("result appears once complete", body["result"]["success"] == 1)
        check("scrollback survives completion", len(body["chunks"]) == 2)
        r = c.post(f"/api/agent/commands/{scid}/output",
                   json={"seq": 9, "chunk": "late"}, headers=auth)
        check("output after result -> 403 (run is over)", r.status_code == 403)

        print("\n== Old agent (3.0.1) compatibility ==")
        # A pre-3.1 agent never posts chunks -- it just returns the whole output at the
        # end. The console distinguishes the two by next_seq == 0, and renders
        # result.output as one block in that case. Guard the mixed-fleet window.
        r = c.post("/api/fleet/commands", json={"machine": "PC-01", "type": "gpupdate"})
        ocid = r.get_json()["command_id"]
        c.get("/api/agent/commands", headers=auth)
        c.post(f"/api/agent/commands/{ocid}/result",
               json={"success": True, "output": "Policy refreshed."}, headers=auth)
        body = c.get(f"/api/fleet/commands/{ocid}/output").get_json()
        check("no chunks from a non-streaming agent", body["chunks"] == [])
        check("next_seq stays 0 -> console falls back to result.output", body["next_seq"] == 0)
        check("status still reported", body["status"] == fleet.STATUS_DONE)
        check("full output still available via result",
              body["result"]["output"] == "Policy refreshed.")

        print("\n== Favorites API ==")
        # CURRENT_USER is already declared global above (audit attribution section).
        CURRENT_USER = "ann@x.com"
        r = c.post("/api/fleet/favorites", json={
            "name": "Ann private", "type": "run_script",
            "params": {"script": "echo ann"}, "shared": False})
        check("create favorite -> 201", r.status_code == 201)
        ann_fav = r.get_json()["favorite_id"]

        CURRENT_USER = "bob@x.com"
        r = c.post("/api/fleet/favorites", json={
            "name": "Team fix", "type": "run_script",
            "params": {"script": "Restart-Service Spooler"}, "shared": True})
        bob_shared = r.get_json()["favorite_id"]
        c.post("/api/fleet/favorites", json={"name": "Bob private", "type": "gpupdate",
                                             "params": {}, "shared": False})

        CURRENT_USER = "ann@x.com"
        ids = {f["id"] for f in c.get("/api/fleet/favorites").get_json()}
        check("GET returns own + shared only",
              ann_fav in ids and bob_shared in ids and len(ids) == 2)

        # The real escalation vector: ownership must come from the session, never the body.
        r = c.post("/api/fleet/favorites", json={
            "name": "spoof attempt", "type": "gpupdate", "params": {},
            "owner_email": "bob@x.com"})
        check("create with body owner_email -> 201", r.status_code == 201)
        spoofed = r.get_json()["favorite_id"]
        CURRENT_USER = "bob@x.com"
        bob_ids = {f["id"] for f in c.get("/api/fleet/favorites").get_json()}
        check("body-supplied owner_email is ignored; session wins",
              spoofed not in bob_ids)

        # Sharing grants read, not write.
        r = c.put(f"/api/fleet/favorites/{ann_fav}", json={"name": "hijacked"})
        check("non-owner PUT -> 403", r.status_code == 403)
        r = c.delete(f"/api/fleet/favorites/{ann_fav}")
        check("non-owner DELETE -> 403", r.status_code == 403)

        CURRENT_USER = "ann@x.com"
        r = c.put(f"/api/fleet/favorites/{ann_fav}", json={"name": "Ann renamed", "shared": True})
        check("owner PUT -> 200", r.status_code == 200)
        check("rename applied",
              any(f["name"] == "Ann renamed" for f in c.get("/api/fleet/favorites").get_json()))
        r = c.post("/api/fleet/favorites", json={"name": "Ann renamed", "type": "gpupdate",
                                                 "params": {}})
        check("duplicate name -> 400", r.status_code == 400)
        r = c.post("/api/fleet/favorites", json={"name": "bad", "type": "frobnicate",
                                                 "params": {}})
        check("unknown type -> 400", r.status_code == 400)
        r = c.put("/api/fleet/favorites/nope", json={"name": "x"})
        check("unknown favorite PUT -> 404", r.status_code == 404)
        r = c.delete("/api/fleet/favorites/nope")
        check("unknown favorite DELETE -> 404", r.status_code == 404)
        r = c.delete(f"/api/fleet/favorites/{ann_fav}")
        check("owner DELETE -> 200", r.status_code == 200)
        CURRENT_USER = "operator@x.com"

        print("\n== Heartbeat config channel ==")
        hdr = {"Authorization": f"Bearer {agent_id}:{token}"}
        # An agent that holds no version yet must be given the config.
        r = c.post("/api/agent/heartbeat", json={"config_version": ""}, headers=hdr)
        check("heartbeat 200", r.status_code == 200)
        body = r.get_json()
        check("stale agent is sent config", "config" in body)
        version = body.get("config_version")
        check("config carries a version", bool(version))
        check("config carries the sensor preference",
              "computer.primary_sensor_preference" in body["config"])
        # Only agent-flagged settings travel; hub internals must not leak to endpoints.
        check("hub-only knobs are not shipped",
              not any(k.startswith(("hub.", "data.", "fleet.")) for k in body["config"]))
        # Trust roots must never be settable from the hub.
        for forbidden in ("update_manifest_url", "update_public_key", "hub_base", "registry"):
            check(f"config carries no {forbidden}",
                  not any(forbidden in k.lower() for k in body["config"]))

        # An up-to-date agent gets a two-field response, so the 10s heartbeat stays cheap.
        r = c.post("/api/agent/heartbeat", json={"config_version": version}, headers=hdr)
        check("current agent is not re-sent config", "config" not in r.get_json())

        # Changing an agent-facing setting re-arms the push...
        settings.set_many(db_path, {"computer.primary_sensor_preference": ["core max"]})
        r = c.post("/api/agent/heartbeat", json={"config_version": version}, headers=hdr)
        check("a config change is pushed on the next heartbeat", "config" in r.get_json())
        check("pushed config reflects the change",
              r.get_json()["config"]["computer.primary_sensor_preference"] == ["core max"])
        new_version = r.get_json()["config_version"]
        check("the version changed", new_version != version)

        # ...but a hub-only setting must not churn the fleet.
        settings.set_many(db_path, {"data.retention_days": 45})
        r = c.post("/api/agent/heartbeat", json={"config_version": new_version}, headers=hdr)
        check("a hub-only change does not push to agents", "config" not in r.get_json())

        # Reverting hashes back to the original, so agents that never saw the
        # intermediate value don't re-apply anything.
        settings.reset(db_path, ["computer.primary_sensor_preference"])
        r = c.post("/api/agent/heartbeat", json={"config_version": version}, headers=hdr)
        check("reverting restores the original version (content hash, not a counter)",
              "config" not in r.get_json())

        # A heartbeat with no body at all must still work (older agents).
        r = c.post("/api/agent/heartbeat", headers=hdr)
        check("bodyless heartbeat still 200", r.status_code == 200)
        check("bodyless heartbeat is sent config", "config" in r.get_json())
        r = c.post("/api/agent/heartbeat", json={"config_version": "x"})
        check("heartbeat without a bearer token -> 401", r.status_code == 401)
        settings.reset(db_path, ["data.retention_days"])

        print("\n== Status & result isolation ==")
        r = c.get("/api/fleet/status")
        check("status shows PC-01 online",
              any(s["machine"] == "PC-01" and s["status"] == "online" for s in r.get_json()))

        # The configured offline window must actually reach fleet.list_agent_status --
        # backdate last_seen past a deliberately tiny window and the agent flips offline.
        with fleet.get_conn(db_path) as conn:
            conn.execute("UPDATE agents SET last_seen = ? WHERE machine = 'PC-01'",
                         (int(time.time()) - 60,))
        settings.set_many(db_path, {"fleet.offline_after_seconds": 30})
        r = c.get("/api/fleet/status")
        check("configured offline window is honoured",
              any(s["machine"] == "PC-01" and s["status"] == "offline" for s in r.get_json()))
        settings.set_many(db_path, {"fleet.offline_after_seconds": 3600})
        r = c.get("/api/fleet/status")
        check("widening the window brings it back online",
              any(s["machine"] == "PC-01" and s["status"] == "online" for s in r.get_json()))
        settings.reset(db_path, ["fleet.offline_after_seconds"])
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
