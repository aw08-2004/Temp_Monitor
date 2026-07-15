"""End-to-end test of fleet.py core logic against a temp SQLite DB.
Run from the repo root so `import fleet` resolves.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet

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
        fleet.init_fleet_db(db_path)

        # --- offline signing keypair (simulates sign_release.py private key) ---
        priv = Ed25519PrivateKey.generate()
        pub_hex = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        ).hex()

        def sign(ctype, machine, params):
            return priv.sign(fleet.canonical_command_bytes(ctype, machine, params)).hex()

        print("\n== Enrollment & auth ==")
        SECRET = "enroll-secret-xyz"
        agent_id, token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        check("enroll returns id+token", bool(agent_id) and bool(token))
        check("auth with correct token -> machine", fleet.authenticate_agent(db_path, agent_id, token) == "PC-01")
        check("auth with wrong token -> None", fleet.authenticate_agent(db_path, agent_id, "nope") is None)
        check("auth with unknown agent -> None", fleet.authenticate_agent(db_path, "deadbeef", token) is None)
        expect_raise("enroll wrong secret rejected", PermissionError,
                     lambda: fleet.enroll_agent(db_path, "PC-X", "wrong", SECRET))
        expect_raise("enroll with unset hub secret fails closed", PermissionError,
                     lambda: fleet.enroll_agent(db_path, "PC-X", "", ""))

        print("\n== Status derivation ==")
        check("fresh agent is online", fleet.derive_status(int(time.time())) == "online")
        check("old last_seen is offline", fleet.derive_status(int(time.time()) - 999) == "offline")
        check("no last_seen is unknown", fleet.derive_status(None) == "unknown")
        statuses = fleet.list_agent_status(db_path)
        check("list_agent_status shows PC-01 online",
              any(s["machine"] == "PC-01" and s["status"] == "online" for s in statuses))

        print("\n== Low-risk command lifecycle ==")
        cid = fleet.create_command(db_path, "PC-01", "restart", {}, issued_by="admin@x.com")
        check("restart command created", bool(cid))
        claimed = fleet.claim_commands(db_path, agent_id, "PC-01")
        check("agent claims exactly 1 command", len(claimed) == 1 and claimed[0]["id"] == cid)
        check("claimed restart needs no signature", claimed[0]["requires_signature"] is False)
        # Re-claim returns nothing (already claimed)
        check("second claim is empty", fleet.claim_commands(db_path, agent_id, "PC-01") == [])
        machine = fleet.complete_command(db_path, cid, agent_id, success=True, output="rebooting")
        check("complete returns machine", machine == "PC-01")
        got = fleet.get_command(db_path, cid)
        check("command now done", got["status"] == fleet.STATUS_DONE)
        check("result recorded", got["result"] and got["result"]["success"] == 1)

        print("\n== Cross-agent / bad completion ==")
        agent2, token2 = fleet.enroll_agent(db_path, "PC-02", SECRET, SECRET)
        cid2 = fleet.create_command(db_path, "PC-01", "shutdown", {}, issued_by="admin@x.com")
        fleet.claim_commands(db_path, agent_id, "PC-01")
        expect_raise("other agent cannot complete my command", PermissionError,
                     lambda: fleet.complete_command(db_path, cid2, agent2, True))
        expect_raise("completing unknown command raises", KeyError,
                     lambda: fleet.complete_command(db_path, "nope", agent_id, True))

        print("\n== High-risk command signing enforcement ==")
        expect_raise("run_script without signature rejected", PermissionError,
                     lambda: fleet.create_command(db_path, "PC-01", "run_script",
                                                  {"script": "echo hi"}, issued_by="admin@x.com",
                                                  public_key_hex=pub_hex))
        bad_sig = sign("run_script", "PC-01", {"script": "DIFFERENT"})
        expect_raise("run_script with tampered/mismatched signature rejected", PermissionError,
                     lambda: fleet.create_command(db_path, "PC-01", "run_script",
                                                  {"script": "echo hi"}, issued_by="admin@x.com",
                                                  signature=bad_sig, public_key_hex=pub_hex))
        good_sig = sign("run_script", "PC-01", {"script": "echo hi"})
        hcid = fleet.create_command(db_path, "PC-01", "run_script", {"script": "echo hi"},
                                    issued_by="admin@x.com", signature=good_sig, public_key_hex=pub_hex)
        check("run_script with valid signature accepted", bool(hcid))
        hclaim = [c for c in fleet.claim_commands(db_path, agent_id, "PC-01") if c["id"] == hcid][0]
        check("claimed high-risk carries signature for agent re-verify",
              hclaim["requires_signature"] and hclaim["signature"] == good_sig)
        check("agent-side re-verify of same signature passes",
              fleet.verify_command_signature(pub_hex, "run_script", "PC-01",
                                             {"script": "echo hi"}, good_sig))
        check("verify fails closed with empty pubkey",
              fleet.verify_command_signature("", "run_script", "PC-01",
                                             {"script": "echo hi"}, good_sig) is False)

        print("\n== Unknown type & expiry ==")
        expect_raise("unknown command type rejected", ValueError,
                     lambda: fleet.create_command(db_path, "PC-01", "frobnicate", {}, issued_by="a"))
        exp_cid = fleet.create_command(db_path, "PC-03", "restart", {}, issued_by="a", ttl_seconds=-1)
        check("expired command not delivered", fleet.claim_commands(db_path, "deadagent", "PC-03") == [])
        check("expired command marked expired",
              fleet.get_command(db_path, exp_cid)["status"] == fleet.STATUS_EXPIRED)

        print("\n== Audit trail ==")
        with fleet.get_conn(db_path) as conn:
            actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
        for expected in ("enroll", "issue_command", "claim_commands", "complete_command"):
            check(f"audit logged '{expected}'", expected in actions)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
        for ext in ("-wal", "-shm"):
            try:
                os.remove(db_path + ext)
            except OSError:
                pass


if __name__ == "__main__":
    main()
