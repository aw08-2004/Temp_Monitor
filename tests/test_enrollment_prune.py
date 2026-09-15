"""An enrollment prune that deletes a live agent, or that quietly stops matching anything.

**Two silent failures, pulling in opposite directions** (roadmap #23). Android agents 0.2.1
and 0.2.2 threw away every token they were issued and enrolled again every 30 seconds, so a device left
running for a day left thousands of never-used credential rows under one machine name.
fleet.prune_unauthenticated_enrollments removes them, and can go wrong either way without a
sound:

  * **Too eager.** It deletes a row an agent really holds. That agent's next heartbeat is a
    401 `unknown`; it re-enrolls, drops any command it had claimed, and nothing in the
    console explains why. Or it deletes a REVOKED row, and a machine that was deliberately
    shut out is invited back in.
  * **Too timid.** It keys off `last_seen = enrolled_at`, which is how it was first sketched.
    touch_last_seen rewrites last_seen on every orphan the moment the device's telemetry
    works, so the prune matches nothing on exactly the devices it exists for, while the
    retention log reports success by saying nothing.

Also asserted: pruning never changes what the console shows about a machine.
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet

PASS = 0
FAIL = 0

SECRET = "enroll-secret"
HOUR = 3600


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def _backdate(db_path, agent_id, enrolled_at):
    """Make a row look as if it was enrolled at `enrolled_at`, untouched since."""
    with fleet.get_conn(db_path) as conn:
        conn.execute("UPDATE agents SET enrolled_at = ?, last_seen = ? WHERE agent_id = ?",
                     (enrolled_at, enrolled_at, agent_id))


def _ids(db_path, machine):
    with fleet.get_conn(db_path) as conn:
        return {r["agent_id"] for r in conn.execute(
            "SELECT agent_id FROM agents WHERE machine = ?", (machine,))}


def _loop(db_path, machine, count, start):
    """What a 0.2.1 or 0.2.2 device does: enroll, lose the token, enroll 30 seconds later."""
    ids = []
    for i in range(count):
        agent_id, _ = fleet.enroll_agent(db_path, machine, SECRET, SECRET)
        _backdate(db_path, agent_id, start + i * 30)
        ids.append(agent_id)
    return ids


def _visible(db_path):
    """Everything the console derives from the agents table."""
    return (fleet.list_agent_status(db_path), fleet.enrolled_machines(db_path))


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        now = int(time.time())
        old = now - 5 * HOUR

        print("\n== the re-enrollment loop ==")
        loop = _loop(db_path, "PHONE-01", 6, old)
        before = _visible(db_path)
        removed = fleet.prune_unauthenticated_enrollments(db_path, now=now)
        check("five of six never-used enrollments pruned", removed == 5)
        check("the newest enrollment is the one that survives",
              _ids(db_path, "PHONE-01") == {loop[-1]})
        check("the console sees exactly what it saw before", _visible(db_path) == before)
        check("the machine still reads as enrolled", fleet.is_enrolled(db_path, "PHONE-01"))
        check("a second pass finds nothing", fleet.prune_unauthenticated_enrollments(db_path, now=now) == 0)

        print("\n== orphans that telemetry has touched ==")
        # The case `last_seen = enrolled_at` would have missed. A 0.2.2 beta device reports while
        # it loops, and every report rewrites last_seen on all of its machine's rows.
        loop = _loop(db_path, "PHONE-02", 4, old)
        fleet.touch_last_seen(db_path, "PHONE-02")
        with fleet.get_conn(db_path) as conn:
            touched = conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE machine = 'PHONE-02' "
                "AND last_seen > enrolled_at").fetchone()["n"]
        check("precondition: the report moved last_seen on every orphan", touched == 4)
        before = _visible(db_path)
        check("touched orphans are still pruned",
              fleet.prune_unauthenticated_enrollments(db_path, now=now) == 3)
        check("...leaving the newest", _ids(db_path, "PHONE-02") == {loop[-1]})
        check("...and the console unchanged", _visible(db_path) == before)

        print("\n== rows that must survive ==")
        # A real agent, superseded by a later enrollment under the same name: a reinstall that
        # lost agent.json, or two PCs sharing a hostname. It authenticated, so it stays.
        real_id, real_token = fleet.enroll_agent(db_path, "PC-REAL", SECRET, SECRET)
        _backdate(db_path, real_id, old)
        check("precondition: the real agent authenticates",
              fleet.authenticate_agent(db_path, real_id, real_token) == "PC-REAL")
        newer_id, _ = fleet.enroll_agent(db_path, "PC-REAL", SECRET, SECRET)
        _backdate(db_path, newer_id, old + 60)
        fleet.prune_unauthenticated_enrollments(db_path, now=now)
        check("a superseded agent that ever authenticated is kept",
              real_id in _ids(db_path, "PC-REAL"))
        check("...and its token still works",
              fleet.authenticate_agent(db_path, real_id, real_token) == "PC-REAL")

        # Inside the grace window: an agent that has its token and has not heartbeated yet.
        fresh = _loop(db_path, "PHONE-FRESH", 3, now - 120)
        fleet.prune_unauthenticated_enrollments(db_path, now=now)
        check("enrollments younger than the grace period are kept",
              _ids(db_path, "PHONE-FRESH") == set(fresh))

        # A revoked row is what makes its holder hear "revoked" and stay down.
        revoked_id, revoked_token = fleet.enroll_agent(db_path, "PC-REVOKED", SECRET, SECRET)
        _backdate(db_path, revoked_id, old)
        fleet.revoke_agent(db_path, revoked_id)
        later_id, _ = fleet.enroll_agent(db_path, "PC-REVOKED", SECRET, SECRET)
        _backdate(db_path, later_id, old + 60)
        fleet.prune_unauthenticated_enrollments(db_path, now=now)
        check("a revoked, never-used, superseded row is kept",
              revoked_id in _ids(db_path, "PC-REVOKED"))
        check("...so its holder is still told revoked",
              fleet.auth_failure_reason(db_path, revoked_id, revoked_token) == fleet.AUTH_REVOKED)

        # Superseded only by a REVOKED row: pruning would flip the machine to not-enrolled.
        orphan_id, _ = fleet.enroll_agent(db_path, "PC-ONLY-REVOKED-NEWER", SECRET, SECRET)
        _backdate(db_path, orphan_id, old)
        newest_revoked, _ = fleet.enroll_agent(db_path, "PC-ONLY-REVOKED-NEWER", SECRET, SECRET)
        _backdate(db_path, newest_revoked, old + 60)
        fleet.revoke_agent(db_path, newest_revoked)
        fleet.prune_unauthenticated_enrollments(db_path, now=now)
        check("an orphan whose only newer row is revoked is kept",
              fleet.is_enrolled(db_path, "PC-ONLY-REVOKED-NEWER"))

        # A lone enrollment never pruned, however old: it IS the machine's enrollment.
        lone_id, _ = fleet.enroll_agent(db_path, "PC-LONE", SECRET, SECRET)
        _backdate(db_path, lone_id, now - 90 * 86400)
        fleet.prune_unauthenticated_enrollments(db_path, now=now)
        check("a machine's only enrollment is kept, however old",
              _ids(db_path, "PC-LONE") == {lone_id})

        print("\n== migrating a hub that predates last_auth ==")
        legacy_fd, legacy_path = tempfile.mkstemp(suffix=".db")
        os.close(legacy_fd)
        try:
            conn = sqlite3.connect(legacy_path)
            conn.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY, machine TEXT NOT NULL, "
                         "token_hash TEXT NOT NULL, enrolled_at INTEGER NOT NULL, "
                         "last_seen INTEGER, revoked INTEGER NOT NULL DEFAULT 0)")
            conn.executemany(
                "INSERT INTO agents VALUES (?, ?, 'h', ?, ?, 0)",
                [("used", "PC-L", old, now),          # has been seen since enrolling
                 ("never", "PC-L", old, old),         # untouched since enrolling
                 ("newest", "PC-L", old + 60, old + 60)])
            conn.commit()
            conn.close()
            fleet.init_fleet_db(legacy_path)
            with fleet.get_conn(legacy_path) as c:
                rows = {r["agent_id"]: r["last_auth"] for r in c.execute(
                    "SELECT agent_id, last_auth FROM agents")}
            check("a row seen since enrolling is backfilled as authenticated", rows["used"] == now)
            check("an untouched row migrates as never authenticated", rows["never"] is None)
            fleet.prune_unauthenticated_enrollments(legacy_path, now=now)
            check("after migration, only the untouched superseded row is pruned",
                  _ids(legacy_path, "PC-L") == {"used", "newest"})
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(legacy_path + suffix)
                except OSError:
                    pass

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
