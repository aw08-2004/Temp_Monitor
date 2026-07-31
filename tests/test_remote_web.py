"""HTTP-layer test for remote_web.py using a minimal Flask app + test client (roadmap #2).
Wires the blueprint directly, avoiding app.py's OAuth boot -- same approach as test_fleet_web.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import remote
import settings
from remote_web import create_remote_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"   # a break-glass superuser unless a test switches identity


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
        remote.init_remote_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        # A scoped, non-superuser tech: remote_control over PC-01 only.
        permissions.create_group(
            db_path, "Techs", capabilities=[permissions.VIEW, permissions.REMOTE_CONTROL],
            machines=["PC-01"], members=["tech@x.com"])
        # A viewer with no remote_control at all.
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-01"], members=["viewer@x.com"])
        settings.invalidate()

        # Enroll two agents via the fleet blueprint's model directly.
        agent_id, token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        other_id, other_token = fleet.enroll_agent(db_path, "PC-09", SECRET, SECRET)

        env_fd, env_path = tempfile.mkstemp(suffix=".env")
        os.close(env_fd)
        with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("HUB_URL=https://h\n")
        app.register_blueprint(
            create_remote_blueprint(db_path, fake_login_required, access, env_path=env_path))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()

        auth = {"Authorization": f"Bearer {agent_id}:{token}"}
        other_auth = {"Authorization": f"Bearer {other_id}:{other_token}"}

        print("\n== Start a session (superuser) ==")
        r = c.post("/api/remote/PC-01/start", json={"monitor": 0})
        check("start -> 201", r.status_code == 201)
        body = r.get_json()
        sid = body["session_id"]
        check("start returns a session id + ice_servers",
              bool(sid) and isinstance(body["ice_servers"], list))
        check("a start_remote_session command was queued for the agent",
              any(cmd["type"] == "start_remote_session" and cmd["params"]["session_id"] == sid
                  for cmd in fleet.claim_commands(db_path, agent_id, "PC-01")))
        check("session is pending", remote.get_session(db_path, sid)["status"] == remote.STATUS_PENDING)

        print("\n== Signaling relay through the endpoints ==")
        # Agent posts its offer; the console poll should receive it and the status flips.
        r = c.post(f"/api/agent/remote/{sid}/signal",
                   json={"kind": "offer", "payload": {"sdp": "v=0..."}}, headers=auth)
        check("agent posts offer -> 200", r.status_code == 200)
        check("offer flips session to connecting",
              remote.get_session(db_path, sid)["status"] == remote.STATUS_CONNECTING)
        r = c.get(f"/api/remote/session/{sid}/poll?after_seq=0")
        pv = r.get_json()
        check("console poll receives the agent's offer",
              len(pv["signals"]) == 1 and pv["signals"][0]["kind"] == "offer")
        check("console poll reports status", pv["status"] == remote.STATUS_CONNECTING)

        # Console answers; the agent poll should receive it.
        r = c.post(f"/api/remote/session/{sid}/signal",
                   json={"kind": "answer", "payload": {"sdp": "answer"}})
        check("console posts answer -> 200", r.status_code == 200)
        r = c.get(f"/api/agent/remote/{sid}/poll?after_seq=0", headers=auth)
        av = r.get_json()
        check("agent poll receives the console's answer",
              len(av["signals"]) == 1 and av["signals"][0]["kind"] == "answer")

        print("\n== Agent isolation: a foreign agent can't touch this session ==")
        r = c.post(f"/api/agent/remote/{sid}/signal",
                   json={"kind": "ice", "payload": {"c": "x"}}, headers=other_auth)
        check("foreign agent signaling -> 404", r.status_code == 404)
        r = c.get(f"/api/agent/remote/{sid}/poll", headers=other_auth)
        check("foreign agent poll -> 404", r.status_code == 404)
        r = c.post(f"/api/agent/remote/{sid}/signal", json={"kind": "ice", "payload": {}})
        check("agent signal without token -> 401", r.status_code == 401)

        print("\n== Agent reports the session ended ==")
        r = c.post("/api/remote/PC-01/start", json={})
        agent_end_sid = r.get_json()["session_id"]
        r = c.post(f"/api/agent/remote/{agent_end_sid}/ended",
                   json={"reason": "consent denied"}, headers=auth)
        check("agent-ended -> 200", r.status_code == 200)
        check("agent-ended terminates the session",
              remote.get_session(db_path, agent_end_sid)["status"] == remote.STATUS_ENDED)
        r = c.post(f"/api/agent/remote/{agent_end_sid}/ended", json={}, headers=other_auth)
        check("a foreign agent cannot end this session -> 404", r.status_code == 404)

        print("\n== Stop ==")
        r = c.post(f"/api/remote/session/{sid}/stop")
        check("stop -> 200", r.status_code == 200)
        check("session is ended", remote.get_session(db_path, sid)["status"] == remote.STATUS_ENDED)
        r = c.post(f"/api/agent/remote/{sid}/signal",
                   json={"kind": "ice", "payload": {"c": "late"}}, headers=auth)
        check("signaling on an ended session -> 409", r.status_code == 409)

        print("\n== Authorization: capability + scope ==")
        global CURRENT_USER
        CURRENT_USER = "viewer@x.com"     # has view, NOT remote_control
        r = c.post("/api/remote/PC-01/start", json={})
        check("no remote_control -> 403", r.status_code == 403)

        CURRENT_USER = "tech@x.com"        # remote_control, scoped to PC-01 only
        r = c.post("/api/remote/PC-01/start", json={})
        check("scoped tech can start in scope -> 201", r.status_code == 201)
        tech_sid = r.get_json()["session_id"]
        r = c.post("/api/remote/PC-09/start", json={})
        check("scoped tech blocked out of scope -> 403", r.status_code == 403)

        # A session on a machine outside scope is invisible: unknown and out-of-scope both 404.
        CURRENT_USER = "super@x.com"
        r = c.post("/api/remote/PC-09/start", json={})
        pc09_sid = r.get_json()["session_id"]
        CURRENT_USER = "tech@x.com"
        r = c.get(f"/api/remote/session/{pc09_sid}/poll")
        check("out-of-scope session poll -> 404 (not an oracle)", r.status_code == 404)
        r = c.get("/api/remote/session/does-not-exist/poll")
        check("unknown session poll -> 404", r.status_code == 404)
        CURRENT_USER = "super@x.com"

        print("\n== Master switch ==")
        settings.set_many(db_path, {"remote.enabled": False})
        r = c.post("/api/remote/PC-01/start", json={})
        check("start refused when remote.enabled is off -> 403", r.status_code == 403)
        settings.set_many(db_path, {"remote.enabled": True})

        print("\n== TURN credentials flow into ice_servers when configured ==")
        os.environ["REMOTE_TURN_SECRET"] = "s3cr3t"
        settings.set_many(db_path, {"remote.turn_urls": ["turn:hub.example:3478"],
                                    "remote.stun_urls": ["stun:stun.example:3478"]})
        r = c.post("/api/remote/PC-01/start", json={})
        ice = r.get_json()["ice_servers"]
        check("ice_servers includes the configured STUN",
              any(s["urls"] == ["stun:stun.example:3478"] for s in ice))
        check("ice_servers includes a credentialed TURN",
              any("username" in s and s.get("urls") == ["turn:hub.example:3478"] for s in ice))
        del os.environ["REMOTE_TURN_SECRET"]

        print("\n== TURN status + secret management (manage_settings) ==")
        os.environ.pop("REMOTE_TURN_SECRET", None)
        # url_list, unlike str_list, accepts an empty list: "no STUN servers" is a real answer
        # on a LAN, and the Settings control lets you delete the last entry to say so.
        settings.set_many(db_path, {"remote.stun_urls": [], "remote.turn_urls": []})
        check("a url_list can be emptied through a normal save",
              settings.get_list(db_path, "remote.stun_urls") == []
              and settings.get_list(db_path, "remote.turn_urls") == [])
        r = c.get("/api/remote/turn/status")
        st = r.get_json()
        check("status -> 200 for a settings manager", r.status_code == 200)
        check("status: nothing configured -> secret unset, ice_count 0, writable",
              st["secret_set"] is False and st["ice_count"] == 0 and st["can_write_secret"] is True)

        # Rotate (empty body) mints a secret, writes it to .env AND the live env, returns it once.
        r = c.post("/api/remote/turn/secret", json={})
        body = r.get_json()
        check("rotate -> 200 with a fresh secret", r.status_code == 200 and len(body["secret"]) >= 32)
        check("rotated secret is written to .env",
              ("REMOTE_TURN_SECRET=" + body["secret"]) in open(env_path, encoding="utf-8").read())
        check("rotated secret is applied to the live process env",
              os.environ.get("REMOTE_TURN_SECRET") == body["secret"])

        settings.set_many(db_path, {"remote.turn_urls": ["turn:hub.example:3478"]})
        st = c.get("/api/remote/turn/status").get_json()
        check("status: with secret + a TURN url, a session yields >=1 ICE server",
              st["secret_set"] is True and st["turn_count"] == 1 and st["ice_count"] >= 1)

        # An explicit value is stored verbatim -- how you match an already-running coturn.
        r = c.post("/api/remote/turn/secret", json={"secret": "explicit-match"})
        check("explicit set stores the given value verbatim",
              r.get_json()["secret"] == "explicit-match"
              and os.environ.get("REMOTE_TURN_SECRET") == "explicit-match")

        CURRENT_USER = "tech@x.com"     # remote_control but NOT manage_settings
        check("status blocked without manage_settings -> 403",
              c.get("/api/remote/turn/status").status_code == 403)
        check("secret blocked without manage_settings -> 403",
              c.post("/api/remote/turn/secret", json={}).status_code == 403)
        CURRENT_USER = "super@x.com"
        os.environ.pop("REMOTE_TURN_SECRET", None)

        print("\n== Stream parameters are validated, not silently clamped ==")
        r = c.post("/api/remote/PC-01/start",
                   json={"fps": 30, "bitrate_kbps": 8000, "scale": 50,
                         "codec": "vp8", "encoder": "hardware", "session": 2, "monitor": 1})
        check("valid stream params -> 201", r.status_code == 201)
        queued = [cmd for cmd in fleet.claim_commands(db_path, agent_id, "PC-01")
                  if cmd["type"] == "start_remote_session"]
        params = queued[-1]["params"]
        check("stream params reach the agent command verbatim",
              params["fps"] == 30 and params["bitrate_kbps"] == 8000 and params["scale"] == 50
              and params["codec"] == "vp8" and params["encoder"] == "hardware"
              and params["target_session"] == 2 and params["monitor"] == 1)
        r = c.post("/api/remote/PC-01/start", json={})
        check("session defaults to auto (-1)",
              [cmd for cmd in fleet.claim_commands(db_path, agent_id, "PC-01")
               if cmd["type"] == "start_remote_session"][-1]["params"]["target_session"] == -1)
        check("fps out of range -> 400",
              c.post("/api/remote/PC-01/start", json={"fps": 500}).status_code == 400)
        check("unknown codec -> 400",
              c.post("/api/remote/PC-01/start", json={"codec": "av1"}).status_code == 400)
        # Session 0 is the non-interactive services session -- it has no desktop at all.
        check("session 0 -> 400",
              c.post("/api/remote/PC-01/start", json={"session": 0}).status_code == 400)

        print("\n== Inventory: sessions + displays for the picker and the headless badge ==")
        remote.record_inventory(db_path, "PC-01", {
            "sessions": [
                {"id": 1, "user": "", "station": "Console", "state_name": "connected",
                 "is_console": True, "is_logon_screen": True},
                {"id": 3, "user": "alice", "domain": "CONTOSO", "account": "CONTOSO\\alice",
                 "station": "RDP-Tcp#0", "state_name": "active", "client": "LAPTOP"},
            ],
            "displays": {"physical_monitors": 0, "active_outputs": 0, "headless": True,
                         "virtual_display_present": False, "output_names": []},
        })
        inv = c.get("/api/remote/PC-01/inventory").get_json()
        check("inventory returns both sessions", len(inv["sessions"]) == 2)
        check("the logon-screen session is surfaced, not filtered out",
              any(s["is_logon_screen"] and s["id"] == 1 for s in inv["sessions"]))
        check("inventory reports headless", inv["displays"]["headless"] is True)
        check("inventory says no driver payload is pinned yet",
              inv["payload_available"] is False)
        r = c.post("/api/remote/PC-01/inventory/refresh", json={})
        check("refresh queues refresh_remote_inventory -> 202", r.status_code == 202)
        check("refresh command reached the queue",
              any(cmd["type"] == "refresh_remote_inventory"
                  for cmd in fleet.claim_commands(db_path, agent_id, "PC-01")))

        print("\n== Virtual display ==")
        r = c.post("/api/remote/PC-01/virtual-display", json={"mode": "install"})
        check("install with no pinned payload -> 409", r.status_code == 409)

        digest = "a" * 64
        r = c.post("/api/remote/virtual-display/payload",
                   json={"sha256": digest, "version": "25.7.23", "filename": "vdd.zip"})
        check("pinning the payload -> 200", r.status_code == 200)
        check("a short digest is rejected -> 400",
              c.post("/api/remote/virtual-display/payload",
                     json={"sha256": "abc", "version": "1"}).status_code == 400)

        r = c.post("/api/remote/PC-01/virtual-display",
                   json={"mode": "install", "monitors": 1,
                         "resolutions": [{"width": 2560, "height": 1440, "hz": 60}]})
        check("install with a pinned payload -> 202", r.status_code == 202)
        install = [cmd for cmd in fleet.claim_commands(db_path, agent_id, "PC-01")
                   if cmd["type"] == "install_virtual_display"][-1]
        check("install command carries the pinned digest and the agent download url",
              install["params"]["payload_sha256"] == digest
              and install["params"]["payload_url"].endswith(f"/api/agent/packages/{digest}"))
        check("install command carries the requested mode",
              install["params"]["resolutions"] == [{"width": 2560, "height": 1440, "hz": 60}])
        check("an out-of-range resolution -> 400",
              c.post("/api/remote/PC-01/virtual-display",
                     json={"mode": "install",
                           "resolutions": [{"width": 99, "height": 99}]}).status_code == 400)
        check("monitors: 0 stand-down is accepted",
              c.post("/api/remote/PC-01/virtual-display/mode",
                     json={"monitors": 0}).status_code == 202)

        CURRENT_USER = "viewer@x.com"    # view only
        check("virtual display blocked without remote_control -> 403",
              c.post("/api/remote/PC-01/virtual-display", json={"mode": "install"}).status_code == 403)
        check("inventory blocked without remote_control -> 403",
              c.get("/api/remote/PC-01/inventory").status_code == 403)
        CURRENT_USER = "tech@x.com"      # remote_control, scoped to PC-01
        check("out-of-scope virtual display -> 403",
              c.post("/api/remote/PC-09/virtual-display", json={"mode": "install"}).status_code == 403)
        check("pinning the payload needs manage_settings -> 403",
              c.post("/api/remote/virtual-display/payload",
                     json={"sha256": "b" * 64, "version": "2"}).status_code == 403)
        CURRENT_USER = "super@x.com"

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        try:
            os.remove(env_path)
        except (OSError, NameError):
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
