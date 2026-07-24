"""End-to-end test of remote.py core logic against a temp SQLite DB (roadmap #2).
Run from the repo root so `import remote` resolves.
"""
import base64
import hashlib
import hmac
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import remote

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


def expect_raise(name, exc, fn):
    try:
        fn()
        check(name + " (expected raise)", False)
    except exc:
        check(name, True)
    except Exception as e:
        check(f"{name} (wrong exc {type(e).__name__})", False)


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        # remote.audit writes to the fleet audit_log, so both schemas must exist.
        fleet.init_fleet_db(db_path)
        remote.init_remote_db(db_path)

        print("\n== Session lifecycle ==")
        sid = remote.create_session(db_path, "PC-01", "op@x.com", "unattended")
        check("create_session returns an id", bool(sid))
        sess = remote.get_session(db_path, sid)
        check("new session is pending", sess["status"] == remote.STATUS_PENDING)
        check("session records machine + issuer", sess["machine"] == "PC-01" and sess["issued_by"] == "op@x.com")
        check("session start is audited",
              any(r["action"] == "remote_session_start" for r in _audit_rows(db_path)))
        expect_raise("create_session needs a machine", ValueError,
                     lambda: remote.create_session(db_path, "  ", "op@x.com", "unattended"))

        check("mark_status advances to connecting", remote.mark_status(db_path, sid, remote.STATUS_CONNECTING))
        check("status is connecting", remote.get_session(db_path, sid)["status"] == remote.STATUS_CONNECTING)
        check("active session shows in list_sessions active_only",
              any(s["id"] == sid for s in remote.list_sessions(db_path, "PC-01", active_only=True)))

        print("\n== Signaling relay ==")
        # Agent posts an offer; the console (poller) should receive it.
        off = remote.add_signal(db_path, sid, remote.SENDER_AGENT, "offer", {"sdp": "v=0..."})
        ice1 = remote.add_signal(db_path, sid, remote.SENDER_AGENT, "ice", {"candidate": "cand-a"})
        check("add_signal returns increasing seqs", ice1 > off)

        console_view = remote.get_signals(db_path, sid, remote.SENDER_CONSOLE, after_seq=0)
        check("console receives the agent's offer + ice", len(console_view["signals"]) == 2)
        check("console does NOT receive its own signals",
              all(s["sender"] == remote.SENDER_AGENT for s in console_view["signals"]))
        check("payload round-trips as an object", console_view["signals"][0]["payload"]["sdp"] == "v=0...")
        cursor = console_view["next_seq"]
        check("cursor advanced", cursor == ice1)
        check("polling past the cursor yields nothing",
              remote.get_signals(db_path, sid, remote.SENDER_CONSOLE, after_seq=cursor)["signals"] == [])

        # Console answers; the agent (poller) should receive it and NOT its own offer.
        remote.add_signal(db_path, sid, remote.SENDER_CONSOLE, "answer", {"sdp": "answer-sdp"})
        agent_view = remote.get_signals(db_path, sid, remote.SENDER_AGENT, after_seq=0)
        check("agent receives exactly the console's answer", len(agent_view["signals"]) == 1)
        check("agent's view is the answer", agent_view["signals"][0]["kind"] == "answer")

        expect_raise("unknown sender rejected", ValueError,
                     lambda: remote.add_signal(db_path, sid, "martian", "offer", {}))
        expect_raise("unknown kind rejected", ValueError,
                     lambda: remote.add_signal(db_path, sid, remote.SENDER_AGENT, "gibberish", {}))
        expect_raise("oversized payload rejected", ValueError,
                     lambda: remote.add_signal(db_path, sid, remote.SENDER_AGENT, "offer",
                                               {"sdp": "x" * (remote.MAX_SIGNAL_BYTES + 1)}))
        expect_raise("signal to unknown session raises KeyError", KeyError,
                     lambda: remote.add_signal(db_path, "nope", remote.SENDER_AGENT, "offer", {}))
        expect_raise("get_signals on unknown session raises KeyError", KeyError,
                     lambda: remote.get_signals(db_path, "nope", remote.SENDER_AGENT))

        print("\n== End + expiry ==")
        check("end_session ends a live session", remote.end_session(db_path, sid, "operator closed"))
        check("ended session status", remote.get_session(db_path, sid)["status"] == remote.STATUS_ENDED)
        check("end is idempotent", remote.end_session(db_path, sid, "again") is False)
        check("end is audited",
              any(r["action"] == "remote_session_end" for r in _audit_rows(db_path)))
        # Signaling on an ended session is refused (a closed session can't be reopened).
        expect_raise("signal on ended session refused", PermissionError,
                     lambda: remote.add_signal(db_path, sid, remote.SENDER_AGENT, "ice", {"candidate": "late"}))
        check("mark_status on ended session is a no-op",
              remote.mark_status(db_path, sid, remote.STATUS_ACTIVE) is False)

        # TTL expiry sweeps live sessions, and only those past their TTL.
        s_fresh = remote.create_session(db_path, "PC-02", "op@x.com", "unattended", ttl_seconds=3600)
        s_stale = remote.create_session(db_path, "PC-03", "op@x.com", "unattended", ttl_seconds=1)
        time.sleep(1.1)
        swept = remote.expire_sessions(db_path)
        check("expire_sessions retired the stale one", swept == 1)
        check("stale session is expired", remote.get_session(db_path, s_stale)["status"] == remote.STATUS_EXPIRED)
        check("fresh session survives expiry", remote.get_session(db_path, s_fresh)["status"] == remote.STATUS_PENDING)

        print("\n== TURN credentials ==")
        secret = "turn-shared-secret"
        cred = remote.mint_turn_credentials(secret, "sess-xyz", ttl_seconds=600)
        check("username is '<expiry>:<session>'", cred["username"].endswith(":sess-xyz"))
        # Re-derive the password the way the TURN server will, to prove interop.
        expected = base64.b64encode(
            hmac.new(secret.encode(), cred["username"].encode(), hashlib.sha1).digest()).decode()
        check("password is HMAC-SHA1(secret, username) b64", cred["password"] == expected)
        check("expiry is in the future", cred["expiry"] > int(time.time()))
        expect_raise("minting without a secret fails", ValueError,
                     lambda: remote.mint_turn_credentials("", "s", 600))

        print("\n== ICE server assembly ==")
        stun_only = remote.ice_servers("s1", stun_urls=["stun:stun.example:3478"])
        check("stun-only yields one credential-free server",
              len(stun_only) == 1 and "username" not in stun_only[0]
              and stun_only[0]["urls"] == ["stun:stun.example:3478"])
        full = remote.ice_servers("s1", stun_urls=["stun:stun.example:3478"],
                                  turn_urls=["turn:hub.example:3478"], turn_secret=secret)
        check("with turn configured, a credentialed turn server is added",
              any("username" in s and s["urls"] == ["turn:hub.example:3478"] for s in full))
        check("turn is skipped when no secret is set",
              all("turn:" not in str(s.get("urls")) for s in remote.ice_servers("s1", turn_urls=["turn:x:3478"])))

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


def _audit_rows(db_path):
    with fleet.get_conn(db_path) as conn:
        return conn.execute("SELECT * FROM audit_log").fetchall()


if __name__ == "__main__":
    main()
