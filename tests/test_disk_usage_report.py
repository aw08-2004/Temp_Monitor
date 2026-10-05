"""The disk-usage daily point written from /api/report (roadmap #27).

**The silent failure this file exists to catch is a backfill filed as today.** An agent that
reconnects after days offline replays its buffered reports, each stamped with the `client_ts` it
was taken at. The hub writes one disk-usage point per volume per day from LIVE reports only;
a replayed one describes a past disk, and filing it under today would put last week's used
space on today's point of the forecast.

The trap is that /api/report nulls an out-of-range `client_ts` (older than
`data.ingest_max_backdate_days`) before the disk-usage code runs. Testing the nulled value made
the OLDEST backfills read as live -- the opposite of the intent, and invisible unless a test
posts one. Found in review on PR #108.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-disk-report-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app
import disk_usage

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


def report(machine, client_ts=None, used_gb=100.0):
    body = {
        "machine": machine, "temp": 40.0,
        "sensors": [
            {"hardware": "C:", "hardware_id": "/volume/c", "type": "Data",
             "name": "Total Space", "value": 500.0},
            {"hardware": "C:", "hardware_id": "/volume/c", "type": "Data",
             "name": "Used Space", "value": used_gb},
        ],
    }
    if client_ts is not None:
        body["client_ts"] = client_ts
    return client.post("/api/report", json=body)


def points(machine):
    return disk_usage.volume_points(app.DB_PATH, machine, "C:")


def main():
    now = int(time.time())

    print("== A live report writes today's point ==")
    check("a live report is accepted", report("LIVE-PC", client_ts=now).status_code == 200)
    check("...and writes a disk point for today",
          [p["day"] for p in points("LIVE-PC")] == [disk_usage.day_of(now)])

    print("\n== A report with no stamp at all still counts as live ==")
    report("OLD-AGENT")
    check("an agent too old to send client_ts gets a point", len(points("OLD-AGENT")) == 1)

    print("\n== Backfills write nothing ==")
    report("RECENT-BACKFILL", client_ts=now - 3600)
    check("a backfill from an hour ago writes no point", points("RECENT-BACKFILL") == [])

    max_days = app.settings.get_int(app.DB_PATH, "data.ingest_max_backdate_days")
    stale = now - (max_days + 5) * 86400
    check("a backfill past the backdate window is still accepted",
          report("STALE-BACKFILL", client_ts=stale).status_code == 200)
    check("...and writes no point either, though its stamp was discarded",
          points("STALE-BACKFILL") == [])

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
