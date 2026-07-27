"""Tests the audit model in fleet.py: levels, the level migration + backfill, and the
list_audit reader (filters, the level perimeter, keyset paging).

The assertions that matter most are the perimeter ones. `levels` is not a convenience
filter, it is the security boundary between an operator who may read the audit trail and
one who may additionally read security-level entries -- so a security row must stay
invisible even when a search term matches it, even when the caller asks for it by name,
and even when paging walks the whole table.

Run from the repo root so `import fleet` resolves.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

import fleet

PASS = 0
FAIL = 0

ORDINARY = {fleet.LEVEL_INFO, fleet.LEVEL_NOTICE}   # what view_audit_log alone may read


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def fresh_db():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    fleet.init_fleet_db(db_path)
    return db_path


def levels_of(db_path, action):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT level FROM audit_log WHERE action=? ORDER BY id DESC",
                           (action,)).fetchone()
    return row[0] if row else None


def test_levels_on_write():
    print("\n-- every audit row is classified, with or without an explicit level --")
    db = fresh_db()
    try:
        fleet.audit(db, "ann@x.com", "issue_command", "PC-1")
        fleet.audit(db, "ann@x.com", "alert.dismiss", "7")
        fleet.audit(db, "agent:PC-1", "complete_command", "PC-1")
        check("an omitted level falls back through ACTION_LEVELS",
              levels_of(db, "issue_command") == fleet.LEVEL_SECURITY
              and levels_of(db, "alert.dismiss") == fleet.LEVEL_NOTICE
              and levels_of(db, "complete_command") == fleet.LEVEL_INFO)

        fleet.audit(db, "ann@x.com", "alert.dismiss", "8", level=fleet.LEVEL_SECURITY)
        check("an explicit level wins over the map",
              levels_of(db, "alert.dismiss") == fleet.LEVEL_SECURITY)

        # Fail closed, twice over: an action nobody mapped, and a caller passing junk.
        fleet.audit(db, "ann@x.com", "something.brand_new", "x")
        check("an unmapped action defaults to security",
              levels_of(db, "something.brand_new") == fleet.LEVEL_SECURITY)
        fleet.audit(db, "ann@x.com", "complete_command", "PC-2", level="lol")
        check("an unrecognised level falls back to the map, never to NULL",
              levels_of(db, "complete_command") == fleet.LEVEL_INFO)

        with sqlite3.connect(db) as conn:
            nulls = conn.execute("SELECT COUNT(*) FROM audit_log WHERE level IS NULL"
                                 ).fetchone()[0]
        check("no row is ever written levelless", nulls == 0)

        check("every action the hub writes is mapped",
              all(lv in fleet.AUDIT_LEVELS for lv in fleet.ACTION_LEVELS.values()))
    finally:
        _rm(db)


def test_level_migration_backfills_history():
    print("\n-- a pre-level audit_log gains the column and is classified --")
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        # The table exactly as it shipped before levels existed: no `level` column at all.
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE audit_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          INTEGER NOT NULL,
                    actor       TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    target      TEXT,
                    detail_json TEXT
                )""")
            for action in ("issue_command", "alert.dismiss", "complete_command",
                           "action.nobody.mapped"):
                conn.execute("INSERT INTO audit_log(ts, actor, action, target) "
                             "VALUES (1, 'ann@x.com', ?, 'PC-1')", (action,))

        fleet.init_fleet_db(db_path)
        check("history is classified from the same map new writes use",
              levels_of(db_path, "issue_command") == fleet.LEVEL_SECURITY
              and levels_of(db_path, "alert.dismiss") == fleet.LEVEL_NOTICE
              and levels_of(db_path, "complete_command") == fleet.LEVEL_INFO)
        check("an action with no mapping is hidden, not leaked",
              levels_of(db_path, "action.nobody.mapped") == fleet.LEVEL_SECURITY)
        with sqlite3.connect(db_path) as conn:
            nulls = conn.execute("SELECT COUNT(*) FROM audit_log WHERE level IS NULL"
                                 ).fetchone()[0]
        check("no unlevelled row survives the migration", nulls == 0)

        # Idempotence: a second start must not re-stamp rows an operator's newer hub wrote.
        fleet.audit(db_path, "ann@x.com", "alert.dismiss", "9", level=fleet.LEVEL_INFO)
        fleet.init_fleet_db(db_path)
        check("re-running the migration leaves existing levels alone",
              levels_of(db_path, "alert.dismiss") == fleet.LEVEL_INFO)
    finally:
        _rm(db_path)


def test_filters():
    print("\n-- list_audit filters --")
    db = fresh_db()
    try:
        fleet.audit(db, "ann@x.com", "alert.dismiss", "PC-alpha")
        fleet.audit(db, "BOB@x.com", "machine.delete", "PC-beta")
        fleet.audit(db, "agent:PC-1", "complete_command", "100% done")
        fleet.audit(db, "ann@x.com", "backup_key_reveal", "hub")

        def entries(**kw):
            kw.setdefault("levels", None)
            return fleet.list_audit(db, **kw)["entries"]

        check("q matches on actor",
              [e["action"] for e in entries(q="bob")] == ["machine.delete"])
        check("q matches on action",
              [e["target"] for e in entries(q="dismiss")] == ["PC-alpha"])
        check("q matches on target",
              [e["actor"] for e in entries(q="beta")] == ["BOB@x.com"])
        # Unescaped, "100%" would be the wildcard "100 followed by anything".
        check("a % in the search term is literal, not a wildcard",
              len(entries(q="100%")) == 1 and len(entries(q="100% never-matches")) == 0)
        check("an _ in the search term is literal",
              len(entries(q="alert_dismiss")) == 0)
        check("a SQL injection attempt is just a search term",
              entries(q="%' OR 1=1 --") == [])

        check("actor is exact and case-insensitive",
              len(entries(actor="bob@x.com")) == 1 and len(entries(actor="bob")) == 0)
        check("action is exact",
              len(entries(action="machine.delete")) == 1)

        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE audit_log SET ts=100 WHERE action='alert.dismiss'")
            conn.execute("UPDATE audit_log SET ts=200 WHERE action='machine.delete'")
            conn.execute("UPDATE audit_log SET ts=300 WHERE action='complete_command'")
        check("since/until are inclusive",
              len(entries(since=100, until=200)) == 2
              and len(entries(since=101, until=199)) == 0)
    finally:
        _rm(db)


