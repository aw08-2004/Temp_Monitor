"""disk_usage.py and disk_usage_web.py (roadmap #27): daily volume points, the fill forecast,
the change summary, retention, and the gate.

Wires the blueprints onto a minimal Flask app, avoiding app.py's OAuth boot -- the same approach
as test_usage_web and test_files_web.

**The silent failures this file exists to catch**, none of which would raise anywhere:

  * **A sensor point overwriting a scan point.** The hub writes a sensor point every hour from
    `/api/report` and a scan point once a day. If the upsert let the hourly one win, the day's
    measured figure would be replaced by whatever the throttle saw last, and the folder rows of
    that day would no longer add up to the volume row beside them.
  * **A forecast that claims a date it has not earned.** Three points, or a flat line, must
    come back as "insufficient" or "not filling", never as a date. An operator ordering a disk
    because of a forecast built on noise is the failure; a missing date costs nothing.
  * **Retention thinning the FRESH end, or deleting a Monday.** The window keeps every day for
    90 days and Mondays after that. Getting the comparison backwards keeps the old dailies
    and thins this month, which looks fine on a test database and breaks every forecast.
  * **PC-3 filing a report about PC-4.** The machine name comes from the bearer token, never
    from the body.
  * **A scope leak.** Folder paths name somebody's documents, so every console route is the
    file browser's gate -- `issue_commands` plus machine scope -- and a `view`-only user or one
    scoped to a different machine gets a 403, not a payload.

And for the full-depth history (disk_history.py), which keeps every folder and file:

  * **A delta applied on top of a scan the hub never saw.** The agent diffs against its own
    previous tree. If the hub missed that upload and applied the next one anyway, every path that
    changed on the missing day would be wrong forever. The base check must say "need full".
  * **A child resurrected by its parent.** A deleted folder lists its children as deleted too;
    a folder deleted and recreated must not bring its old files back on a past-day browse.
  * **Retention that cuts instead of folding.** The value on a day is the newest change before
    it, so a pruned row would erase the size of every file that has not changed since.

Plus the listing pass-through in files.py: a folder's `tree_size` survives ingest, and a
file never gains one.
"""
import functools
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import disk_history
import disk_usage
import files
import fleet
import permissions
import settings
from disk_usage_web import create_disk_usage_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"
SECRET = "hub-enroll-secret"
GB = 1024 ** 3


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


def scan_payload(volume="C:", total=500 * GB, free=200 * GB, scanned_at=None, previous=None,
                 changes=None, large=None):
    return {
        "scanned_at": scanned_at,
        "volumes": [{
            "volume": volume, "fs": "NTFS", "scanned_at": scanned_at,
            "previous_scanned_at": previous, "duration_ms": 4200,
            "total_bytes": total, "free_bytes": free, "used_bytes": total - free,
            "folders": 120000, "files": 900000,
            "changes": changes if changes is not None else [],
            "new_large_files": large if large is not None else [],
        }],
        "skipped": [],
    }


def points(days_back_and_gb, total_gb=500):
    today = date.today()
    return [{"day": (today - timedelta(days=d)).isoformat(), "used_bytes": int(gb * GB),
             "total_bytes": total_gb * GB} for d, gb in days_back_and_gb]


