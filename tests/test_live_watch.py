"""live.py -- the watch that decides how fast a machine reports.

What this feature costs is entirely decided by the watch, so that is what is tested:

  * **It must LAPSE on its own.** A watched machine reports twelve times as often, with a
    sensor block on every report. Nothing sends an unsubscribe -- a closed tab, a killed
    browser and a sleeping laptop all just stop pinging -- so an expiry that is a real
    expiry, and not a flag somebody is trusted to clear, is the only thing standing between
    "an operator looked at this once" and that machine reporting at 1 Hz forever.

  * **It must survive a missed ping.** The console renews every POLL_INTERVAL_SECONDS and
    the TTL has to outlast a slow request or a briefly-backgrounded tab, or the charts drop
    back to five-second steps in the middle of somebody reading them.

  * **The cadence numbers are one design, not three.** The TTL has to be a comfortable
    multiple of the poll interval, and the fast interval has to actually be faster than the
    ordinary one -- these are asserted here because the browser, the hub and the agent each
    hold a copy of part of that design.

The field names here are the other half of a contract the agent asserts in C#
(Telemetry/LiveTelemetry, FleetClient.PollWatchAsync). Drift is not a crash: it is a page
that quietly never speeds up.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import live

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


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        live.init_live_db(db_path)
        live.init_live_db(db_path)   # idempotent, like every other init_*_db

        # ================================================================ the cadences
        print("\n== The cadence numbers have to agree with each other ==")
        check("the fast interval is actually faster than a normal report",
              live.FAST_INTERVAL_SECONDS < 5)
        check("...and is at least a second -- a sensor read is not free",
              live.FAST_INTERVAL_SECONDS >= 1)
        check("the TTL outlasts several pings, so one slow request costs nothing",
              live.WATCH_TTL_SECONDS >= 3 * live.POLL_INTERVAL_SECONDS)
        check("...but still lets a machine slow down within half a minute of the tab closing",
              live.WATCH_TTL_SECONDS <= 30)

        # ================================================================ the watch
        print("\n== Nothing sends an unsubscribe, so the watch has to lapse by itself ==")
        check("nobody is watching by default",
              live.is_watched(db_path, "PC-1", now=1000) is False)
        live.note_watch(db_path, "PC-1", watcher="tech@x.com", now=1000)
        check("opening the machine page turns it on", live.is_watched(db_path, "PC-1", now=1000))
        check("...and it survives a missed ping",
              live.is_watched(db_path, "PC-1",
                              now=1000 + live.POLL_INTERVAL_SECONDS * 2))
        check("...but LAPSES -- a closed tab never gets to say goodbye",
              live.is_watched(db_path, "PC-1",
                              now=1000 + live.WATCH_TTL_SECONDS + 1) is False)
        live.note_watch(db_path, "PC-1", now=2000)
        check("pinging again renews it", live.is_watched(db_path, "PC-1", now=2000 + 10))
        check("one machine's watch says nothing about another",
              live.is_watched(db_path, "PC-2", now=2000 + 10) is False)
        live.clear_watch(db_path, "PC-1")
        check("and it can be dropped outright",
              live.is_watched(db_path, "PC-1", now=2000 + 10) is False)

        # A second operator opening the same page renews the one row rather than adding a
        # second: two people watching a PC want one cadence, not two.
        live.note_watch(db_path, "PC-3", watcher="a@x.com", now=3000)
        live.note_watch(db_path, "PC-3", watcher="b@x.com", now=3000)
        with live.get_conn(db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) c FROM live_watch WHERE machine = 'PC-3'"
                                ).fetchone()["c"]
        check("two operators on the same machine are one watch", rows == 1)

        # ================================================================ housekeeping
        print("\n== A hub up for months should not carry a row per machine ever opened ==")
        live.note_watch(db_path, "PC-OLD", now=100)
        live.note_watch(db_path, "PC-NEW", now=100000)
        check("pruning drops the lapsed row", live.prune_watches(db_path, now=100000) >= 1)
        check("...and leaves the live one alone", live.is_watched(db_path, "PC-NEW", now=100000))

        # ================================================================ deletion
        print("\n== A deleted machine leaves nothing behind ==")
        live.note_watch(db_path, "PC-GONE")
        live.forget_machine(db_path, "PC-GONE")
        check("its watch is gone", not live.is_watched(db_path, "PC-GONE"))

        # ================================================================ junk
        print("\n== Nothing a caller passes may raise ==")
        live.note_watch(db_path, "")
        check("an empty machine name is not a watch", live.is_watched(db_path, "") is False)
        check("...and neither is None", live.is_watched(db_path, None) is False)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
