"""Unit tests for software.py -- the installed-software inventory (roadmap #25 B).

**The silent failure this file exists to catch is a PC that reads as having nothing
installed.** Three routes lead there and each is asserted:

  * a machine that has NEVER reported must come back `reported_at: None`, distinctly from one
    that reported an empty list -- the device sheet words the two differently, and a hub
    deployed ahead of the agent (always, VERSIONING.md) holds the first state for every PC;
  * a MALFORMED report (not a list, or a non-empty list of garbage) must replace nothing, or
    one truncated heartbeat empties a good inventory;
  * an empty LIST is a real report and must be stored, including its state row, or "every
    program was uninstalled" can never arrive.

Also pinned: the fleet catalog's scoping contract (an empty scope is an empty catalog, never
the fleet), survivor-wins on a duplicate merge, and that forgetting a machine takes its state
row with it -- a leftover state row would make a reused hostname read as "reported, nothing
installed".
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import software

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


def entry(name, version="1.0", entry_id=None, **extra):
    return {"id": entry_id or f"HKLM64:{name}", "name": name, "version": version,
            "publisher": "Acme", **extra}


def main():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        software.init_software_db(db)
        software.init_software_db(db)  # idempotent

        print("\n== Never reported is not the same as nothing installed ==")
        inv = software.get_inventory(db, "PC-01")
        check("a machine that never reported has reported_at None", inv["reported_at"] is None)
        check("...and an empty list", inv["software"] == [])

        print("\n== A report is stored, capped and normalised ==")
        n = software.record_inventory(db, "PC-01", {"software": [
            entry("Zeta Tool"),
            entry("Acrobat Reader", "2017.011", arch="x86"),
            entry("Notes", "3", entry_id="HKU:S-1-5-21-1:Notes", scope="user",
                  user_sid="S-1-5-21-1"),
            entry("Weird", scope="galaxy", arch="arm128", user_sid="S-9"),
        ], "error": ""}, now=1000)
        check("four entries stored", n == 4)
        inv = software.get_inventory(db, "PC-01")
        check("reported_at is the report time", inv["reported_at"] == 1000)
        names = [s["name"] for s in inv["software"]]
        check("listed alphabetically", names == ["Acrobat Reader", "Notes", "Weird", "Zeta Tool"])
        weird = next(s for s in inv["software"] if s["name"] == "Weird")
        check("an unknown scope reads as machine -- never attributes an install to a person",
              weird["scope"] == "machine")
        check("...and a machine-scope entry carries no user SID", weird["user_sid"] == "")
        check("an unknown arch is blanked, not stored verbatim", weird["arch"] == "")
        notes = next(s for s in inv["software"] if s["name"] == "Notes")
        check("a per-user entry keeps its SID", notes["scope"] == "user"
              and notes["user_sid"] == "S-1-5-21-1")

        long_name = "x" * 5000
        software.record_inventory(db, "PC-02", {"software": [entry(long_name)]})
        stored = software.list_software(db, "PC-02")[0]["name"]
        check("a hostile name is capped", len(stored) == software.MAX_NAME_CHARS)

        print("\n== Malformed reports replace nothing ==")
        for junk in ({"software": "nope"}, {"software": None}, {}, "string", None,
                     {"software": [1, "two", {"name": ""}, {"id": "x"}]}):
            check(f"record_inventory({junk!r:.40}) writes nothing",
                  software.record_inventory(db, "PC-01", junk) is None)
        check("...and the good inventory survives all of them",
              software.get_inventory(db, "PC-01")["count"] == 4)

        print("\n== An empty list is a real report ==")
        check("an empty list is stored (returns 0, not None)",
              software.record_inventory(db, "PC-01", {"software": []}, now=2000) == 0)
        inv = software.get_inventory(db, "PC-01")
        check("...and reads as reported, with nothing in it",
              inv["reported_at"] == 2000 and inv["software"] == [] and inv["count"] == 0)

        print("\n== Duplicate ids keep the first ==")
        software.record_inventory(db, "PC-03", {"software": [
            entry("A", "1", entry_id="dup"), entry("B", "2", entry_id="dup")]})
        check("one row per id", [s["name"] for s in software.list_software(db, "PC-03")] == ["A"])

        print("\n== The fleet catalog ==")
        software.record_inventory(db, "PC-01", {"software": [entry("Acrobat Reader", "2017"),
                                                             entry("VPN", "4")]})
        software.record_inventory(db, "PC-04", {"software": [
            entry("Acrobat Reader", "2017"), entry("Acrobat Reader", "2024", entry_id="b")]})
        rows = software.catalog(db)
        acro = [r for r in rows if r["name"] == "Acrobat Reader"]
        check("grouped by version, not name alone",
              {(r["version"], r["machines"]) for r in acro} == {("2017", 2), ("2024", 1)})
        check("an empty scope is an empty catalog -- never the unscoped fleet",
              software.catalog(db, machines=[]) == [])
        scoped = software.catalog(db, machines=["PC-04"])
        check("a scope counts only its machines",
              {(r["name"], r["version"], r["machines"]) for r in scoped}
              == {("Acrobat Reader", "2017", 1), ("Acrobat Reader", "2024", 1)})
        check("the search matches the publisher too",
              len(software.catalog(db, query="acme")) == len(rows))
        check("a LIKE wildcard in the search is literal, not a match-everything",
              software.catalog(db, query="%") == [])
        check("machines_with names the machines carrying a version",
              software.machines_with(db, "Acrobat Reader", "2017") == ["PC-01", "PC-04"])
        check("reported_at_many answers only for machines that reported",
              set(software.reported_at_many(db, ["PC-01", "PC-99"])) == {"PC-01"})

        print("\n== Merge and forget ==")
        software.rename_machine(db, "PC-04", "PC-01")
        check("survivor wins: PC-01 keeps its own list",
              {s["name"] for s in software.list_software(db, "PC-01")}
              == {"Acrobat Reader", "VPN"})
        check("...and the dropped machine's rows and state are gone",
              software.get_inventory(db, "PC-04")["reported_at"] is None)
        software.rename_machine(db, "PC-03", "PC-NEW")
        check("with no survivor inventory, the dropped one moves over",
              software.get_inventory(db, "PC-NEW")["count"] == 1)
        software.forget_machine(db, "PC-01")
        check("forget drops the state row, so a reused hostname reads as never reported",
              software.get_inventory(db, "PC-01")["reported_at"] is None)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db + suffix)
            except OSError:
                pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