def test_forecast():
    print("\n== The forecast claims a date only when it has earned one ==")
    today = date.today()

    f = disk_usage.forecast(points([(2, 100), (1, 110), (0, 120)]), today=today)
    check("three points are insufficient, with no date", f["status"] == "insufficient"
          and f["full_on"] is None and f["days_to_full"] is None)

    f = disk_usage.forecast(points([(d, 300) for d in range(10)]), today=today)
    check("a flat line is not filling", f["status"] == "not_filling" and f["full_on"] is None)

    f = disk_usage.forecast(points([(d, 300 + d) for d in range(10)]), today=today)
    check("a shrinking volume is not filling", f["status"] == "not_filling")

    # 2 GB a day, 400 used of 500: 50 days to go.
    f = disk_usage.forecast(points([(d, 400 - 2 * d) for d in range(20)]), today=today)
    check("a steady fill gets a date", f["status"] == "filling")
    check("...about fifty days out", f["days_to_full"] is not None
          and 49 <= f["days_to_full"] <= 51)
    check("...at about 2 GB a day", abs(f["bytes_per_day"] - 2 * GB) < 0.01 * GB)
    check("...with high confidence", f["confidence"] == "high")
    check("...and the full_on date matches", f["full_on"] == (today + timedelta(
        days=int(f["days_to_full"]))).isoformat())
    check("the 7-day rate is reported beside it",
          abs(f["bytes_per_day_7d"] - 2 * GB) < 0.01 * GB)

    # Ten MB a day on a nearly empty 2 TB disk: more than ten years away.
    slow = [(d, 100 - d * 0.01) for d in range(20)]
    f = disk_usage.forecast(points(slow, total_gb=2000), today=today)
    check("a date more than ten years away reads as not filling", f["status"] == "not_filling")

    old = points([(d, 400 - 2 * d) for d in range(40, 60)])
    f = disk_usage.forecast(old, today=today)
    check("points older than the window are ignored", f["status"] == "insufficient"
          and f["points"] == 0)

    # A PC that was off for a week: x is the calendar day, so the gap does not double the rate.
    gappy = points([(d, 400 - 2 * d) for d in list(range(0, 5)) + list(range(12, 20))])
    f = disk_usage.forecast(gappy, today=today)
    check("a gap in the days does not distort the rate",
          abs(f["bytes_per_day"] - 2 * GB) < 0.01 * GB)

    jumpy = points([(d, 300 + (40 if d % 2 else 0) - d * 0.2) for d in range(20)])
    f = disk_usage.forecast(jumpy, today=today)
    check("an erratic series is labelled low confidence when it is given a date",
          f["status"] != "filling" or f["confidence"] == "low")


def test_model(db_path):
    print("\n== A scan point always beats a sensor point ==")
    now = int(disk_usage.time.time())
    day = disk_usage.day_of(now)

    disk_usage.record_sensor_volumes(db_path, "PC-1", [
        {"letter": "c", "total_gb": 500, "used_gb": 250}], now=now)
    row = disk_usage.volume_points(db_path, "PC-1", "C:")[-1]
    check("a sensor point is stored for today", row["day"] == day and row["source"] == "sensor"
          and row["used_bytes"] == 250 * GB)

    stored = disk_usage.record_scan(db_path, "PC-1", scan_payload(scanned_at=now), now=now)
    check("a scan is stored", stored == 1)
    row = disk_usage.volume_points(db_path, "PC-1", "C:")[-1]
    check("...and replaces the sensor point", row["source"] == "scan"
          and row["used_bytes"] == 300 * GB)

    disk_usage.record_sensor_volumes(db_path, "PC-1", [
        {"letter": "C", "total_gb": 500, "used_gb": 260}], now=now + 3600)
    row = disk_usage.volume_points(db_path, "PC-1", "C:")[-1]
    check("a later sensor point does NOT overwrite the scan", row["source"] == "scan"
          and row["used_bytes"] == 300 * GB)

    print("\n== Malformed parts are dropped, not stored ==")
    junk = scan_payload(scanned_at=now, changes=[
        {"path": "C:\\Users\\Ann\\Cache", "before": 1 * GB, "after": 6 * GB},
        {"path": "E:\\Elsewhere", "before": 0, "after": 5 * GB},
        {"path": "C:\\Bad\x07Name", "before": 0, "after": 5 * GB},
        {"path": "C:\\Negative", "before": -5, "after": 5 * GB},
        "not-an-object"])
    disk_usage.record_scan(db_path, "PC-1", junk, now=now)
    changes = disk_usage.get_changes(db_path, "PC-1", "C:")
    check("the change list keeps the good row and drops the foreign, odd and negative ones",
          [c["path"] for c in changes["changes"]] == ["C:\\Users\\Ann\\Cache"])
    check("...with its delta worked out", changes["changes"][0]["delta"] == 5 * GB)

    check("a payload that is not a report stores nothing",
          disk_usage.record_scan(db_path, "PC-1", {"volumes": "x"}) == 0
          and disk_usage.record_scan(db_path, "PC-1", None) == 0)

    print("\n== Skipped volumes say why ==")
    disk_usage.record_scan(db_path, "PC-1", {"volumes": [], "skipped": [
        {"volume": "E:", "reason": "filesystem:ReFS"}]}, now=now)
    summary = disk_usage.get_summary(db_path, "PC-1")
    e = [v for v in summary["volumes"] if v["volume"] == "E:"]
    check("a skipped volume appears with its reason",
          e and e[0]["scan"]["status"] == "skipped" and e[0]["scan"]["reason"] == "filesystem:ReFS")

    print("\n== A first scan is not 'nothing changed' ==")
    disk_usage.record_scan(db_path, "PC-9", scan_payload(scanned_at=now, previous=None), now=now)
    first = disk_usage.get_changes(db_path, "PC-9", "C:")
    check("previous_scanned_at is null on a first scan", first["previous_scanned_at"] is None
          and first["changes"] == [])
    none = disk_usage.get_changes(db_path, "PC-NONE", "C:")
    check("a machine with no scans has no day", none["day"] is None and none["days"] == [])


