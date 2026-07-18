"""Tests duplicate-serial dedup / merge in the asset inventory (app.resolve_serial_group,
app.merge_machines, and the /api/report ingest trigger that fires them).

The same physical machine reappears under a new hostname after an agent upgrade
renames/re-cases it (OpenClaw -> OPENCLAW), leaving two machine_info rows that share
one BIOS serial. We collapse those, preferring the record still reporting; two live
machines on one serial are left alone; junk BIOS serials are never merged on.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# app.py resolves LOG_DIR/DB_PATH relative to the cwd at import time, so run it
# against a throwaway directory rather than the real logs/temp_v2.db.
_TMPDIR = tempfile.mkdtemp(prefix="hub-dedup-test-")
os.chdir(_TMPDIR)

import app

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


client = app.app.test_client()


def report(machine, serial, temp=42.0):
    return client.post("/api/report", json={
        "machine": machine, "temp": temp, "serial_number": serial, "model": "TestModel",
    })


def make_offline(machine, seconds_ago=None):
    """Backdate a machine's updated_at so derive_machine_status() reads it offline."""
    if seconds_ago is None:
        seconds_ago = app.DASHBOARD_ONLINE_WINDOW_SECONDS + 180
    ts = app.to_timestamp_str(datetime.now() - timedelta(seconds=seconds_ago))
    with app.get_db_conn() as conn:
        conn.execute("UPDATE machine_info SET updated_at=? WHERE machine=?", (ts, machine))


def machine_exists(machine):
    with app.get_db_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM machine_info WHERE machine=?", (machine,)
        ).fetchone()["c"] > 0


def readings_count(machine):
    with app.get_db_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM readings WHERE machine=?", (machine,)
        ).fetchone()["c"]


def seed_readings(machine, n=3):
    """Insert deterministic history rows (the async db_writer is too flaky to assert on)."""
    now = datetime.now()
    with app.get_db_conn() as conn:
        for i in range(n):
            ts = now - timedelta(minutes=i)
            conn.execute(
                "INSERT INTO readings(ts_text, ts_epoch, machine, temp, sensors_json) "
                "VALUES (?, ?, ?, ?, NULL)",
                (app.to_timestamp_str(ts), int(ts.timestamp()), machine, 40.0 + i),
            )


def test_valid_serial():
    print("\n-- is_valid_serial guards against junk --")
    check("real serial is valid", app.is_valid_serial("ABC123XYZ") is True)
    check("empty is junk", app.is_valid_serial("") is False)
    check("None is junk", app.is_valid_serial(None) is False)
    check("'Default string' is junk", app.is_valid_serial("Default string") is False)
    check("OEM placeholder is junk (case-insensitive)",
          app.is_valid_serial("To Be Filled By O.E.M.") is False)
    check("'0' is junk", app.is_valid_serial("0") is False)


def test_offline_overwrite_preserves_history():
    print("\n-- offline duplicate merges into the online one, history preserved --")
    report("OpenClaw", "SER-RENAME-1")
    seed_readings("OpenClaw", 3)          # pre-rename history
    make_offline("OpenClaw")               # old hostname went offline
    report("OPENCLAW", "SER-RENAME-1")     # new hostname reports -> triggers dedup

    check("offline duplicate removed", not machine_exists("OpenClaw"))
    check("online record survives", machine_exists("OPENCLAW"))
    check("survivor reads online", app.derive_machine_status(
        _updated_at("OPENCLAW")) == "online")
    check("old history re-pointed off the dropped host", readings_count("OpenClaw") == 0)
    check("old history now lives under the survivor", readings_count("OPENCLAW") >= 3)


def test_both_online_kept_separate():
    print("\n-- two live machines on one serial are NOT merged (pass 1: no auto-merge) --")
    report("boxA", "SER-CONFLICT-1")
    report("boxB", "SER-CONFLICT-1")       # both fresh/online -> conflict
    check("boxA kept", machine_exists("boxA"))
    check("boxB kept", machine_exists("boxB"))
    survivors = app.resolve_serial_group("SER-CONFLICT-1")
    check("resolve reports both as still present", set(survivors) == {"boxA", "boxB"})


def test_all_offline_keeps_newest():
    print("\n-- all-offline duplicates collapse to the most recently updated --")
    report("boxOld", "SER-OFFLINE-1")
    report("boxNew", "SER-OFFLINE-1")
    make_offline("boxOld", seconds_ago=600)
    make_offline("boxNew", seconds_ago=300)
    app.resolve_all_duplicate_serials()    # startup-style sweep
    check("older offline row dropped", not machine_exists("boxOld"))
    check("newest offline row kept", machine_exists("boxNew"))


def test_junk_serial_not_merged():
    print("\n-- machines sharing a junk BIOS serial are never merged --")
    report("junkA", "Default string")
    report("junkB", "Default string")      # ingest trigger must skip junk serials
    make_offline("junkA")
    app.resolve_all_duplicate_serials()
    check("junkA kept", machine_exists("junkA"))
    check("junkB kept", machine_exists("junkB"))
    check("resolve refuses to act on a junk serial",
          app.resolve_serial_group("Default string") == [])


def test_merge_cleans_fleet_and_caches():
    print("\n-- merge removes the dropped host's fleet enrollment and live caches --")
    report("boxKeep", "SER-CLEAN-1")
    report("boxDrop", "SER-CLEAN-1")
    with app.get_db_conn() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, machine, token_hash, enrolled_at, last_seen, revoked) "
            "VALUES ('stale-agent', 'boxDrop', 'h', 0, 0, 0)"
        )
    app.set_latest_temp("boxDrop", 55.0)

    app.merge_machines("boxKeep", "boxDrop")

    check("dropped identity row gone", not machine_exists("boxDrop"))
    check("survivor stays", machine_exists("boxKeep"))
    with app.get_db_conn() as conn:
        agent_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM agents WHERE machine='boxDrop'"
        ).fetchone()["c"]
    check("stale fleet enrollment removed", agent_rows == 0)
    check("live temp cache evicted", "boxDrop" not in app.latest_temp)


def _updated_at(machine):
    with app.get_db_conn() as conn:
        row = conn.execute(
            "SELECT updated_at FROM machine_info WHERE machine=?", (machine,)
        ).fetchone()
    return row["updated_at"] if row else None


if __name__ == "__main__":
    test_valid_serial()
    test_offline_overwrite_preserves_history()
    test_both_online_kept_separate()
    test_all_offline_keeps_newest()
    test_junk_serial_not_merged()
    test_merge_cleans_fleet_and_caches()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
