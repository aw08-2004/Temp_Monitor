"""End-to-end test of fleet.py core logic against a temp SQLite DB.
Run from the repo root so `import fleet` resolves.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet

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

        print("\n== Command lifecycle ==")
        cid = fleet.create_command(db_path, "PC-01", "restart", {}, issued_by="admin@x.com")
        check("restart command created", bool(cid))
        claimed = fleet.claim_commands(db_path, agent_id, "PC-01")
        check("agent claims exactly 1 command", len(claimed) == 1 and claimed[0]["id"] == cid)
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

        print("\n== Commands need no signature ==")
        # These three used to require an offline Ed25519 signature. They no longer do:
        # an authorized session is the whole gate, so a helpdesk operator can issue them
        # directly. (In practice the old gate was never passable -- no key was ever
        # configured on hub or agent -- so run_script was refused outright.)
        script = {"script": "Get-Service Spooler", "shell": "powershell"}
        rcid = fleet.create_command(db_path, "PC-01", "run_script", script, issued_by="helpdesk@x.com")
        check("run_script accepted with no signature", bool(rcid))
        for ctype in ("install_driver", "update_bios"):
            check(f"{ctype} accepted with no signature",
                  bool(fleet.create_command(db_path, "PC-01", ctype, {}, issued_by="helpdesk@x.com")))

        rclaim = [c for c in fleet.claim_commands(db_path, agent_id, "PC-01") if c["id"] == rcid][0]
        check("claimed run_script carries params intact", rclaim["params"] == script)
        check("claim no longer exposes signature fields",
              "signature" not in rclaim and "requires_signature" not in rclaim)
        check("list_commands no longer exposes requires_signature",
              all("requires_signature" not in row for row in fleet.list_commands(db_path)))

        print("\n== Live output streaming ==")
        scid = fleet.create_command(db_path, "PC-01", "run_script",
                                    {"script": "loop"}, issued_by="helpdesk@x.com")
        fleet.claim_commands(db_path, agent_id, "PC-01")

        check("no output yet -> next_seq 0 (a non-streaming agent looks identical)",
              fleet.get_command_output(db_path, scid)["next_seq"] == 0)

        for i, text in enumerate(["step 1\n", "step 2\n", "step 3\n"]):
            fleet.append_command_output(db_path, scid, agent_id, i, text)
        out = fleet.get_command_output(db_path, scid)
        check("chunks come back in seq order",
              [c["text"] for c in out["chunks"]] == ["step 1\n", "step 2\n", "step 3\n"])
        check("next_seq is the cursor to resume from", out["next_seq"] == 3)
        check("status rides along with the chunks", out["status"] == fleet.STATUS_CLAIMED)
        check("no result while still running", out["result"] is None)

        after = fleet.get_command_output(db_path, scid, after_seq=1)
        check("after_seq returns only newer chunks",
              [c["seq"] for c in after["chunks"]] == [2])

        # The whole retry story depends on this being a no-op.
        fleet.append_command_output(db_path, scid, agent_id, 1, "step 2\n")
        check("re-posting the same seq is idempotent",
              len(fleet.get_command_output(db_path, scid)["chunks"]) == 3)
        fleet.append_command_output(db_path, scid, agent_id, 1, "DIFFERENT")
        check("re-posting a seq cannot overwrite the original text",
              fleet.get_command_output(db_path, scid)["chunks"][1]["text"] == "step 2\n")

        expect_raise("foreign agent cannot inject output", PermissionError,
                     lambda: fleet.append_command_output(db_path, scid, agent2, 9, "evil"))
        expect_raise("output for an unknown command raises", KeyError,
                     lambda: fleet.append_command_output(db_path, "nope", agent_id, 0, "x"))
        expect_raise("oversized chunk rejected", ValueError,
                     lambda: fleet.append_command_output(
                         db_path, scid, agent_id, 99,
                         "x" * (fleet.STREAM_MAX_CHUNK_CHARS + 1)))
        expect_raise("non-integer seq rejected", ValueError,
                     lambda: fleet.append_command_output(db_path, scid, agent_id, "abc", "x"))
        expect_raise("negative seq rejected", ValueError,
                     lambda: fleet.append_command_output(db_path, scid, agent_id, -1, "x"))
        expect_raise("non-integer after_seq rejected", ValueError,
                     lambda: fleet.get_command_output(db_path, scid, after_seq="abc"))
        expect_raise("output for an unknown command on read raises", KeyError,
                     lambda: fleet.get_command_output(db_path, "nope"))

        print("\n== Output cap ==")
        ccid = fleet.create_command(db_path, "PC-01", "run_script",
                                    {"script": "runaway"}, issued_by="helpdesk@x.com")
        fleet.claim_commands(db_path, agent_id, "PC-01")
        big = "x" * fleet.STREAM_MAX_CHUNK_CHARS
        capped_at = None
        for i in range(60):  # 60 * 16k = 960k, well past the 256k cap
            if fleet.append_command_output(db_path, ccid, agent_id, i, big):
                capped_at = i
                break
        check("runaway output reports truncated", capped_at is not None)
        capped = fleet.get_command_output(db_path, ccid)
        check("cap is surfaced to the console", capped["truncated"] is True)
        total = sum(len(c["text"]) for c in capped["chunks"])
        check("stored output stays near the cap, not unbounded",
              total < fleet.STREAM_MAX_COMMAND_CHARS * 2)
        marker_count = sum(1 for c in capped["chunks"]
                           if c["text"] == fleet.STREAM_TRUNCATION_MARKER)
        # Further posts after the cap must not each append another marker.
        for i in range(capped_at + 1, capped_at + 5):
            fleet.append_command_output(db_path, ccid, agent_id, i, big)
        after_cap = fleet.get_command_output(db_path, ccid)
        check("marker written exactly once",
              sum(1 for c in after_cap["chunks"]
                  if c["text"] == fleet.STREAM_TRUNCATION_MARKER) == 1 and marker_count <= 1)
        check("post-cap chunks are dropped, not stored",
              len(after_cap["chunks"]) == len(capped["chunks"]))

        print("\n== Output after completion ==")
        fleet.complete_command(db_path, scid, agent_id, success=True, output="step 1\nstep 2\nstep 3\n")
        done = fleet.get_command_output(db_path, scid)
        check("result appears alongside the chunks", done["result"]["success"] == 1)
        check("status flips to done", done["status"] == fleet.STATUS_DONE)
        check("chunks survive completion (scrollback stays readable)",
              len(done["chunks"]) == 3)
        expect_raise("late output for a finished command refused", PermissionError,
                     lambda: fleet.append_command_output(db_path, scid, agent_id, 50, "late"))

        print("\n== Output pruning ==")
        pruned = fleet.prune_command_output(db_path, int(time.time()) + 60)
        check("pruner removes aged scrollback", pruned > 0)
        check("pruned command keeps its durable result",
              fleet.get_command(db_path, scid)["result"]["output"] == "step 1\nstep 2\nstep 3\n")
        check("pruned command has no chunks left",
              fleet.get_command_output(db_path, scid)["chunks"] == [])
        fresh_cid = fleet.create_command(db_path, "PC-05", "run_script", {"script": "x"},
                                         issued_by="a@x.com")
        fleet.claim_commands(db_path, "agent-5", "PC-05")
        fleet.append_command_output(db_path, fresh_cid, "agent-5", 0, "running")
        fleet.prune_command_output(db_path, int(time.time()) - 3600)
        check("pruner spares recent output",
              len(fleet.get_command_output(db_path, fresh_cid)["chunks"]) == 1)

        print("\n== Pre-1.10 DB compatibility ==")
        # The live DB predates the signing removal and still has requires_signature +
        # signature. init_fleet_db is CREATE TABLE IF NOT EXISTS with no ALTER path, so
        # it will never rewrite them -- the whole no-migration bet is that they are
        # inert. Prove it against a production-shaped table.
        legacy_fd, legacy_db = tempfile.mkstemp(suffix=".db")
        os.close(legacy_fd)
        try:
            with fleet.get_conn(legacy_db) as conn:
                conn.execute("""
                    CREATE TABLE commands (
                        id TEXT PRIMARY KEY, machine TEXT NOT NULL, type TEXT NOT NULL,
                        params_json TEXT NOT NULL,
                        requires_signature INTEGER NOT NULL DEFAULT 0, signature TEXT,
                        issued_by TEXT NOT NULL, created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL, status TEXT NOT NULL,
                        claimed_at INTEGER, claimed_by TEXT)""")
                conn.execute(
                    "INSERT INTO commands VALUES ('old1','PC-9','run_script',"
                    "'{\"script\":\"legacy\"}',1,'abc123','admin@x.com',1,9999999999,"
                    "'done',1,'agent-old')")
            fleet.init_fleet_db(legacy_db)

            lcid = fleet.create_command(legacy_db, "PC-9", "run_script",
                                        {"script": "new"}, issued_by="helpdesk@x.com")
            check("create_command works against a pre-1.10 table", bool(lcid))
            with fleet.get_conn(legacy_db) as conn:
                r = conn.execute("SELECT requires_signature, signature FROM commands "
                                 "WHERE id = ?", (lcid,)).fetchone()
            check("legacy columns default harmlessly on insert",
                  r["requires_signature"] == 0 and r["signature"] is None)
            check("claim works against a pre-1.10 table",
                  len(fleet.claim_commands(legacy_db, "agent-new", "PC-9")) == 1)
            check("get_command reads a legacy signed row",
                  fleet.get_command(legacy_db, "old1")["type"] == "run_script")
            # SELECT * would otherwise leak the dead columns on old DBs only, making the
            # API response shape depend on the DB's age.
            check("get_command hides vestigial signing columns",
                  all(k not in fleet.get_command(legacy_db, "old1")
                      for k in ("requires_signature", "signature")))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(legacy_db + suffix)
                except OSError:
                    pass

        print("\n== Params shape validation ==")
        expect_raise("non-dict params rejected", ValueError,
                     lambda: fleet.create_command(db_path, "PC-01", "run_script",
                                                  ["not", "a", "dict"], issued_by="a"))
        expect_raise("blank machine rejected", ValueError,
                     lambda: fleet.create_command(db_path, "   ", "restart", {}, issued_by="a"))

        print("\n== Unknown type & expiry ==")
        expect_raise("unknown command type rejected", ValueError,
                     lambda: fleet.create_command(db_path, "PC-01", "frobnicate", {}, issued_by="a"))
        exp_cid = fleet.create_command(db_path, "PC-03", "restart", {}, issued_by="a", ttl_seconds=-1)
        check("expired command not delivered", fleet.claim_commands(db_path, "deadagent", "PC-03") == [])
        check("expired command marked expired",
              fleet.get_command(db_path, exp_cid)["status"] == fleet.STATUS_EXPIRED)

        print("\n== Audit trail ==")
        # With signing gone this trail is the ONLY record of who ran what, so it is
        # load-bearing rather than nice-to-have -- assert its contents, not just that
        # a row exists.
        with fleet.get_conn(db_path) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT actor, action, target, detail_json FROM audit_log ORDER BY id")]
        actions = [r["action"] for r in rows]
        for expected in ("enroll", "issue_command", "claim_commands", "complete_command"):
            check(f"audit logged '{expected}'", expected in actions)

        issues = [r for r in rows if r["action"] == "issue_command"]
        run_script_audit = [r for r in issues if '"run_script"' in (r["detail_json"] or "")]
        check("issue_command audit names the issuing operator",
              any(r["actor"] == "helpdesk@x.com" for r in run_script_audit))
        check("issue_command audit records the script text (accountability control)",
              any("Get-Service Spooler" in (r["detail_json"] or "") for r in run_script_audit))
        check("issue_command audit no longer records high_risk",
              all("high_risk" not in (r["detail_json"] or "") for r in issues))

        # A pasted megabyte must not bloat the log table.
        big = fleet.create_command(db_path, "PC-01", "run_script",
                                   {"script": "x" * 50_000}, issued_by="helpdesk@x.com")
        with fleet.get_conn(db_path) as conn:
            detail = conn.execute(
                "SELECT detail_json FROM audit_log WHERE action = 'issue_command' "
                "ORDER BY id DESC LIMIT 1").fetchone()["detail_json"]
        check("oversized params truncated in audit detail",
              len(detail) < 10_000 and big is not None)

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
