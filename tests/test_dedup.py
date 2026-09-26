"""Tests duplicate-serial dedup / merge in the asset inventory (app.resolve_serial_group,
app.merge_machines, and the /api/report ingest trigger that fires them).

The same physical machine reappears under a new hostname after an agent upgrade
renames/re-cases it (OpenClaw -> OPENCLAW), leaving two machine_info rows that share
one BIOS serial. We collapse those, preferring the record still reporting; two live
machines on one serial are left alone; junk BIOS serials are never merged on.

Run from the repo root so `import app` resolves.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

# app.py resolves LOG_DIR/DB_PATH relative to the cwd at import time, so run it
# against a throwaway directory rather than the real logs/temp_v2.db.
_TMPDIR = tempfile.mkdtemp(prefix="hub-dedup-test-")
# See test_alerts.py: app resolves its DB from HUB_LOG_DIR, so declare this module's dir
# before importing app to keep a standalone run off the real logs/.
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
# The session user these tests sign in as has to be a break-glass superuser, or every
# console endpoint below now 403s on the permission-group layer. Set before importing
# app, which reads ALLOWED_EMAILS at import time; load_dotenv doesn't override an
# already-set env var, so this beats the real .env.
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import alerts
import app
import backups
import bitlocker
import console_session
import fleet
import settings


def _write_reading_synchronously(timestamp_str, timestamp_epoch, machine, temp,
                                 sensors_json=None, metrics=None):
    """Commit a report's reading inline instead of handing it to the async db_writer.

    /api/report queues its reading and the writer thread batches for
    DB_WRITE_FLUSH_SECONDS (0.5) before touching SQLite -- while the merge this module
    tests fires inside the very same request. So a merge re-points the rows that are on
    disk at that instant, and the queued one lands afterwards under the hostname the
    merge just deleted. Whether "no readings left under the dropped name" holds then
    comes down to whether the assert or the flush timer wins the race: fast box passes,
    loaded box fails, same code either way.

    Patching it out (rather than sleeping until the flush, which just makes the race
    slower) also means no writer thread ever starts, so nothing in this module writes to
    the DB off the test's own thread. This is app's own synchronous path -- the fallback
    enqueue_reading itself takes when the queue is full -- not a reimplementation of it.
    """
    app.write_readings_batch([
        (timestamp_str, timestamp_epoch, machine, float(temp), sensors_json)
        + app._metric_values_tuple(metrics)
    ])


app.enqueue_reading = _write_reading_synchronously

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


def enroll(machine):
    """Give `machine` a live agent enrollment.

    Not decoration: resolve_serial_group refuses to merge INTO a hostname that has none,
    because the merge is triggered from the unauthenticated /api/report and an
    unenrolled hostname is an identity claim nobody vouched for. The legitimate rename
    case always has this -- the box that renamed itself is the box already enrolled --
    so a dedup test that omits it is testing a case that cannot happen in the field.
    """
    with app.get_db_conn() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, machine, token_hash, enrolled_at, last_seen, "
            "revoked) VALUES (?, ?, 'h', 0, 0, 0)",
            (f"agent-{machine}", machine),
        )


def make_offline(machine, seconds_ago=None):
    """Backdate a machine's updated_at so derive_machine_status() reads it offline."""
    if seconds_ago is None:
        seconds_ago = settings.get_int(
            app.DB_PATH, "fleet.dashboard_online_window_seconds") + 180
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
    """Insert history rows directly, at timestamps this test picks rather than the ones a
    report would stamp -- see _write_reading_synchronously for the writer itself."""
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
    enroll("OPENCLAW")                     # the renamed box is a real, enrolled machine
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
    enroll("boxNew")
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


