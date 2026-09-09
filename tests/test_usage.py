"""usage.py -- the per-app foreground ledger (roadmap #23 phase E).

**The silent failure this file exists to catch is an erased history.** The app inventory is
REPLACED on every report, because an uninstalled app must disappear. Usage is the opposite and
the difference is easy to get wrong by copying the neighbour: a device reports TODAY on every
pass, so replacing the machine's rows would wipe every earlier day the moment a phone came back
from a week in a drawer with only today in hand. Nothing about that is visible -- the console
shows one day, which is exactly what a device that was off for a week looks like.

The second is the day key. A day here is the DEVICE's own local day, an ISO string, and it is a
primary key, an ordering and a retention decision all at once. A row keyed on something that is
not a date ("yesterday", an epoch, a localised format) sorts wrongly, prunes never, and merges
with nothing -- so a day that will not parse is dropped rather than stored.

The third is the retention prune, which is the whole privacy argument made real. This is a
record of what somebody did with their evenings; it has to actually go away, and it is pruned by
comparing the DEVICE's day strings rather than by a hub timestamp -- a device in a timezone
ahead of the hub would otherwise have its current day pruned out from under it.

And the fourth is the merge within a day: a later report for the same day is a larger number,
because usage only grows. A smaller one means the device's own accounting reset, which the agent
already guards with a high-water mark -- so the newest figure wins here rather than the largest,
and a rename that collides keeps the larger of the two.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import usage

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


def report(days):
    return {"days": days}


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        usage.init_usage_db(db_path)
        usage.init_usage_db(db_path)      # idempotent, like every other init_*_db
        check("init_usage_db can run twice", True)

        print("== Recording ==")
        written = usage.record_usage(db_path, "PHONE-1", report({
            "2026-09-07": {"com.zhiliaoapp.musically": 3600, "com.android.chrome": 600},
        }), now=1000)
        check("a day's totals are stored", written == 2)
        check("...and read back per day",
              usage.day_totals(db_path, "PHONE-1", "2026-09-07")
              == {"com.zhiliaoapp.musically": 3600, "com.android.chrome": 600})

        print("\n== A later report MERGES rather than replacing ==")
        # The assertion this file exists for. The device sends only today; yesterday must
        # survive it.
        usage.record_usage(db_path, "PHONE-1", report({
            "2026-09-08": {"com.zhiliaoapp.musically": 120},
        }), now=2000)
        days = [d["day"] for d in usage.days_for(db_path, "PHONE-1")]
        check("today's report does not erase yesterday", days == ["2026-09-08", "2026-09-07"])

        print("\n== Within a day, the newest figure wins ==")
        usage.record_usage(db_path, "PHONE-1", report({
            "2026-09-08": {"com.zhiliaoapp.musically": 900},
        }), now=3000)
        check("a later, larger figure for the same day replaces the earlier one",
              usage.day_totals(db_path, "PHONE-1", "2026-09-08")
              == {"com.zhiliaoapp.musically": 900})

        print("\n== A day that is not a date is dropped ==")
        for bad in ("yesterday", "2026-9-8", "08-09-2026", "1757280000", ""):
            before = len(usage.days_for(db_path, "PHONE-1", limit=365))
            usage.record_usage(db_path, "PHONE-1",
                               report({bad: {"com.example": 60}}), now=4000)
            check(f"a day key of {bad!r} stores nothing",
                  len(usage.days_for(db_path, "PHONE-1", limit=365)) == before)

        print("\n== A malformed report is not an empty one ==")
        for junk in (None, "days", [], {"days": None}, {"days": []}, {"days": {"x": 1}}):
            usage.record_usage(db_path, "PHONE-1", junk, now=4100)
        check("...and none of it disturbed what was stored",
              len(usage.days_for(db_path, "PHONE-1", limit=365)) == 2)

        print("\n== Shapes the console renders ==")
        usage.record_usage(db_path, "PHONE-2", report({
            "2026-09-08": {"com.a": 100, "com.b": 300},
            "2026-09-07": {"com.a": 50},
        }), now=5000)
        rows = usage.days_for(db_path, "PHONE-2")
        check("days come back newest first", [r["day"] for r in rows]
              == ["2026-09-08", "2026-09-07"])
        check("...with a total per day", rows[0]["total"] == 400)
        check("...and the packages biggest first",
              [p["package"] for p in rows[0]["packages"]] == ["com.b", "com.a"])
        totals = usage.totals_for(db_path, "PHONE-2", days=7)
        check("totals sum across days, biggest first",
              totals == [{"package": "com.b", "seconds": 300},
                         {"package": "com.a", "seconds": 150}])

        payload = usage.get_usage(db_path, "PHONE-2")
        check("get_usage carries when the device last reported",
              payload["reported_at"] == 5000)
        check("a device that never reported reads as 'not told', not as zero",
              usage.get_usage(db_path, "NEVER")["reported_at"] is None
              and usage.get_usage(db_path, "NEVER")["days"] == [])

        print("\n== Retention ==")
        usage.record_usage(db_path, "OLD", report({
            "2026-08-01": {"com.a": 60},
            "2026-09-07": {"com.a": 60},
            "2026-09-08": {"com.a": 60},
        }), now=6000)
        removed = usage.prune(db_path, "2026-09-07")
        check("pruning removes days before the cutoff", removed >= 1)
        check("...and keeps the cutoff day itself",
              [d["day"] for d in usage.days_for(db_path, "OLD", limit=365)]
              == ["2026-09-08", "2026-09-07"])

        print("\n== Lifecycle ==")
        usage.forget_machine(db_path, "PHONE-1")
        check("forgetting a machine erases its usage",
              usage.days_for(db_path, "PHONE-1", limit=365) == [])

        usage.record_usage(db_path, "RENAME-OLD", report({
            "2026-09-08": {"com.a": 100},
        }), now=7000)
        usage.rename_machine(db_path, "RENAME-OLD", "RENAME-NEW")
        check("a rename carries the ledger with it",
              usage.day_totals(db_path, "RENAME-NEW", "2026-09-08") == {"com.a": 100})
        check("...and leaves nothing behind under the old name",
              usage.days_for(db_path, "RENAME-OLD", limit=365) == [])

        # A rename onto a name that already has rows for the same day and package. Keeping the
        # LARGER is the only answer that cannot lose time somebody actually spent.
        usage.record_usage(db_path, "MERGE-OLD", report({"2026-09-08": {"com.a": 500}}),
                           now=7100)
        usage.record_usage(db_path, "MERGE-NEW", report({"2026-09-08": {"com.a": 200}}),
                           now=7200)
        usage.rename_machine(db_path, "MERGE-OLD", "MERGE-NEW")
        check("a colliding rename keeps the larger figure",
              usage.day_totals(db_path, "MERGE-NEW", "2026-09-08") == {"com.a": 500})

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
