"""capabilities.py -- what a machine can actually do (roadmap #23).

**The silent failure this file exists to catch is the fleet going quiet.** Every command in
the hub now passes through a capability check inside `fleet.create_command`, and every Windows
agent in the field reports no capabilities at all. If "has not reported" is ever read as
"cannot do anything" -- by a refactor, by a helper that forgets the None/[] distinction, by a
`get` that defaults to an empty list -- then on the morning that ships, every command on every
machine in the fleet is refused, the schedulers retire their targets with a tidy reason, and
nothing anywhere logs an error. The console would look fine. So the absent-report rule is
asserted from four directions: the reader, `can_run`, `filter_machines`, and `create_command`
itself.

The second failure is the one that prompted the feature: a scheduled command reaching a device
that cannot answer it. `MIN_*_AGENT` is console-side JavaScript about buttons, and the first
Android device to enroll was queued `backup_files` within a minute. That path is tested through
`fleet.create_command` rather than against `can_run` alone, because the funnel is the whole
guarantee -- a test that only checked the helper would still pass on the day somebody moved
the call out of it.

The third is quieter and would only be found by a person: `reported_at` is when a capability
set FIRST appeared, not when the machine was last heard from, and the ingest must not rewrite
the row on every heartbeat just because one arrived.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import capabilities
import fleet

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


#: What the Android agent actually reports today (AgentCapabilities.Report). Spelled out here
#: rather than imported from anywhere, because it is the C# side's contract and a test that
#: derived it from the hub could not notice the two drifting apart.
ANDROID = {"platform": "android", "commands": ["rename"], "features": []}


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        capabilities.init_capabilities_db(db_path)
        # init twice: the hub calls it on every start, and app.py and fleet.init_fleet_db
        # both call it, so a CREATE that was not IF NOT EXISTS would fail at boot.
        capabilities.init_capabilities_db(db_path)

        print("== Parsing a report ==")
        check("a well-formed Android report survives intact",
              capabilities.clean_report(ANDROID)
              == {"platform": "android", "commands": ["rename"], "features": []})
        check("an unknown platform is stored as unknown, not verbatim",
              capabilities.clean_report({"platform": "sailfish",
                                         "commands": ["rename"]})["platform"] == "")
        check("...and platform case does not matter",
              capabilities.clean_report({"platform": "ANDROID"})["platform"] == "android")
        check("an absent commands key parses as None, not as an empty list",
              capabilities.clean_report({"platform": "android"})["commands"] is None)
        check("...and so does a commands key that is not a list at all",
              capabilities.clean_report({"platform": "android",
                                         "commands": "rename"})["commands"] is None)
        check("an EMPTY commands list is a real claim and is kept",
              capabilities.clean_report({"platform": "android",
                                         "commands": []})["commands"] == [])
        check("a report with nothing usable in it is not a report",
              capabilities.clean_report({"platform": "sailfish"}) is None)
        check("...nor is a non-dict", capabilities.clean_report(["rename"]) is None)
        check("names that could never match a command type are dropped",
              capabilities.clean_report(
                  {"platform": "android",
                   "commands": ["rename", "drop table x", "", "  ", "ok_2"]}
              )["commands"] == ["rename", "ok_2"])
        check("duplicates are collapsed",
              capabilities.clean_report({"platform": "android",
                                         "commands": ["rename", "rename"]}
                                        )["commands"] == ["rename"])
        check("a flood of names is capped",
              len(capabilities.clean_report(
                  {"platform": "android",
                   "commands": [f"cmd_{i}" for i in range(500)]}
              )["commands"]) == capabilities.MAX_COMMANDS)
        check("...and so is one long name",
              len(capabilities.clean_report(
                  {"platform": "android", "commands": ["x" * 5000]}
              )["commands"][0]) == capabilities.MAX_NAME_CHARS)

        print("\n== Ingest ==")
        check("recording a report writes it",
              capabilities.record_capabilities(db_path, "PHONE-1", ANDROID, now=1000))
        stored = capabilities.get_capabilities(db_path, "PHONE-1")
        check("...and it reads back as reported",
              stored == {"platform": "android", "commands": ["rename"], "features": [],
                         "reported_at": 1000})
        check("an unchanged report is NOT rewritten",
              capabilities.record_capabilities(db_path, "PHONE-1", ANDROID, now=2000)
              is False)
        check("...so reported_at still means when this set first appeared",
              capabilities.get_capabilities(db_path, "PHONE-1")["reported_at"] == 1000)
        check("a CHANGED report is written, and moves reported_at",
              capabilities.record_capabilities(
                  db_path, "PHONE-1",
                  {"platform": "android", "commands": ["rename", "show_message"],
                   "features": ["locate"]}, now=3000)
              and capabilities.get_capabilities(db_path, "PHONE-1")["reported_at"] == 3000)
        # Back to the baseline so the rest of the file reasons about one known state.
        capabilities.record_capabilities(db_path, "PHONE-1", ANDROID, now=3100)

        print("\n== Nothing a machine can send may break the heartbeat ==")
        for junk in (None, [], "rename", 7, {"platform": {"nested": True}},
                     {"commands": {"a": 1}}, {"platform": "", "commands": None},
                     {"platform": "android", "features": 7}):
            try:
                capabilities.record_capabilities(db_path, "JUNK-1", junk)
                ok = True
            except Exception as e:                     # noqa: BLE001
                ok = False
                print(f"       raised on {junk!r}: {e}")
            check(f"a malformed report is survivable: {junk!r}", ok)
        # The last one in that list is only PARTLY junk -- it names a real platform and a
        # nonsense features value -- and the honest outcome is to keep the half that parsed
        # rather than to drop the report. Dropping it would leave a device whose agent sends
        # one bad field looking, forever, like a machine that had never reported at all.
        salvaged = capabilities.get_capabilities(db_path, "JUNK-1")
        check("a partly-valid report keeps the part that parsed",
              salvaged["platform"] == "android")
        check("...and the unparseable field stays absent rather than becoming empty",
              salvaged["features"] is None and salvaged["commands"] is None)
        capabilities.forget_machine(db_path, "JUNK-1")
        for junk in (None, [], "rename", {"platform": "sailfish"}, {"commands": {"a": 1}}):
            capabilities.record_capabilities(db_path, "JUNK-2", junk)
        check("a report with nothing usable in it creates no row at all",
              capabilities.get_capabilities(db_path, "JUNK-2")["reported_at"] is None)
        check("a report with no machine name is refused",
              capabilities.record_capabilities(db_path, "", ANDROID) is False)

        print("\n== The absent-report rule ==")
        never = capabilities.get_capabilities(db_path, "PC-01")
        check("a machine that never reported reads as unknown, not incapable",
              never == {"platform": "", "commands": None, "features": None,
                        "reported_at": None})
        check("...so it may be sent anything",
              capabilities.can_run(db_path, "PC-01", "backup_files")
              and capabilities.can_run(db_path, "PC-01", "run_script")
              and capabilities.can_run(db_path, "PC-01", "restart"))
        check("...and refusal_for has nothing to say about it",
              capabilities.refusal_for(db_path, "PC-01", "backup_files") is None)
        check("...and filter_machines keeps it",
              capabilities.filter_machines(db_path, ["PC-01"], "backup_files") == ["PC-01"])
        # The other side of the asymmetry, and the reason it is not a mistake: a FEATURE is
        # only ever reported by an agent new enough to have it, so unknown really is no.
        check("an unreported FEATURE is False, unlike an unreported command",
              capabilities.supports(db_path, "PC-01", capabilities.FEATURE_LOCATE) is False)

        print("\n== Enforcement ==")
        check("a reported command is allowed",
              capabilities.can_run(db_path, "PHONE-1", "rename"))
        check("one left out of the report is not",
              not capabilities.can_run(db_path, "PHONE-1", "backup_files"))
        refusal = capabilities.refusal_for(db_path, "PHONE-1", "backup_files")
        check("the refusal names the machine, the command and the platform",
              refusal and "PHONE-1" in refusal and "backup_files" in refusal
              and "android" in refusal)
        check("...and says a newer agent will not help, so nobody goes looking for one",
              refusal and "newer agent will not change it" in refusal)
        check("a machine reporting an EMPTY command list is enforced, not ignored",
              capabilities.record_capabilities(
                  db_path, "MUTE-1", {"platform": "android", "commands": []}, now=4000)
              and not capabilities.can_run(db_path, "MUTE-1", "rename"))
        check("a machine reporting only a platform is still unknown for commands",
              capabilities.record_capabilities(
                  db_path, "BARE-1", {"platform": "windows"}, now=4100)
              and capabilities.can_run(db_path, "BARE-1", "run_script"))
        check("features answer separately from commands",
              capabilities.record_capabilities(
                  db_path, "PHONE-2",
                  {"platform": "android", "commands": ["rename"],
                   "features": [capabilities.FEATURE_LOCATE]}, now=4200)
              and capabilities.supports(db_path, "PHONE-2", capabilities.FEATURE_LOCATE)
              and not capabilities.supports(db_path, "PHONE-2",
                                            capabilities.FEATURE_APP_POLICY)
              and not capabilities.can_run(db_path, "PHONE-2", "locate_device"))

        print("\n== filter_machines: what the schedulers call ==")
        fleet_names = ["PC-01", "PHONE-1", "PC-02", "MUTE-1", "BARE-1"]
        check("only the machines that said they cannot are dropped",
              capabilities.filter_machines(db_path, fleet_names, "backup_files")
              == ["PC-01", "PC-02", "BARE-1"])
        check("...and the caller's order is preserved",
              capabilities.filter_machines(db_path, ["PC-02", "PC-01"], "restart")
              == ["PC-02", "PC-01"])
        check("an empty ask is an empty answer, not the whole fleet",
              capabilities.filter_machines(db_path, [], "backup_files") == [])
        check("blank names are dropped rather than queried",
              capabilities.filter_machines(db_path, ["", "  ", "PC-01"], "restart")
              == ["PC-01"])

        print("\n== platform lookups ==")
        check("platforms_for reports only machines that named one",
              capabilities.platforms_for(db_path)
              == {"PHONE-1": "android", "PHONE-2": "android", "MUTE-1": "android",
                  "BARE-1": "windows"})
        check("...and can be narrowed to a scope",
              capabilities.platforms_for(db_path, ["PHONE-1", "PC-01"])
              == {"PHONE-1": "android"})
        check("...where an empty scope means nothing, not everything",
              capabilities.platforms_for(db_path, []) == {})
        check("platform_of is blank for a machine that never said",
              capabilities.platform_of(db_path, "PC-01") == "")

        print("\n== The funnel: fleet.create_command ==")
        # The guarantee is that the CHECK IS IN THE FUNNEL, not that a helper exists. A test
        # against can_run alone would still pass the day somebody moved the call out of it.
        fleet.init_fleet_db(db_path)
        try:
            fleet.create_command(db_path, "PHONE-1", "backup_files", {},
                                 issued_by="scheduler")
            check("create_command refuses a command the machine cannot run", False)
        except fleet.UnsupportedCommand as e:
            check("create_command refuses a command the machine cannot run", True)
            check("...with the reason attached, not a bare 'unsupported'",
                  "backup_files" in str(e) and "android" in str(e))
        check("UnsupportedCommand is a ValueError, so existing handlers keep working",
              issubclass(fleet.UnsupportedCommand, ValueError))
        check("...and nothing was queued",
              fleet.list_commands(db_path, "PHONE-1") == [])
        allowed = fleet.create_command(db_path, "PHONE-1", "rename",
                                       {"new_name": "PHONE-9"}, issued_by="op@x.com")
        check("a command it CAN run is queued as before", bool(allowed))
        unreported = fleet.create_command(db_path, "PC-01", "backup_files", {},
                                          issued_by="scheduler")
        check("a machine that never reported is queued exactly as before",
              bool(unreported))
        check("init_fleet_db creates the capability table it now depends on",
              _table_exists(db_path, "machine_capabilities"))

        print("\n== Lifecycle ==")
        capabilities.forget_machine(db_path, "PHONE-2")
        check("a deleted machine's report is dropped",
              capabilities.get_capabilities(db_path, "PHONE-2")["reported_at"] is None)
        check("...so a reused hostname is not silently refused commands",
              capabilities.can_run(db_path, "PHONE-2", "run_script"))
        capabilities.record_capabilities(db_path, "MERGE-OLD", ANDROID, now=5000)
        capabilities.rename_machine(db_path, "MERGE-OLD", "MERGE-NEW")
        check("a merge carries the report to a survivor that has none",
              capabilities.get_capabilities(db_path, "MERGE-NEW")["platform"] == "android")
        capabilities.record_capabilities(db_path, "MERGE-OLD2",
                                         {"platform": "android", "commands": []}, now=5100)
        capabilities.rename_machine(db_path, "MERGE-OLD2", "MERGE-NEW")
        check("...but the survivor's own report wins a collision, and is not merged into",
              capabilities.get_capabilities(db_path, "MERGE-NEW")["commands"] == ["rename"])
        check("...and the merged-away row is gone",
              capabilities.get_capabilities(db_path, "MERGE-OLD2")["reported_at"] is None)

        print("\n== Stored shape ==")
        # Asserted directly, because the NULL/'[]' distinction in the column IS the
        # absent-report rule -- a migration that gave the columns a DEFAULT '[]' would pass
        # every behavioural test above on a fresh row and fail on every existing one.
        with sqlite3.connect(db_path) as conn:
            bare = conn.execute("SELECT commands_json, features_json FROM "
                                "machine_capabilities WHERE machine = 'BARE-1'").fetchone()
            mute = conn.execute("SELECT commands_json FROM machine_capabilities "
                                "WHERE machine = 'MUTE-1'").fetchone()
        check("an unreported list is SQL NULL", bare == (None, None))
        check("an empty reported list is a JSON array", json.loads(mute[0]) == [])

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


def _table_exists(db_path, name):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None


if __name__ == "__main__":
    sys.exit(main())