def test_prune(db_path):
    print("\n== Retention thins the old end and never the fresh one ==")
    today = date(2026, 10, 5)  # a Monday
    with disk_usage.get_conn(db_path) as conn:
        conn.execute("DELETE FROM disk_volume_daily WHERE machine = 'PRUNE'")
        for back in range(0, 400):
            day = (today - timedelta(days=back)).isoformat()
            conn.execute("INSERT INTO disk_volume_daily VALUES ('PRUNE', 'C:', ?, 1, 1, 'scan', 0)",
                         (day,))
            conn.execute("INSERT INTO disk_changes VALUES ('PRUNE', 'C:', ?, 0, NULL, '{}')", (day,))
    disk_usage.prune(db_path, 90, 365, today=today)
    with disk_usage.get_conn(db_path) as conn:
        days = [r["day"] for r in conn.execute(
            "SELECT day FROM disk_volume_daily WHERE machine = 'PRUNE' ORDER BY day DESC")]
        change_days = [r["day"] for r in conn.execute(
            "SELECT day FROM disk_changes WHERE machine = 'PRUNE'")]
    recent = [d for d in days if d >= (today - timedelta(days=90)).isoformat()]
    older = [d for d in days if d < (today - timedelta(days=90)).isoformat()]
    check("every one of the last 90 days survives", len(recent) == 91)
    check("older days are Mondays only",
          older and all(date.fromisoformat(d).weekday() == 0 for d in older))
    check("...and every Monday in that range survives",
          len(older) == len([b for b in range(91, 366)
                             if (today - timedelta(days=b)).weekday() == 0]))
    check("nothing older than a year survives",
          min(days) >= (today - timedelta(days=365)).isoformat())
    check("change lists are kept for the daily window only",
          min(change_days) >= (today - timedelta(days=90)).isoformat())


def test_lifecycle(db_path):
    print("\n== Rename and forget ==")
    now = int(disk_usage.time.time())
    disk_usage.record_scan(db_path, "OLD-NAME", scan_payload(scanned_at=now), now=now)
    disk_usage.rename_machine(db_path, "OLD-NAME", "NEW-NAME")
    check("history follows a rename",
          disk_usage.volume_points(db_path, "NEW-NAME", "C:")
          and not disk_usage.volume_points(db_path, "OLD-NAME", "C:"))
    disk_usage.forget_machine(db_path, "NEW-NAME")
    check("forgetting a machine erases its history",
          not disk_usage.volume_points(db_path, "NEW-NAME", "C:")
          and disk_usage.get_summary(db_path, "NEW-NAME")["volumes"] == [])


