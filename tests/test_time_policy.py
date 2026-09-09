"""policy.py's time half -- curfews and daily budgets (roadmap #23 phase E).

**The silent failure this file exists to catch is a schedule that never reaches the device.**
The heartbeat sends a document only when its version differs from the one the device holds, and
the version is a hash of the document's contents. Editing a curfew changes nothing about the
blocked list -- so a version computed over `blocked` alone would leave every phone holding
yesterday's schedule until something unrelated changed. There is no error anywhere in that: the
console shows the new curfew, the device enforces the old one, and the only way to notice is to
be standing next to a phone at 22:00.

The second is that a schedule is NOT resolved into the blocked list. It cannot be: a curfew
depends on what time it is where the device is, and a budget on how much the device has been
used today, and the hub has neither fact promptly. The rules travel; the device decides. A
change that "simplified" this by resolving curfews here would produce a fleet that enforces its
bedtime only while it can reach the hub -- which is the opposite of what a curfew is for.

The third is the merge rule. Two schedules that both cover a device both apply, unreconciled,
because inventing a precedence would mean an operator cannot predict either one. The single
exception is a duplicate budget on the same package, where a union has no meaning: the SMALLER
number wins, which is the strict reading a restriction is expected to have.

And the fourth is validation of a curfew that starts and ends at the same minute -- ambiguous
between "no time at all" and "the whole day". Guessing either would produce a rule whose author
and whose reader disagree about what it says.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import policy
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


def rejected(fn, *a, **k):
    """True when policy.py refuses with a sentence rather than raising something else."""
    try:
        fn(*a, **k)
    except policy.PolicyRejected:
        return True
    return False


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        policy.init_policy_db(db_path)
        policy.init_policy_db(db_path)
        check("init_policy_db can run twice", True)

        print("== Validation ==")
        check("a policy with neither a curfew nor a budget is refused",
              rejected(policy.validate_time_rules, "Empty", {}, ["PHONE-1"], False))
        check("a curfew that starts and ends at the same minute is refused",
              rejected(policy.validate_time_rules, "Same",
                       {"windows": [{"days": [0], "start": "22:00", "end": "22:00"}]},
                       ["PHONE-1"], False))
        check("a curfew with no days is refused",
              rejected(policy.validate_time_rules, "Nodays",
                       {"windows": [{"days": [], "start": "22:00", "end": "07:00"}]},
                       ["PHONE-1"], False))
        check("a day outside Monday-to-Sunday is refused",
              rejected(policy.validate_time_rules, "Badday",
                       {"windows": [{"days": [9], "start": "22:00", "end": "07:00"}]},
                       ["PHONE-1"], False))
        check("a time that is not a time is refused",
              rejected(policy.validate_time_rules, "Badtime",
                       {"windows": [{"days": [0], "start": "bedtime", "end": "07:00"}]},
                       ["PHONE-1"], False))
        check("a negative budget is refused",
              rejected(policy.validate_time_rules, "Negative",
                       {"budgets": [{"package": "com.a", "minutes": -1}]},
                       ["PHONE-1"], False))
        # The launcher can never be suspended, so a budget on it would report as applied and do
        # nothing -- which is worse than being told no.
        check("a budget on a package that can never be suspended is refused",
              rejected(policy.validate_time_rules, "Launcher",
                       {"budgets": [{"package": "com.sec.android.app.launcher",
                                     "minutes": 30}]},
                       ["PHONE-1"], False))
        check("a policy that is neither fleet-wide nor aimed at a machine is refused",
              rejected(policy.validate_time_rules, "Nowhere",
                       {"budgets": [{"package": "com.a", "minutes": 30}]}, [], False))

        parts = policy.validate_time_rules(
            "Bedtime", {"windows": [{"days": [0, 0, 2], "start": "22:00", "end": "07:00"}]},
            ["PHONE-1"], False)
        check("times are normalised to minutes past midnight",
              parts["windows"][0]["start"] == 1320 and parts["windows"][0]["end"] == 420)
        check("...duplicate days collapse and sort", parts["windows"][0]["days"] == [0, 2])
        check("a window naming no package covers the whole device",
              parts["windows"][0]["packages"] == [policy.EVERY_PACKAGE])
        # Zero minutes is meaningful: "not at all today", a blocklist written as a schedule.
        zero = policy.validate_time_rules(
            "Zero", {"budgets": [{"package": "com.a", "minutes": 0}]}, ["PHONE-1"], False)
        check("a zero-minute budget is accepted, because it means 'not at all today'",
              zero["budgets"][0]["minutes"] == 0)

        print("\n== Storing and reading ==")
        bedtime = policy.create_time_policy(
            db_path, name="Bedtime",
            rules={"windows": [{"days": [0, 1, 2, 3, 4], "start": "22:00", "end": "07:00",
                                "packages": ["com.zhiliaoapp.musically"]}]},
            machines=["PHONE-1"], actor="super@x.com", now=1000)
        found = policy.get_time_policy(db_path, bedtime)
        check("a stored policy reads back with its window", len(found["windows"]) == 1)
        check("...and its target", found["machines"] == ["PHONE-1"])
        check("an unknown id reads as None",
              policy.get_time_policy(db_path, "nope") is None)
        check("listing finds it", len(policy.list_time_policies(db_path)) == 1)

        print("\n== Resolution: rules travel, the device decides ==")
        document = policy.resolve_for(db_path, "PHONE-1")
        check("a curfew is NOT resolved into the blocked list", document["blocked"] == [])
        check("...it rides the document as a schedule",
              len(document["schedule"]["windows"]) == 1)
        check("...and the console can see which policy produced it",
              any(p["mode"] == "schedule" for p in document["policies"]))
        check("a machine the policy does not name gets an empty schedule",
              policy.resolve_for(db_path, "PHONE-2")["schedule"]
              == {"windows": [], "budgets": []})

        print("\n== The version covers the schedule ==")
        # The assertion this file exists for.
        before = policy.resolve_for(db_path, "PHONE-1")["version"]
        policy.update_time_policy(
            db_path, bedtime, name="Bedtime",
            rules={"windows": [{"days": [0, 1, 2, 3, 4], "start": "21:00", "end": "07:00",
                                "packages": ["com.zhiliaoapp.musically"]}]},
            machines=["PHONE-1"], now=1100)
        after = policy.resolve_for(db_path, "PHONE-1")["version"]
        check("moving a curfew an hour earlier changes the document version", before != after)
        check("...and the blocked list is still empty, which is why it had to",
              policy.resolve_for(db_path, "PHONE-1")["blocked"] == [])

        print("\n== Merging, unreconciled ==")
        policy.create_time_policy(
            db_path, name="Weekend",
            rules={"windows": [{"days": [5, 6], "start": "23:00", "end": "08:00"}]},
            fleet_wide=True, actor="super@x.com", now=1200)
        schedule = policy.resolve_schedule(db_path, "PHONE-1")
        check("two policies that both cover a device contribute both windows",
              len(schedule["windows"]) == 2)

        policy.create_time_policy(
            db_path, name="Loose", rules={"budgets": [{"package": "com.a", "minutes": 120}]},
            machines=["PHONE-1"], actor="super@x.com", now=1300)
        policy.create_time_policy(
            db_path, name="Strict", rules={"budgets": [{"package": "com.a", "minutes": 30}]},
            machines=["PHONE-1"], actor="super@x.com", now=1400)
        budgets = policy.resolve_schedule(db_path, "PHONE-1")["budgets"]
        check("a duplicate budget on one package keeps the SMALLER number",
              [b for b in budgets if b["package"] == "com.a"][0]["minutes"] == 30)

        print("\n== Disabled and deleted ==")
        policy.update_time_policy(db_path, bedtime, name="Bedtime",
                                  rules=policy.get_time_policy(db_path, bedtime),
                                  machines=["PHONE-1"], enabled=False, now=1500)
        check("a disabled policy contributes nothing",
              len(policy.resolve_schedule(db_path, "PHONE-1")["windows"]) == 1)
        check("deleting one that does not exist says so",
              policy.delete_time_policy(db_path, "nope") is False)
        check("deleting one that does works", policy.delete_time_policy(db_path, bedtime))

        print("\n== Budgets in the compliance view ==")
        usage.init_usage_db(db_path)
        usage.record_usage(db_path, "PHONE-1",
                           {"days": {"2026-09-08": {"com.a": 45 * 60, "com.b": 10 * 60}}},
                           now=2000)
        answer = policy.compliance(db_path, "PHONE-1")
        spent = [b for b in answer["budgets"] if b["package"] == "com.a"][0]
        check("a budget is shown against what has actually been used",
              spent["used_minutes"] == 45 and spent["minutes"] == 30)
        check("...and says whether it has been exceeded", spent["over"] is True)

        policy.create_time_policy(
            db_path, name="Screen", rules={"budgets": [{"package": "*", "minutes": 120}]},
            machines=["PHONE-1"], actor="super@x.com", now=2100)
        answer = policy.compliance(db_path, "PHONE-1")
        whole = [b for b in answer["budgets"] if b["package"] == "*"][0]
        # "Two hours of screen time" means the screen, including apps no rule mentions.
        check("a whole-device budget counts every app the device reported",
              whole["used_minutes"] == 55 and whole["over"] is False)

        print("\n== Lifecycle ==")
        policy.create_time_policy(
            db_path, name="Gone", rules={"budgets": [{"package": "com.a", "minutes": 10}]},
            machines=["PHONE-9"], actor="super@x.com", now=2200)
        policy.forget_machine(db_path, "PHONE-9")
        # Only the policies that NAMED it. The fleet-wide weekend curfew still resolves for any
        # name, which is what fleet-wide means -- and is why this checks the budget rather than
        # the whole schedule.
        check("forgetting a machine drops it from every schedule that named it",
              policy.resolve_schedule(db_path, "PHONE-9")["budgets"] == [])

        policy.create_time_policy(
            db_path, name="Renamed", rules={"budgets": [{"package": "com.a", "minutes": 10}]},
            machines=["OLD-NAME"], actor="super@x.com", now=2300)
        policy.rename_machine(db_path, "OLD-NAME", "NEW-NAME")
        check("a rename carries the schedule with it",
              len(policy.resolve_schedule(db_path, "NEW-NAME")["budgets"]) == 1)
        check("...and leaves nothing under the old name",
              policy.resolve_schedule(db_path, "OLD-NAME")["budgets"] == [])

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
