"""device_groups.py -- saved target filters (hub 1.114.0).

The silent failures this file exists to catch:

  * **A group that fails OPEN.** A corrupt definition, a deleted group or a nested group must
    each resolve to NO machines. Each of them resolving to every machine instead would make a
    rule or a deployment aimed at the group hit the whole fleet, with nothing in the UI looking
    wrong.
  * **A rule that quietly stops following its group.** The `group` selector is expanded inside
    rules.resolve_targets; if that expansion regresses, a rule aimed at a group targets nobody
    and reads as a rule that never matches.
  * **A group deleted out from under a rule.** rules_using_group has to find the rule in either
    half of its target, or the refusal that protects it never fires.
  * **Two groups whose names differ only by case**, which is how a rule ends up aimed at the
    stale one.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import device_groups
import rules

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


ROWS = [{"machine": "PC-1", "ad_ou": "OU=Sales,DC=corp", "ad_dn": ""},
        {"machine": "PC-2", "ad_ou": "OU=Sales,DC=corp", "ad_dn": ""},
        {"machine": "PC-3", "ad_ou": "OU=Lab,DC=corp", "ad_dn": ""}]


def main():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        device_groups.init_device_groups_db(db)
        device_groups.init_device_groups_db(db)   # idempotent

        print("\n-- validation --")
        err, _ = device_groups.validate({"name": "", "target": {"include": [{"kind": "all"}]}})
        check("a nameless group is refused", err is not None)
        err, _ = device_groups.validate({"name": "x", "target": {"include": []}})
        check("a group with no include is refused, not defaulted to every PC", err is not None)
        err, _ = device_groups.validate({"name": "x", "target": {"include": [{"kind": "group", "group_id": 1}]}})
        check("a group cannot include a group", err and "another device group" in err)
        err, _ = device_groups.validate({"name": "x", "target": {"include": [{"kind": "all"}],
                                                                  "exclude": [{"kind": "group", "group_id": 1}]}})
        check("...nor exclude one", err and "another device group" in err)
        err, _ = device_groups.validate({"name": "x" * 81, "target": {"include": [{"kind": "all"}]}})
        check("an over-long name is refused", err is not None)

        print("\n-- saving --")
        err, sales = device_groups.save_group(db, {
            "name": "Sales", "description": "Sales floor",
            "target": {"include": [{"kind": "ad_ou", "ou": "OU=Sales,DC=corp"}],
                       "exclude": [{"kind": "machines", "machines": ["PC-2"]}]}}, actor="a@x")
        check("a valid group saves", err is None and sales["id"] > 0)
        err, _ = device_groups.save_group(db, {"name": "sales", "target": {"include": [{"kind": "all"}]}})
        check("a name differing only by case is refused", err and "already exists" in err)
        err, _ = device_groups.save_group(db, {"name": "Ghost", "target": {"include": [{"kind": "all"}]}},
                                          group_id=9999)
        check("updating a group that does not exist says so", err == "no such group")

        print("\n-- resolving --")
        check("a group resolves through its own include/exclude",
              device_groups.resolve(db, sales, ROWS) == ["PC-1"])
        sets = device_groups.member_sets(db, [sales["id"], 424242], ROWS)
        check("member_sets answers each id asked", set(sets) == {sales["id"], 424242})
        check("...with lowercased names", sets[sales["id"]] == {"pc-1"})
        check("...and a missing group is an EMPTY set, never everything", sets[424242] == set())

        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE device_groups SET target_json = '{not json' WHERE id = ?", (sales["id"],))
        broken = device_groups.get_group(db, sales["id"])
        check("a corrupt definition fails closed", device_groups.resolve(db, broken, ROWS) == [])
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE device_groups SET target_json = ? WHERE id = ?",
                         (json.dumps(sales["target"]), sales["id"]))

        print("\n-- rules aimed at a group --")
        err, target = rules.validate_target({"include": [{"kind": "group", "group_id": sales["id"]}]})
        check("rules accept a group selector", err is None and target["include"][0]["kind"] == "group")
        err, _ = rules.validate_target({"include": [{"kind": "group", "group_id": "nope"}]})
        check("...and refuse one without a usable id", err is not None)
        check("a rule target expands the group",
              rules.resolve_targets(db, target, ROWS) == ["PC-1"])
        excluded = {"include": [{"kind": "all"}], "exclude": [{"kind": "group", "group_id": sales["id"]}]}
        check("...in the exclude half too",
              rules.resolve_targets(db, excluded, ROWS) == ["PC-2", "PC-3"])
        stale = {"include": [{"kind": "group", "group_id": 424242}]}
        check("a rule aimed at a deleted group targets nobody", rules.resolve_targets(db, stale, ROWS) == [])

        print("\n-- deletion --")
        original = rules.list_rules
        rules.list_rules = lambda _db: [
            {"id": 1, "name": "Nightly", "target": {"include": [{"kind": "group", "group_id": sales["id"]}]}},
            {"id": 2, "name": "Except sales", "target": {"include": [{"kind": "all"}],
                                                         "exclude": [{"kind": "group", "group_id": sales["id"]}]}},
            {"id": 3, "name": "Unrelated", "target": {"include": [{"kind": "all"}]}},
        ]
        try:
            using = rules.rules_using_group(db, sales["id"])
            check("rules_using_group finds the rule in either half", [r["name"] for r in using] == ["Nightly", "Except sales"])
            check("...and not a rule that merely targets every PC", all(r["id"] != 3 for r in using))
            err, _ = device_groups.delete_group(db, sales["id"], in_use=using)
            check("deleting a group in use is refused, naming the rules", err and "Nightly" in err)
            check("...and the group is still there", device_groups.get_group(db, sales["id"]) is not None)
        finally:
            rules.list_rules = original
        err, gone = device_groups.delete_group(db, sales["id"], in_use=[])
        check("an unused group deletes", err is None and gone["name"] == "Sales")
        check("...and is gone", device_groups.get_group(db, sales["id"]) is None)
    finally:
        # `with sqlite3.connect(...)` commits but does not close, so on Windows the file can
        # still be held when this runs. A leftover temp file is not a test failure.
        try:
            os.unlink(db)
        except OSError:
            pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