def test_listing_passthrough(db_path):
    print("\n== Folder sizes survive the listing ingest ==")
    request_id, _ = files.create_listing(db_path, "PC-1", "C:\\Users")
    ok = files.record_listing(db_path, request_id, "PC-1", {
        "path": "C:\\Users",
        "tree_scanned_at": 1_700_000_000,
        "entries": [
            {"name": "Ann", "directory": True, "tree_size": 5 * GB, "tree_allocated": 4 * GB,
             "tree_files": 1200},
            {"name": "notes.txt", "directory": False, "size": 12, "tree_size": 999},
            {"name": "Weird", "directory": True, "tree_size": -1, "surprise": "x"},
        ],
    })
    check("the listing is stored", ok)
    listing = files.get_listing(db_path, request_id, machine="PC-1")
    by_name = {e["name"]: e for e in listing["entries"]}
    check("a folder keeps its tree size", by_name["Ann"]["tree_size"] == 5 * GB
          and by_name["Ann"]["tree_allocated"] == 4 * GB and by_name["Ann"]["tree_files"] == 1200)
    check("...and still has no size of its own", by_name["Ann"]["size"] is None)
    check("a file never gains a tree size", "tree_size" not in by_name["notes.txt"])
    check("a negative tree size is dropped", "tree_size" not in by_name["Weird"])
    check("unknown keys are still dropped", "surprise" not in by_name["Weird"])
    check("the scan time comes through", listing["tree_scanned_at"] == 1_700_000_000)


def delta(lines):
    """A gzip upload body, as the agent's TreeDelta writes one."""
    text = "".join(json.dumps(line) + "\n" for line in lines)
    return gzip.compress(text.encode("utf-8"))


def entry(path, size, alloc=None, files=None, directory=False):
    line = {"p": path, "d": 1 if directory else 0, "s": size,
            "a": size if alloc is None else alloc}
    if directory:
        line["f"] = files or 0
    return line


def send(root, machine, volume, scanned_at, base, full, lines):
    body = delta(lines)
    outcome = disk_history.accept(root, machine, volume, scanned_at, base, full,
                                  io.BytesIO(body), len(body))
    disk_history.process_spool(root)
    return outcome


def at(days_ago, hour=12):
    """A scan timestamp `days_ago` days back, mid-day, so it lands on that calendar day."""
    d = date.today() - timedelta(days=days_ago)
    return int(disk_usage.datetime(d.year, d.month, d.day, hour).timestamp())