def test_merge_survives_identical_readings():
    """Two names for one box reporting the same temperature in the same second.

    readings has a UNIQUE index on (ts_epoch, machine, temp), so re-pointing the dropped
    host's rows onto the survivor collides. This used to raise IntegrityError and abort the
    whole merge -- and it is not an edge case: duplicates ARE one physical machine, usually
    caught mid-rename while both names are still reporting, which is exactly when identical
    readings happen. It only failed intermittently in this suite because it depended on two
    HTTP posts landing in the same wall-clock second; here it is forced.
    """
    print("\n-- merge tolerates readings that collide on the unique index --")
    report("dupKeep", "SER-DUP-1")
    report("dupDrop", "SER-DUP-1")

    ts = int(datetime.now().timestamp())
    with app.get_db_conn() as conn:
        # The colliding pair: same second, same temp, different machine.
        for machine in ("dupKeep", "dupDrop"):
            conn.execute(
                "INSERT OR IGNORE INTO readings(ts_text, ts_epoch, machine, temp, sensors_json) "
                "VALUES (?, ?, ?, ?, NULL)",
                (app.to_timestamp_str(datetime.fromtimestamp(ts)), ts, machine, 42.0),
            )
        # ...and one only the dropped host has, which must survive the merge.
        conn.execute(
            "INSERT OR IGNORE INTO readings(ts_text, ts_epoch, machine, temp, sensors_json) "
            "VALUES (?, ?, ?, ?, NULL)",
            (app.to_timestamp_str(datetime.fromtimestamp(ts - 60)), ts - 60, "dupDrop", 41.0),
        )

    app.merge_machines("dupKeep", "dupDrop")   # must not raise

    check("merge completed", not machine_exists("dupDrop") and machine_exists("dupKeep"))
    with app.get_db_conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) AS c FROM readings WHERE machine='dupDrop'"
        ).fetchone()["c"]
        collided = conn.execute(
            "SELECT COUNT(*) AS c FROM readings WHERE machine='dupKeep' AND ts_epoch=? AND temp=42.0",
            (ts,),
        ).fetchone()["c"]
        unique_row = conn.execute(
            "SELECT COUNT(*) AS c FROM readings WHERE machine='dupKeep' AND ts_epoch=?",
            (ts - 60,),
        ).fetchone()["c"]
    check("no readings left behind under the dropped name", left == 0)
    check("the colliding reading is kept exactly once", collided == 1)
    check("a reading only the dropped host had is preserved", unique_row == 1)


def test_unenrolled_survivor_never_absorbs_a_real_machine():
    """The merge trigger lives on /api/report, which is unauthenticated by design.

    Without a corroboration check that made the dedup a weapon: invent a hostname, claim
    an offline machine's serial, and the merge hands you its identity -- deleting its
    agent enrollment and re-pointing every permission group scoped to it onto the name
    you chose. Enrollment is the one thing an attacker cannot forge without
    AGENT_ENROLLMENT_SECRET, so it is what the merge now requires of its survivor.
    """
    print("\n-- an unenrolled hostname cannot absorb an enrolled machine --")
    report("REAL-PC", "SER-HIJACK-1")
    enroll("REAL-PC")
    make_offline("REAL-PC")                # powered off overnight
    report("EVIL-BOX", "SER-HIJACK-1")     # attacker claims the serial, unauthenticated

    check("the real machine survives", machine_exists("REAL-PC"))
    check("the invented hostname did not absorb it", machine_exists("EVIL-BOX"))
    with app.get_db_conn() as conn:
        agent_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM agents WHERE machine='REAL-PC' AND revoked=0"
        ).fetchone()["c"]
    check("the real machine keeps its enrollment", agent_rows == 1)
    check("resolve reports both as still present",
          set(app.resolve_serial_group("SER-HIJACK-1")) == {"REAL-PC", "EVIL-BOX"})
    # The collision is surfaced rather than swallowed -- an operator has to be able to see
    # that two records claim one serial, whichever of them is lying.
    open_alerts = alerts.list_open(app.DB_PATH)
    check("the refused collision raises a duplicate_serial alert",
          any(a.get("serial_number", "").upper() == "SER-HIJACK-1" for a in open_alerts))


def test_report_cannot_rewrite_an_established_serial():
    """A BIOS serial is immutable hardware identity -- that is why dedup keys on it.

    Letting the open ingress retype one is what lets an attacker MANUFACTURE the
    collision the merge acts on, so a serial is write-once: a blank can be filled (a
    machine's first report describing itself), an established one cannot be replaced.
    """
    print("\n-- /api/report cannot overwrite a serial that is already set --")
    report("SERIAL-PC", "SER-TRUE-1")
    check("first report establishes the serial", _serial("SERIAL-PC") == "SER-TRUE-1")

    report("SERIAL-PC", "SER-ATTACKER-1")  # unauthenticated overwrite attempt
    check("a later report cannot change it", _serial("SERIAL-PC") == "SER-TRUE-1")

    # Other identity fields stay last-write-wins: they legitimately change (a machine is
    # re-tagged, a model string is corrected) and none of them keys the merge.
    client.post("/api/report", json={"machine": "SERIAL-PC", "temp": 42.0,
                                     "asset_tag": "ASSET-2"})
    with app.get_db_conn() as conn:
        row = conn.execute("SELECT asset_tag FROM machine_info WHERE machine='SERIAL-PC'"
                           ).fetchone()
    check("asset tag still updates", row["asset_tag"] == "ASSET-2")


