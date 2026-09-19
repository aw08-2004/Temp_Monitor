"""events.py -- Windows event log mining (roadmap #16).

**The silent failure this file exists to catch is a fleet that has stopped collecting while
the console still looks fine.** Three separate mistakes produce exactly that page, and none
of them raises anything:

  * A truthiness check on the heartbeat payload. The report from a HEALTHY machine is an
    empty list, and `if payload` would discard it -- so the state row would never be written,
    every machine would read as "never reported", and the one signal that tells a stopped
    collector from a quiet fleet would be the one thing that could never arrive. The ingest
    tests below assert that an empty report is stored as a report.
  * An empty subscription document read as "nothing to send". Deleting the last subscription
    must reach the agent as an instruction to stop; `document()` therefore returns an empty
    list with a version, and that version must differ from the one a populated set hashed to.
  * A scope allow-list of `[]` read as "no filter". `list_events(machines=[])` must return
    nothing, not the fleet -- an empty scope read as unfiltered is how a scoped operator is
    shown every machine in the building.

The second thing asserted is the roll-up, because it is the one place this module
deliberately loses information: repeats of an identical record inside the window increment a
count instead of inserting a row, and two records that differ in ANY field -- including the
message, which is what separates one account's failed logon from another's -- must not
collapse into each other.

The third is the caps. The table is the only one in the hub that a single over-broad
subscription can grow by thousands of rows an hour, so both bounds are pinned here: the
per-report cap (which must count what it dropped rather than silently truncating) and the
per-machine row cap (which must drop the OLDEST rows, not the newest).
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import events

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


def record(event_id=4625, **overrides):
    payload = {"log": "Security", "provider": "Microsoft-Windows-Security-Auditing",
               "event_id": event_id, "level": "information",
               "message": "An account failed to log on. Account Name: alex",
               "occurred_at": int(time.time())}
    payload.update(overrides)
    return payload


def report(*entries, **extra):
    body = {"events": list(entries)}
    body.update(extra)
    return body


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        events.init_events_db(db_path)
        events.init_events_db(db_path)      # idempotent: called on every hub start

        print("== Subscriptions ==")
        sub = events.create_subscription(
            db_path, name="Failed logons", log="Security", event_ids=[4625, 4740],
            levels=["error", "information"], created_by="alex@example.com")
        check("stores what it was given", sub["log"] == "Security"
              and sub["event_ids"] == [4625, 4740])
        check("levels come back in severity order, not typing order",
              sub["levels"] == ["error", "information"])
        check("a new subscription is enabled", sub["enabled"] is True)

        try:
            events.create_subscription(db_path, name="Bad", log="Security",
                                       event_ids=[70000])
            check("an out-of-range event id is refused", False)
        except events.SubscriptionRejected:
            check("an out-of-range event id is refused", True)
        try:
            events.create_subscription(db_path, name="Bad", log="Security",
                                       levels=["catastrophic"])
            check("an unknown level is refused", False)
        except events.SubscriptionRejected:
            check("an unknown level is refused", True)
        try:
            events.create_subscription(db_path, name="  ", log="Security")
            check("a nameless subscription is refused", False)
        except events.SubscriptionRejected:
            check("a nameless subscription is refused", True)

        wide = events.create_subscription(db_path, name="Everything", log="System")
        check("no ids and no levels is a legal subscription (the whole channel)",
              wide["event_ids"] == [] and wide["levels"] == [])

        print("\n== The document the agent receives ==")
        full = events.document(db_path)
        check("only the matching fields travel",
              set(full["subscriptions"][0]) == {"id", "log", "event_ids", "levels", "provider"})
        check("both enabled subscriptions are in it", len(full["subscriptions"]) == 2)

        renamed = events.update_subscription(db_path, sub["id"], name="Logon failures")
        check("a rename does not change the version",
              events.document(db_path)["version"] == full["version"])
        check("...but it did rename it", renamed["name"] == "Logon failures")

        events.update_subscription(db_path, wide["id"], enabled=False)
        narrowed = events.document(db_path)
        check("disabling one drops it from the document",
              len(narrowed["subscriptions"]) == 1)
        check("...and changes the version", narrowed["version"] != full["version"])

        events.update_subscription(db_path, sub["id"], enabled=False)
        emptied = events.document(db_path)
        # THE RELEASE VALVE. An empty document must be a document -- see the module
        # docstring. If this ever returns None, or hashes to the same version as a populated
        # set, an agent goes on collecting the Security channel forever.
        check("disabling the last one yields an EMPTY document, not an absent one",
              emptied["subscriptions"] == [])
        check("...with a version of its own", emptied["version"]
              not in (full["version"], narrowed["version"]))
        events.update_subscription(db_path, sub["id"], enabled=True)
        check("re-enabling hashes back to where it was",
              events.document(db_path)["version"] == narrowed["version"])

        print("\n== Ingest: the empty report is the important one ==")
        stored, rolled = events.record_events(db_path, "PC-1", report())
        check("an empty report stores no rows", (stored, rolled) == (0, 0))
        state = events.machine_state(db_path, "PC-1")
        check("...but IS recorded as a report", state["reported_at"] is not None)
        check("a machine that never reported reads as 'not been told'",
              events.machine_state(db_path, "PC-2")["reported_at"] is None)
        check("a machine that reported is in scope even with no rows",
              events.known_machines(db_path) == ["PC-1"])

        print("\n== Ingest: roll-up ==")
        now = int(time.time())
        events.record_events(db_path, "PC-1", report(record(occurred_at=now)))
        events.record_events(db_path, "PC-1", report(record(occurred_at=now + 1)))
        events.record_events(db_path, "PC-1", report(record(occurred_at=now + 2)))
        rows = events.list_events(db_path, machine="PC-1")
        check("three identical records are one row", len(rows) == 1)
        check("...counted, not lost", rows[0]["count"] == 3)
        check("...spanning first to last", rows[0]["first_seen"] == now
              and rows[0]["last_seen"] == now + 2)

        events.record_events(db_path, "PC-1", report(record(
            occurred_at=now + 3,
            message="An account failed to log on. Account Name: sam")))
        rows = events.list_events(db_path, machine="PC-1")
        # The conservative direction: a different message is a different row, so the page can
        # still answer "which account". A looser key would merge the two and lose the answer.
        check("a different message is a different row", len(rows) == 2)

        # Backdated rather than post-dated, because a timestamp in the future is clamped to
        # arrival (see _normalize) and would test the clamp instead of the window. Backdating
        # is also the real case: a machine back from a day offline reports a day of records.
        events.record_events(db_path, "PC-1", report(record(occurred_at=now - ROLLUP_PAST)))
        rows = events.list_events(db_path, machine="PC-1", event_id=4625)
        check("an identical record outside the window starts a new row", len(rows) == 3)
        alex = [r for r in rows if "alex" in r["message"]]
        check("...and does NOT drag the recent row's window backwards",
              all(r["first_seen"] >= now or r["count"] == 1 for r in alex))

        print("\n== Ingest: what a malformed entry costs ==")
        stored, _ = events.record_events(db_path, "PC-3", report(
            "not a dict", {"log": "System"}, record(event_id=1000, log="System"),
            {"event_id": 6, "log": ""}))
        check("one good entry survives three bad ones", stored == 1)

        far_future = int(time.time()) + 86400
        events.record_events(db_path, "PC-4", report(record(occurred_at=far_future)))
        row = events.list_events(db_path, machine="PC-4")[0]
        check("a timestamp from the future falls back to arrival, not stored as read",
              row["last_seen"] < far_future)

        events.record_events(db_path, "PC-4", report(record(
            event_id=1, level="apocalyptic", message="unclassified")))
        row = events.list_events(db_path, machine="PC-4", event_id=1)[0]
        check("an unreadable level defaults to information, not to critical",
              row["level"] == events.LEVEL_INFORMATION)

        print("\n== Caps ==")
        flood = [record(event_id=100 + n, occurred_at=now + n)
                 for n in range(events.MAX_EVENTS_PER_REPORT + 25)]
        stored, _ = events.record_events(db_path, "PC-5", report(*flood))
        check("a report over the cap is truncated by the hub too",
              stored == events.MAX_EVENTS_PER_REPORT)
        check("...and the overflow is counted rather than silently dropped",
              events.machine_state(db_path, "PC-5")["dropped"] == 25)

        check("the agent's own dropped count is added, not replaced",
              events.record_events(db_path, "PC-5", report(dropped=7)) == (0, 0)
              and events.machine_state(db_path, "PC-5")["dropped"] == 32)

        # The per-machine cap, with a small cap rather than the real 5000 so the test stays
        # fast. What is asserted is the DIRECTION: the oldest events go.
        for n in range(6):
            events.record_events(db_path, "PC-6", report(record(
                event_id=200 + n, occurred_at=now + n)))
        dropped = events.enforce_machine_cap(db_path, "PC-6", cap=3)
        kept = {row["event_id"] for row in events.list_events(db_path, machine="PC-6")}
        check("the per-machine cap drops the excess", dropped == 3 and len(kept) == 3)
        check("...and it is the OLDEST that go", kept == {203, 204, 205})

        print("\n== Reads and scope ==")
        # The inversion this catches: `if machines:` instead of `if machines is not None:`
        # turns "this operator can see nothing" into "no filter", which is every machine in
        # the building.
        check("an empty scope returns nothing, not everything",
              events.list_events(db_path, machines=[]) == [])
        check("None means unscoped", len(events.list_events(db_path, machines=None)) > 0)
        check("a scope narrows to its own machines",
              {row["machine"] for row in events.list_events(db_path, machines=["PC-6"])}
              == {"PC-6"})
        check("an empty scope returns an empty summary too",
              events.summary(db_path, machines=[])["occurrences"] == 0)

        check("search matches the message",
              all("sam" in row["message"]
                  for row in events.list_events(db_path, search="sam")))
        check("a wildcard in the search is a literal, not a match-everything",
              events.list_events(db_path, search="%") == [])

        print("\n== Summary ==")
        digest = events.summary(db_path, window_seconds=86400)
        check("occurrences sums counts rather than counting rows",
              digest["occurrences"] > digest["rows"])
        check("top_events is ranked and bounded", len(digest["top_events"]) <= 10)
        check("by_level is keyed on the slugs",
              set(digest["by_level"]) <= set(events.LEVELS))

        print("\n== Retention ==")
        old = int(time.time()) - 90 * 86400
        events.record_events(db_path, "PC-7", report(record(event_id=999, occurred_at=old)))
        gone = events.prune(db_path, int(time.time()) - 30 * 86400)
        check("a record past the cutoff is pruned", gone == 1)
        check("...and the recent ones are not",
              len(events.list_events(db_path, machine="PC-1")) > 0)

        print("\n== Deleting a subscription keeps its evidence ==")
        before = len(events.list_events(db_path, machine="PC-1"))
        check("delete reports what it did", events.delete_subscription(db_path, sub["id"]))
        check("deleting one that is gone is not an error",
              events.delete_subscription(db_path, sub["id"]) is False)
        check("the events it collected are still there",
              len(events.list_events(db_path, machine="PC-1")) == before)
    finally:
        os.unlink(db_path)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


#: Far enough past ROLLUP_WINDOW_SECONDS that an identical record is a new row rather than a
#: repeat. Named rather than inlined so the assertion above reads as "outside the window"
#: instead of as an arbitrary number of seconds.
ROLLUP_PAST = events.ROLLUP_WINDOW_SECONDS + 60


if __name__ == "__main__":
    sys.exit(main())