def test_level_perimeter():
    print("\n-- the level perimeter holds, in SQL, under every filter --")
    db = fresh_db()
    try:
        fleet.audit(db, "ann@x.com", "backup_key_reveal", "PC-secret")
        fleet.audit(db, "ann@x.com", "alert.dismiss", "PC-secret")

        def ordinary(**kw):
            return fleet.list_audit(db, levels=ORDINARY, **kw)["entries"]

        check("a security row is absent from an unrestricted read's counterpart",
              len(fleet.list_audit(db, levels=None)["entries"]) == 2
              and len(ordinary()) == 1)
        check("...even when the search term matches it",
              [e["action"] for e in ordinary(q="PC-secret")] == ["alert.dismiss"])
        check("...even when it is asked for by action name",
              ordinary(action="backup_key_reveal") == [])
        check("...and asking for the security level alone returns nothing",
              fleet.list_audit(db, levels={fleet.LEVEL_SECURITY} & ORDINARY)["entries"] == [])
        check("an empty allowed set returns nothing, never everything",
              fleet.list_audit(db, levels=set())["entries"] == [])

        # A row written before levels existed reads as security, so it is withheld from an
        # ordinary auditor rather than exposed by a migration that hasn't run.
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO audit_log(ts, actor, action, target, level) "
                         "VALUES (1, 'ghost', 'unknown.action', 'x', NULL)")
        check("a NULL level reads as security, i.e. withheld",
              all(e["action"] != "unknown.action" for e in ordinary())
              and any(e["action"] == "unknown.action"
                      for e in fleet.list_audit(db, levels=None)["entries"]))

        check("list_audit_actors takes the same perimeter",
              "ghost" not in fleet.list_audit_actors(db, levels=ORDINARY)
              and "ghost" in fleet.list_audit_actors(db, levels=None))
    finally:
        _rm(db)


def test_paging_with_tied_timestamps():
    print("\n-- keyset paging is exact when every row shares one timestamp --")
    db = fresh_db()
    try:
        for i in range(5):
            fleet.audit(db, "ann@x.com", "alert.dismiss", f"PC-{i}")
        # The case OFFSET paging gets wrong: audit ts is whole seconds, and a bulk
        # operation writes several rows inside one.
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE audit_log SET ts=5000")

        seen, cursor, pages = [], None, 0
        while True:
            page = fleet.list_audit(db, levels=ORDINARY, limit=2,
                                    before_ts=cursor["ts"] if cursor else None,
                                    before_id=cursor["id"] if cursor else None)
            seen.extend(e["id"] for e in page["entries"])
            pages += 1
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            if pages > 10:
                break
        check("every row is returned exactly once", sorted(seen) == sorted(set(seen))
              and len(seen) == 5)
        check("...in three pages of two, the last one short", pages == 3)
        check("the last page reports no cursor",
              fleet.list_audit(db, levels=ORDINARY, limit=50)["next_cursor"] is None)
        check("limit is honoured exactly",
              len(fleet.list_audit(db, levels=ORDINARY, limit=1)["entries"]) == 1)
        # A caller asking for everything gets a page, not the table; 0/None mean
        # "unspecified" and fall back to the default rather than returning nothing.
        check("limit is clamped, not trusted",
              len(fleet.list_audit(db, levels=ORDINARY, limit=99999)["entries"]) == 5
              and len(fleet.list_audit(db, levels=ORDINARY, limit=0)["entries"]) == 5)
    finally:
        _rm(db)


def test_detail_decoding():
    print("\n-- detail payloads decode, and a corrupt one does not lose the line --")
    db = fresh_db()
    try:
        fleet.audit(db, "ann@x.com", "alert.dismiss", "7", {"reason": "handled"})
        fleet.audit(db, "ann@x.com", "machine.delete", "PC-1")
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO audit_log(ts, actor, action, target, detail_json, level)"
                         " VALUES (9, 'ann@x.com', 'machine.merge', 'PC-2', '{oops', 'notice')")
        by_action = {e["action"]: e for e in fleet.list_audit(db, levels=ORDINARY)["entries"]}
        check("a JSON detail decodes to a dict",
              by_action["alert.dismiss"]["detail"] == {"reason": "handled"})
        check("no detail reads as None", by_action["machine.delete"]["detail"] is None)
        check("a corrupt detail loses the payload, not the audit line",
              "machine.merge" in by_action and by_action["machine.merge"]["detail"] is None)

        # audit() must survive a payload json.dumps can't take -- the action it records
        # matters more than its detail.
        fleet.audit(db, "ann@x.com", "alert.dismiss", "8", {"obj": object()})
        stored = fleet.list_audit(db, levels=ORDINARY, action="alert.dismiss")["entries"][0]
        check("an unserializable detail is recorded, not raised",
              isinstance(stored["detail"], dict) and "_unserializable" in stored["detail"])
    finally:
        _rm(db)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    test_levels_on_write()
    test_level_migration_backfills_history()
    test_filters()
    test_level_perimeter()
    test_paging_with_tied_timestamps()
    test_detail_decoding()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