def _report_posture(machine, protector_id="{REC-X}"):
    """Give `machine` a reported BitLocker posture with one recovery-password protector."""
    bitlocker.record_inventory(app.DB_PATH, machine, {
        "support": "supported", "error": "",
        "volumes": [{"mount": "C:", "protection": "on", "conversion": "fully_encrypted",
                     "percentage": 100, "method": "XTS-AES 128",
                     "protectors": [{"id": protector_id, "kind": "recovery_password",
                                     "label": ""}]}],
    })


def _unopenable_blob(machine, sealed_for):
    """File a blob under `machine`'s secret id that was sealed under `sealed_for`'s.

    The secret id is the AAD, so the copy cannot be decrypted -- the state a hub is in after
    BACKUP_MASTER_KEY is replaced without a rewrap, produced here without having to rotate a key
    mid-test.
    """
    backups.store_secret(app.LOG_DIR, backups.load_master_key(),
                         bitlocker.secret_id_for(sealed_for),
                         {"keys": {"{REC-X}": {"recovery_password": "1" * 48, "volume": "C:"}}})
    path = backups.secrets_path(app.LOG_DIR)
    with open(path, encoding="utf-8") as fh:
        store = json.load(fh)
    store[bitlocker.secret_id_for(machine)] = store[bitlocker.secret_id_for(sealed_for)]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh)


def test_merge_refused_rather_than_stranding_recovery_keys():
    """A merge is refused outright when the dropped machine's escrowed BitLocker recovery keys
    cannot be moved with it (roadmap #19).

    The silent failure this catches: the merge reports success, deletes the dropped machine's
    identity row, and leaves its recovery passwords filed under a hostname the console can no
    longer show. **Nothing can re-run it afterwards** -- /api/machines/merge answers 404 for a
    machine with no machine_info row -- so before the first DELETE is the only place to catch it.
    """
    print("\n-- a merge that would strand escrowed recovery keys is refused --")
    report("keyKeep", "SER-ESCROW-1")
    report("keyDrop", "SER-ESCROW-1")
    enroll("keyKeep")
    os.environ["BACKUP_MASTER_KEY"] = backups.generate_master_key()
    try:
        _unopenable_blob("keyDrop", sealed_for="keyKeep")
        _report_posture("keyDrop")

        check("merge_machines reports that it did not merge",
              app.merge_machines("keyKeep", "keyDrop") is False)
        check("...and the dropped machine is still there, so the merge can be retried",
              machine_exists("keyDrop"))
        check("...and its posture was not moved either",
              bitlocker.get_inventory(app.DB_PATH, "keyDrop")["support"] == "supported")
        check("...and its keys are still filed where the index says they are",
              backups.has_secret(app.LOG_DIR, bitlocker.secret_id_for("keyDrop")))
        refusals = fleet.list_audit(app.DB_PATH, action="machine.merge_refused")["entries"]
        check("...and the refusal is audited at security level against the blocking machine",
              any(e["target"] == "keyDrop" and e["level"] == fleet.LEVEL_SECURITY
                  for e in refusals))

        # The console gets a 409 naming the fix, not a 200 that quietly lost custody of a key.
        console_session.sign_in(client, "tester@example.com")
        resp = client.post("/api/machines/merge",
                           json={"survivor": "keyKeep", "victims": ["keyDrop"]})
        body = resp.get_json() or {}
        check("the console endpoint refuses with 409", resp.status_code == 409)
        check("...names what it would not merge", body.get("refused") == ["keyDrop"])
        check("...and says what to restore, because the console shows this text verbatim",
              "BACKUP_MASTER_KEY" in (body.get("error") or ""))
        check("...and the duplicate is still there afterwards",
              machine_exists("keyDrop") and machine_exists("keyKeep"))

        # The survivor's side of the same failure, which a source-only check let straight through.
        # move_escrow unions both machines' key sets, so it opens the SURVIVOR's blob too and
        # refuses when that one will not open -- the shape of a hub whose long-lived machine was
        # escrowed before a master key rotation and whose duplicate appeared after it. Here the
        # dropped machine's own keys are perfectly readable, which is what made it look safe.
        report("destKeep", "SER-ESCROW-2")
        report("destDrop", "SER-ESCROW-2")
        enroll("destKeep")
        _unopenable_blob("destKeep", sealed_for="destDrop")
        _report_posture("destDrop")
        check("a readable dropped machine is still refused when the SURVIVOR's blob will not open",
              app.merge_machines("destKeep", "destDrop") is False)
        check("...and both rows survive, so restoring the key and merging again works",
              machine_exists("destDrop") and machine_exists("destKeep"))
        check("...and the dropped machine's own keys were never touched",
              backups.has_secret(app.LOG_DIR, bitlocker.secret_id_for("destDrop")))
    finally:
        os.environ.pop("BACKUP_MASTER_KEY", None)


