"""location.py -- on-demand device location (roadmap #23 phase B).

**The silent failure this file exists to catch is a position that is wrong in a way nothing
looks wrong about.** Every other failure mode here announces itself: a device that cannot get a
fix says so, a command that never lands expires, a refused permission comes back with a reason.
But a sign dropped off a longitude, a stale fix rendered as a current one, or a zero-metre
accuracy circle all produce a map with a confident marker on it -- and somebody drives there.

So the assertions are about the ways a coordinate can be quietly mangled:

  * **Zero is a real coordinate.** Any check that treats a latitude with `if lat:` throws away
    the equator, and (0, 0) in the Gulf of Guinea is the classic symptom of that mistake made
    upstream. The parse uses explicit None checks and this file holds it to that.
  * **Negative coordinates must survive.** This fleet is in Paraguay: both axes are negative,
    and a sign lost anywhere puts every device in Kazakhstan with nothing looking wrong.
  * **`stale` is the most important field in the payload.** A last-known position two hours old
    shown as current is the single most misleading thing this feature could do.
  * **Accuracy must stay None when it is unknown**, never 0 -- the console draws a confidence
    circle from that number, and a zero radius claims a precision no consumer GPS has.

The second thing asserted is that the three outcomes stay distinguishable. "Located",
"the device answered and had no fix", and "the device never answered at all" send an operator
to three different places, and collapsing any pair of them is the kind of simplification that
looks tidy and costs somebody an afternoon.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import location

PASS = 0
FAIL = 0

#: Filadelfia, Paraguay. Both axes negative on purpose -- see the module docstring.
LAT, LON = -22.3489, -60.0331


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def fix(**overrides):
    payload = {"lat": LAT, "lon": LON, "accuracy_m": 12.5, "provider": "gps",
               "fixed_at": 1_900_000_000, "stale": False}
    payload.update(overrides)
    return payload


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        fleet.init_fleet_db(db_path)
        location.init_location_db(db_path)
        location.init_location_db(db_path)      # idempotent: called on every hub start

        print("== Parsing a reported position ==")
        parsed = location.clean_fix(fix())
        check("a good fix parses as LOCATED", parsed["status"] == location.STATUS_LOCATED)
        check("...and both negative coordinates survive",
              parsed["lat"] == LAT and parsed["lon"] == LON)
        check("...with the provider and accuracy carried through",
              parsed["provider"] == "gps" and parsed["accuracy_m"] == 12.5)
        check("zero is a real coordinate, not a missing one",
              location.clean_fix(fix(lat=0.0, lon=0.0))["status"] == location.STATUS_LOCATED)
        check("a stale fix says so", location.clean_fix(fix(stale=True))["stale"] == 1)
        check("...and a fresh one says that", location.clean_fix(fix())["stale"] == 0)

        check("an unknown accuracy stays None rather than becoming zero",
              location.clean_fix(fix(accuracy_m=None))["accuracy_m"] is None)
        check("...and so does a negative one",
              location.clean_fix(fix(accuracy_m=-5))["accuracy_m"] is None)
        check("an accuracy wider than a city is not a position",
              location.clean_fix(fix(accuracy_m=999999))["accuracy_m"] is None)

        for bad, why in ((None, "absent"), ("x", "not a number"), (91, "off the planet"),
                         (float("nan"), "NaN")):
            got = location.clean_fix(fix(lat=bad))
            check(f"a latitude that is {why} makes it UNAVAILABLE, not a wrong point",
                  got["status"] == location.STATUS_UNAVAILABLE and got["lat"] is None)
        check("a longitude past 180 is refused too",
              location.clean_fix(fix(lon=181))["status"] == location.STATUS_UNAVAILABLE)

        check("the device's reason is kept when there is no fix",
              location.clean_fix({"error": "location is switched off on this device"})["detail"]
              == "location is switched off on this device")
        check("...and there is always SOME reason, so the console never shows a blank row",
              location.clean_fix({})["detail"] != "")
        check("a non-dict is not a location answer at all",
              location.clean_fix("somewhere") is None and location.clean_fix(None) is None)

        print("\n== Storing ==")
        check("a fix is stored",
              location.record_fix(db_path, "PHONE-1", fix(), requested_by="op@x.com",
                                  command_id="c1", now=1000) is not None)
        latest = location.latest_fix(db_path, "PHONE-1")
        check("...and reads back with the operator who asked",
              latest["requested_by"] == "op@x.com" and latest["status"] == "located")
        check("...and `stale` comes back as a bool, not a 0/1 the console has to guess at",
              latest["stale"] is False)
        check("a duplicate result post does not file the fix twice",
              location.record_fix(db_path, "PHONE-1", fix(), command_id="c1", now=1001) is None)
        check("...and the history still holds exactly one row",
              len(location.history(db_path, "PHONE-1")) == 1)

        check("an unavailable answer is STORED, not discarded",
              location.record_fix(db_path, "PHONE-1", {"error": "no fix indoors"},
                                  requested_by="op@x.com", command_id="c2",
                                  now=2000) is not None)
        check("...and does not become the latest fix, which is still the real one",
              location.latest_fix(db_path, "PHONE-1")["lat"] == LAT)
        check("...but it IS in the history, because 'we asked and it said no' is the answer",
              [r["status"] for r in location.history(db_path, "PHONE-1")]
              == ["unavailable", "located"])

        check("a machine with no name stores nothing",
              location.record_fix(db_path, "", fix(), command_id="c3") is None)
        check("a malformed payload stores nothing and does not raise",
              location.record_fix(db_path, "PHONE-1", "junk", command_id="c4") is None)

        print("\n== The three outcomes stay apart ==")
        location.record_no_answer(db_path, "PHONE-2", requested_by="op@x.com",
                                  command_id="c5", detail="the device never collected it",
                                  now=3000)
        rows = location.history(db_path, "PHONE-2")
        check("a locate that produced no answer is its own status",
              rows[0]["status"] == location.STATUS_NO_ANSWER)
        check("...and is NOT a fix", location.latest_fix(db_path, "PHONE-2") is None)
        check("...with the reason, so 'switched off' and 'no GPS' do not look alike",
              "never collected" in rows[0]["detail"])
        check("the three statuses are distinct values",
              len({location.STATUS_LOCATED, location.STATUS_UNAVAILABLE,
                   location.STATUS_NO_ANSWER}) == 3)

        print("\n== Routing a command result ==")
        # A locate whose result comes back through the same hook as every other command's.
        locate_id = fleet.create_command(db_path, "PHONE-3", location.COMMAND_TYPE,
                                         {"timeout_seconds": 45}, issued_by="asker@x.com")
        check("a locate result is filed against the machine and the asker",
              location.handle_result(db_path, locate_id, success=True,
                                     output=json.dumps(fix()), now=4000) is not None)
        filed = location.latest_fix(db_path, "PHONE-3")
        check("...taking the operator from the COMMAND, not from anything the agent sent",
              filed["requested_by"] == "asker@x.com")
        check("...and the machine too", filed["machine"] == "PHONE-3")

        other_id = fleet.create_command(db_path, "PC-01", "restart", {}, issued_by="op@x.com")
        check("a result for any OTHER command type is ignored, not misfiled",
              location.handle_result(db_path, other_id, success=True,
                                     output=json.dumps(fix())) is None)
        check("...and an unknown command id is ignored too",
              location.handle_result(db_path, "nope", success=True, output="{}") is None)

        failed_id = fleet.create_command(db_path, "PHONE-4", location.COMMAND_TYPE, {},
                                         issued_by="op@x.com")
        location.handle_result(db_path, failed_id, success=False,
                               output="executor error: provider exploded", now=5000)
        check("a FAILED locate is filed as no-answer, not as no-fix",
              location.history(db_path, "PHONE-4")[0]["status"] == location.STATUS_NO_ANSWER)

        garbled_id = fleet.create_command(db_path, "PHONE-5", location.COMMAND_TYPE, {},
                                          issued_by="op@x.com")
        location.handle_result(db_path, garbled_id, success=True, output="not json at all",
                               now=5100)
        check("a successful locate with unparseable output is unavailable, not a crash",
              location.history(db_path, "PHONE-5")[0]["status"] == location.STATUS_UNAVAILABLE)

        print("\n== Sweeping the ones that never answered ==")
        stranded = fleet.create_command(db_path, "PHONE-6", location.COMMAND_TYPE, {},
                                        issued_by="op@x.com", ttl_seconds=1)
        fleet.expire_stale_commands(db_path, now=9_000_000_000)
        check("an expired locate is swept into a no-answer row",
              location.sweep_unanswered(db_path, now=6000) == 1)
        check("...saying the device never collected it, not that it had no GPS",
              "never collected" in location.history(db_path, "PHONE-6")[0]["detail"])
        check("...and sweeping twice does not file it twice",
              location.sweep_unanswered(db_path, now=6100) == 0)
        check("a locate that DID answer is never swept",
              all(r["command_id"] != locate_id
                  for r in location.history(db_path, "PHONE-3")
                  if r["status"] == location.STATUS_NO_ANSWER))

        print("\n== In flight ==")
        pending_id = fleet.create_command(db_path, "PHONE-7", location.COMMAND_TYPE, {},
                                          issued_by="op@x.com")
        open_row = location.open_request(db_path, "PHONE-7")
        check("an in-flight locate is visible without a request table of our own",
              open_row is not None and open_row["id"] == pending_id)
        check("...and a machine with none reads as none",
              location.open_request(db_path, "PHONE-1") is None)

        print("\n== The fleet map's query ==")
        location.record_fix(db_path, "PHONE-8", fix(lat=-25.3, lon=-57.6),
                            command_id="c8", now=7000)
        location.record_fix(db_path, "PHONE-8", fix(lat=-25.4, lon=-57.7),
                            command_id="c9", now=7100)
        fixes = location.latest_fixes(db_path)
        by_machine = {f["machine"]: f for f in fixes}
        check("one row per machine, newest first",
              by_machine["PHONE-8"]["lat"] == -25.4)
        check("...only machines that HAVE a fix",
              "PHONE-2" not in by_machine and "PHONE-6" not in by_machine)
        check("a scope filter narrows it",
              [f["machine"] for f in location.latest_fixes(db_path, ["PHONE-8"])] == ["PHONE-8"])
        check("...and an empty scope means nothing, not everything",
              location.latest_fixes(db_path, []) == [])

        print("\n== Retention ==")
        check("old fixes are pruned by age", location.prune(db_path, cutoff=6500) >= 1)
        check("...and the recent ones stay",
              location.latest_fix(db_path, "PHONE-8") is not None)
        check("nothing older than the cutoff survives",
              all(r["received_at"] >= 6500 for r in location.history(db_path, "PHONE-8", 200)))

        # The per-machine cap, which is the storage control rather than the privacy one.
        for i in range(location.MAX_FIXES_PER_MACHINE + 20):
            location.record_fix(db_path, "PHONE-9", fix(), command_id=f"cap{i}",
                                now=8000 + i)
        check("a machine cannot outgrow the per-machine cap between prunes",
              len(location.history(db_path, "PHONE-9", limit=200))
              == location.MAX_FIXES_PER_MACHINE)
        check("...and it is the NEWEST rows that survive",
              location.history(db_path, "PHONE-9")[0]["received_at"]
              == 8000 + location.MAX_FIXES_PER_MACHINE + 19)

        print("\n== Lifecycle ==")
        location.forget_machine(db_path, "PHONE-9")
        check("a deleted machine's whole location history goes",
              location.history(db_path, "PHONE-9") == [])
        location.record_fix(db_path, "MERGE-OLD", fix(), command_id="m1", now=8500)
        location.record_fix(db_path, "MERGE-NEW", fix(), command_id="m2", now=8600)
        location.rename_machine(db_path, "MERGE-OLD", "MERGE-NEW")
        check("a merge MERGES history rather than picking a winner",
              len(location.history(db_path, "MERGE-NEW")) == 2)
        check("...and the merged-away name keeps nothing",
              location.history(db_path, "MERGE-OLD") == [])

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