def test_history():
    print("\n== Full-depth history: every folder and file ==")
    root = tempfile.mkdtemp(prefix="disk-history-")
    try:
        day3, day2, day1 = at(3), at(2), at(1)
        full = [
            entry("C:\\", 300, directory=True, files=3),
            entry("C:\\Users", 300, directory=True, files=3),
            entry("C:\\Users\\Ann", 300, directory=True, files=3),
            entry("C:\\Users\\Ann\\a.txt", 100),
            entry("C:\\Users\\Ann\\b.txt", 100),
            entry("C:\\Users\\Ann\\Old", 100, directory=True, files=1),
            entry("C:\\Users\\Ann\\Old\\old.bin", 100),
        ]
        check("a delta with no history on the hub is refused as need-full",
              send(root, "PC-1", "C:", day3, 12345, False, full) == "need_full")
        check("a full tree is accepted", send(root, "PC-1", "C:", day3, None, True, full) == "stored")
        check("...and the base for the next delta is that scan",
              disk_history.last_scanned_at(root, "PC-1", "C:") == day3)

        s = disk_history.series(root, "PC-1", "C:", "C:\\Users\\Ann\\a.txt")
        check("a single file has a history", s["known"] and s["directory"] is False
              and [p["size"] for p in s["points"]] == [100])

        # Day 2: a.txt grows, Old is deleted with its file, new.iso appears.
        d2 = [
            entry("C:\\", 1000, directory=True, files=3),
            entry("C:\\Users", 1000, directory=True, files=3),
            entry("C:\\Users\\Ann", 1000, directory=True, files=3),
            entry("C:\\Users\\Ann\\a.txt", 400),
            {"p": "C:\\Users\\Ann\\Old", "x": 1},
            {"p": "C:\\Users\\Ann\\Old\\old.bin", "x": 1},
            entry("C:\\Users\\Ann\\new.iso", 500),
        ]
        check("a delta on the wrong base is need-full",
              send(root, "PC-1", "C:", day2, day3 - 1, False, d2) == "need_full")
        check("a delta on the right base is stored",
              send(root, "PC-1", "C:", day2, day3, False, d2) == "stored")
        check("a retried upload that already landed is a duplicate, not an error",
              send(root, "PC-1", "C:", day2, day3, False, d2) == "duplicate")

        s = disk_history.series(root, "PC-1", "C:", "c:\\users\\ann\\A.TXT")
        check("the file's history has both values, looked up case-insensitively",
              [p["size"] for p in s["points"]] == [100, 400])
        s = disk_history.series(root, "PC-1", "C:", "C:\\Users\\Ann\\b.txt")
        check("an unchanged file keeps one point", [p["size"] for p in s["points"]] == [100])

        changes = disk_history.file_changes(root, "PC-1", "C:")
        by_path = {c["path"]: c for c in changes["files"]}
        check("what changed names files, not only folders",
              set(by_path) == {"C:\\Users\\Ann\\a.txt", "C:\\Users\\Ann\\new.iso",
                               "C:\\Users\\Ann\\Old\\old.bin"})
        check("...biggest change first", changes["files"][0]["path"] == "C:\\Users\\Ann\\new.iso")
        check("...with new, changed and deleted told apart",
              by_path["C:\\Users\\Ann\\new.iso"]["status"] == "new"
              and by_path["C:\\Users\\Ann\\a.txt"]["status"] == "changed"
              and by_path["C:\\Users\\Ann\\a.txt"]["delta"] == 300
              and by_path["C:\\Users\\Ann\\Old\\old.bin"]["status"] == "deleted")
        first = disk_history.file_changes(root, "PC-1", "C:", day=disk_history.day_number(day3))
        check("the first scan is 'first', not a list of every file", first["first"]
              and first["files"] == [])

        past = disk_history.browse(root, "PC-1", "C:", "C:\\Users\\Ann",
                                   disk_history.day_number(day3))
        names = {e["name"]: e for e in past["entries"]}
        check("browsing a past day shows what was there then", past["exists"]
              and set(names) == {"a.txt", "b.txt", "Old"} and names["a.txt"]["size"] == 100)
        now = disk_history.browse(root, "PC-1", "C:", "C:\\Users\\Ann",
                                  disk_history.day_number(day2))
        check("...and today shows today", {e["name"] for e in now["entries"]}
              == {"a.txt", "b.txt", "new.iso"})
        gone = disk_history.browse(root, "PC-1", "C:", "C:\\Users\\Ann\\Old",
                                   disk_history.day_number(day2))
        check("a deleted folder does not exist on a later day", not gone["exists"])

        # Day 1: Old comes back, empty. Its old file must not come back with it.
        d1 = [entry("C:\\Users\\Ann\\Old", 0, directory=True, files=0)]
        send(root, "PC-1", "C:", day1, day2, False, d1)
        back = disk_history.browse(root, "PC-1", "C:", "C:\\Users\\Ann\\Old",
                                   disk_history.day_number(day1))
        check("a recreated folder does not bring its deleted children back",
              back["exists"] and back["entries"] == [])

        # A full tree after a gap marks what it no longer holds as deleted.
        resync = [entry("C:\\", 0, directory=True, files=0)]
        send(root, "PC-1", "C:", at(0), None, True, resync)
        today = disk_history.browse(root, "PC-1", "C:", "C:\\", date.today().toordinal())
        check("a full resync deletes what the tree no longer has",
              today["exists"] and today["entries"] == [])

        print("\n== Unsafe lines are skipped, not stored ==")
        send(root, "PC-2", "C:", day3, None, True, [
            entry("C:\\ok.txt", 1),
            entry("D:\\planted.txt", 1),
            entry("C:\\..\\escape.txt", 1),
            entry("C:\\bad\x07.txt", 1),
            {"p": "C:\\neg.txt", "d": 0, "s": -4, "a": 0},
            "not-an-object",
        ])
        listing = {e["name"] for e in disk_history.browse(
            root, "PC-2", "C:", "C:\\", disk_history.day_number(day3))["entries"]}
        check("only the well-formed line on this volume is stored", listing == {"ok.txt"})
        try:
            disk_history.accept(root, "PC-2", "C:", day2, None, True, io.BytesIO(b""), 0)
            check("an upload with no length is refused", False)
        except ValueError:
            check("an upload with no length is refused", True)

        print("\n== Retention folds, never cuts ==")
        prune_root = tempfile.mkdtemp(prefix="disk-history-prune-")
        try:
            today = date(2026, 10, 5)   # a Monday
            stamp = lambda back: int(disk_usage.datetime.combine(
                today - timedelta(days=back), disk_usage.datetime.min.time()).timestamp()) + 43200
            # A file written once 400 days ago, and one that changes every day.
            send(prune_root, "PC-P", "C:", stamp(400), None, True,
                 [entry("C:\\still.dat", 7), entry("C:\\daily.log", 0)])
            last = stamp(400)
            for back in range(399, 0, -1):
                send(prune_root, "PC-P", "C:", stamp(back), last, False,
                     [entry("C:\\daily.log", 400 - back)])
                last = stamp(back)
            disk_history.prune(prune_root, 90, 365, today=today)
            still = disk_history.series(prune_root, "PC-P", "C:", "C:\\still.dat")
            check("a file unchanged for over a year still has its size",
                  still["known"] and [p["size"] for p in still["points"]] == [7])
            on_day = disk_history.browse(prune_root, "PC-P", "C:", "C:\\", today.toordinal())
            check("...and still shows on today's browse",
                  {e["name"]: e["size"] for e in on_day["entries"]}.get("still.dat") == 7)
            daily = [date.fromisoformat(p["day"]) for p in
                     disk_history.series(prune_root, "PC-P", "C:", "C:\\daily.log")["points"]]
            recent = [d for d in daily if d > today - timedelta(days=90)]
            # The weekly window starts at the Monday on or before the 90-day cutoff, so the
            # few days between that Monday and the cutoff are still daily. Asserted past them.
            older = [d for d in daily if d <= today - timedelta(days=97)]
            check("the last 90 days keep every daily change", len(recent) >= 89)
            check("older changes sit on Mondays only", older
                  and all(d.weekday() == 0 for d in older))
            check("nothing is older than the keep window",
                  min(daily) >= today - timedelta(days=372))
            log_value = disk_history.browse(prune_root, "PC-P", "C:", "C:\\",
                                            (today - timedelta(days=200)).toordinal())
            check("a day in the weekly window reads the value of a nearby Monday",
                  {e["name"]: e["size"] for e in log_value["entries"]}.get("daily.log")
                  in range(190, 210))
        finally:
            shutil.rmtree(prune_root, ignore_errors=True)

        print("\n== Rename and forget ==")
        disk_history.rename_machine(root, "PC-2", "PC-2B")
        check("history follows a rename", disk_history.volumes(root, "PC-2B") == ["C:"]
              and disk_history.volumes(root, "PC-2") == [])
        disk_history.forget_machine(root, "PC-2B")
        check("forgetting a machine erases its history", disk_history.volumes(root, "PC-2B") == [])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_web(db_path):
    global CURRENT_USER
    print("\n== The HTTP surface ==")
    app = Flask(__name__)
    app.secret_key = "test"
    access = create_access(db_path, {"super@x.com"})
    permissions.create_group(db_path, "Viewers", capabilities=[permissions.VIEW],
                             machines=["PC-1"], members=["viewer@x.com"])
    permissions.create_group(db_path, "Techs", capabilities=[permissions.VIEW,
                                                             permissions.ISSUE_COMMANDS],
                             machines=["PC-2"], members=["tech@x.com"])
    settings.invalidate()
    history_root = tempfile.mkdtemp(prefix="disk-history-web-")
    app.register_blueprint(create_disk_usage_blueprint(db_path, history_root,
                                                       fake_login_required, access))

    @app.before_request
    def _seed_session():
        flask_session["user"] = {"email": CURRENT_USER}

    c = app.test_client()
    pc1_id, pc1_token = fleet.enroll_agent(db_path, "PC-1", SECRET, SECRET)
    pc4_id, pc4_token = fleet.enroll_agent(db_path, "PC-4", SECRET, SECRET)
    pc1 = {"Authorization": f"Bearer {pc1_id}:{pc1_token}"}
    pc4 = {"Authorization": f"Bearer {pc4_id}:{pc4_token}"}

    now = int(disk_usage.time.time())
    r = c.post("/api/agent/disk-usage", json=scan_payload(volume="D:", scanned_at=now))
    check("an unauthenticated report is refused -> 401", r.status_code == 401)
    r = c.post("/api/agent/disk-usage", headers=pc4, json={"machine": "PC-1",
                                                           **scan_payload(volume="D:",
                                                                          scanned_at=now)})
    check("an authenticated report is stored -> 200", r.status_code == 200)
    check("...under the CALLING machine, whatever the body says",
          disk_usage.volume_points(db_path, "PC-4", "D:")
          and not disk_usage.volume_points(db_path, "PC-1", "D:"))
    r = c.post("/api/agent/disk-usage", headers=pc1, json={"nope": 1})
    check("a body that is not a report -> 400", r.status_code == 400)

    CURRENT_USER = "super@x.com"
    r = c.get("/api/disk-usage/machines/PC-1")
    check("a superuser reads the summary", r.status_code == 200
          and any(v["volume"] == "C:" for v in r.get_json()["volumes"]))
    r = c.get("/api/disk-usage/machines/PC-1/history?volume=c")
    body = r.get_json()
    check("history takes a lower-case letter", r.status_code == 200 and body["volume"] == "C:"
          and body["points"] and "forecast" in body)
    check("history refuses a volume that is not a letter -> 400",
          c.get("/api/disk-usage/machines/PC-1/history?volume=..\\x").status_code == 400)
    r = c.get("/api/disk-usage/machines/PC-1/changes?volume=C:")
    check("changes are readable", r.status_code == 200 and r.get_json()["changes"])

    print("\n== The history upload ==")
    body = delta([entry("C:\\", 10, directory=True, files=1), entry("C:\\x.txt", 10)])
    url = f"/api/agent/disk-usage/history?volume=C:&scanned_at={now}"
    r = c.post(url + "&full=1", data=body, content_type="application/gzip")
    check("an unauthenticated history upload is refused -> 401", r.status_code == 401)
    r = c.post(url + "&base=1", data=body, content_type="application/gzip", headers=pc1)
    check("a delta the hub has no base for -> 409 need_full",
          r.status_code == 409 and r.get_json()["need_full"])
    r = c.post(url + "&full=1", data=body, content_type="application/gzip", headers=pc1)
    check("a full tree -> 200", r.status_code == 200)
    disk_history.process_spool(history_root)
    check("...filed under the CALLING machine", disk_history.volumes(history_root, "PC-1") == ["C:"])
    r = c.post("/api/agent/disk-usage/history?volume=..&scanned_at=1&full=1", data=body,
               content_type="application/gzip", headers=pc1)
    check("a volume that is not a letter -> 400", r.status_code == 400)

    r = c.get("/api/disk-usage/machines/PC-1/path-history?volume=C:&path=C:%5Cx.txt")
    check("any file's history is readable", r.status_code == 200
          and [p["size"] for p in r.get_json()["points"]] == [10])
    day = date.today().isoformat()
    r = c.get(f"/api/disk-usage/machines/PC-1/browse?volume=C:&path=C:%5C&day={day}")
    check("a past day can be browsed", r.status_code == 200
          and [e["name"] for e in r.get_json()["entries"]] == ["x.txt"])
    check("browse refuses a day that is not a date -> 400",
          c.get("/api/disk-usage/machines/PC-1/browse?volume=C:&day=soon").status_code == 400)
    r = c.get("/api/disk-usage/machines/PC-1/changes?volume=C:")
    check("the change list carries the file changes too",
          r.status_code == 200 and "file_changes" in r.get_json())
    r = c.get("/api/disk-usage/machines/PC-1/history?volume=C:")
    check("history lists the days a past state can be shown for",
          r.get_json()["history_days"] and r.get_json()["history_days"][0]["day"] == day)
    r = c.get("/api/disk-usage/machines/PC-1/history?volume=C:&days=99999")
    check("an absurd day count is capped, not refused", r.status_code == 200)

    print("\n== Scan now ==")
    r = c.post("/api/disk-usage/machines/PC-1/scan", data="x", content_type="text/plain")
    check("a non-JSON scan request is refused -> 415", r.status_code == 415)
    r = c.post("/api/disk-usage/machines/PC-1/scan", json={})
    check("Scan now queues a command -> 201", r.status_code == 201 and r.get_json()["command_id"])
    queued = fleet.get_command(db_path, r.get_json()["command_id"])
    check("...of the scan_disk_usage type", queued and queued["type"] == "scan_disk_usage")
    check("the type is a known command", "scan_disk_usage" in fleet.ALL_COMMANDS)
    check("...and not a file command, which the command channel refuses",
          "scan_disk_usage" not in fleet.FILE_COMMANDS)

    print("\n== The gate is the file browser's ==")
    CURRENT_USER = "viewer@x.com"
    for url in ("/api/disk-usage/machines/PC-1",
                "/api/disk-usage/machines/PC-1/history?volume=C:",
                "/api/disk-usage/machines/PC-1/changes?volume=C:",
                "/api/disk-usage/machines/PC-1/path-history?volume=C:&path=C:%5Cx.txt",
                f"/api/disk-usage/machines/PC-1/browse?volume=C:&path=C:%5C&day={day}"):
        check(f"a view-only user is refused {url} -> 403", c.get(url).status_code == 403)
    check("...and cannot press Scan now -> 403",
          c.post("/api/disk-usage/machines/PC-1/scan", json={}).status_code == 403)
    CURRENT_USER = "tech@x.com"
    check("a tech scoped to PC-2 is refused PC-1 -> 403",
          c.get("/api/disk-usage/machines/PC-1").status_code == 403)
    check("...but reads PC-2 -> 200",
          c.get("/api/disk-usage/machines/PC-2").status_code == 200)
    CURRENT_USER = "super@x.com"
    shutil.rmtree(history_root, ignore_errors=True)


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        files.init_files_db(db_path)
        disk_usage.init_disk_usage_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()

        test_forecast()
        test_model(db_path)
        test_prune(db_path)
        test_lifecycle(db_path)
        test_history()
        test_listing_passthrough(db_path)
        test_web(db_path)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