def test_partial_merge_stops_the_duplicate_alert_naming_deleted_machines():
    """A manual merge that absorbs some victims and refuses others refreshes the open
    duplicate_serial alert, instead of leaving it listing hostnames it just deleted.

    The silent failure this catches: the operator clicks the duplicate alert, one of three
    victims is refused over its escrowed keys, and the alert -- still open, correctly -- goes on
    offering a merge control for two machines that no longer exist. Nothing corrects it until
    some machine posts a report and re-runs resolve_serial_group, which on an offline duplicate
    is never. The endpoint resolves the alert on a clean merge and did nothing at all on a
    partial one, which is the gap.
    """
    print("\n-- a partial merge refreshes the duplicate alert rather than leaving it stale --")
    for name in ("partKeep", "partGone", "partStay"):
        report(name, "SER-PARTIAL-1")
    enroll("partKeep")
    alerts.upsert_duplicate(app.DB_PATH, "SER-PARTIAL-1", ["partKeep", "partGone", "partStay"])
    os.environ["BACKUP_MASTER_KEY"] = backups.generate_master_key()
    try:
        _unopenable_blob("partStay", sealed_for="partKeep")
        _report_posture("partStay")
        console_session.sign_in(client, "tester@example.com")
        resp = client.post("/api/machines/merge",
                           json={"survivor": "partKeep", "victims": ["partGone", "partStay"]})
        body = resp.get_json() or {}
        check("the endpoint reports a partial merge", resp.status_code == 409
              and body.get("status") == "partial")
        check("...the readable duplicate is gone", not machine_exists("partGone"))
        check("...and the one holding unreadable keys stayed", machine_exists("partStay"))
        open_dupes = [a for a in alerts.list_open(app.DB_PATH)
                      if a["kind"] == alerts.KIND_DUPLICATE_SERIAL
                      and a["serial_number"] == "SER-PARTIAL-1"]
        check("...the alert is still open, because the collision is not collapsed",
              len(open_dupes) == 1)
        check("...and names only the machines that still exist",
              open_dupes and sorted(open_dupes[0]["machines"]) == ["partKeep", "partStay"])
    finally:
        os.environ.pop("BACKUP_MASTER_KEY", None)


def test_escrow_move_failing_mid_merge_leaves_posture_with_the_keys():
    """The residual race: the escrow check passed at the top of the merge, and the store or the
    master key changed before the move ran a few dozen lines later.

    Pinned because a typo in that comparison -- the wrong constant, or a truthiness test -- would
    pass every other test in this suite while renaming the index off the keys, which is the exact
    loss the three-state return exists to prevent. Forced rather than raced, for the same reason
    the identical-readings test above forces its collision.
    """
    print("\n-- an escrow move that fails mid-merge leaves the posture where the keys are --")
    report("raceKeep", "SER-RACE-1")
    report("raceDrop", "SER-RACE-1")
    _report_posture("raceDrop")

    real_move = app.bitlocker_web.move_escrow
    app.bitlocker_web.move_escrow = lambda log_dir, old, new: app.bitlocker_web.ESCROW_FAILED
    try:
        check("the rest of the merge still completes",
              app.merge_machines("raceKeep", "raceDrop") is True)
    finally:
        app.bitlocker_web.move_escrow = real_move

    check("the dropped identity row is gone as in any merge", not machine_exists("raceDrop"))
    check("...but its posture deliberately stays under the old name, with its keys",
          bitlocker.get_inventory(app.DB_PATH, "raceDrop")["support"] == "supported")
    check("...and nothing is claimed under the survivor's name",
          bitlocker.get_inventory(app.DB_PATH, "raceKeep")["support"] is None)
    failed = fleet.list_audit(app.DB_PATH, action="bitlocker_escrow_move_failed")["entries"]
    check("...and it is audited at security level, naming both hostnames",
          any(e["target"] == "raceDrop" and (e["detail"] or {}).get("survivor") == "raceKeep"
              and e["level"] == fleet.LEVEL_SECURITY for e in failed))


def _serial(machine):
    with app.get_db_conn() as conn:
        row = conn.execute(
            "SELECT serial_number FROM machine_info WHERE machine=?", (machine,)
        ).fetchone()
    return row["serial_number"] if row else None


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
    test_merge_survives_identical_readings()
    test_unenrolled_survivor_never_absorbs_a_real_machine()
    test_report_cannot_rewrite_an_established_serial()
    test_merge_refused_rather_than_stranding_recovery_keys()
    test_partial_merge_stops_the_duplicate_alert_naming_deleted_machines()
    test_escrow_move_failing_mid_merge_leaves_posture_with_the_keys()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
