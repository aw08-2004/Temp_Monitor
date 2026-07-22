"""Unit tests for backup_paths.py -- the include/exclude grammar, with no I/O involved.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
Under pytest, conftest.py wraps `check` so a false condition fails the test properly.

Most of this file is driven by `tests/backup_path_vectors.json`, the fixture the AGENT's
PathExpander tests read too. That is deliberate: the grammar has two implementations, and
the failure mode when they disagree is not a crash -- it is a folder that quietly stops
being backed up, discovered months later at restore time.

The hand-written checks on top cover what a shared fixture cannot: that bad patterns are
REFUSED rather than silently expanding to nothing, and that preview() explains an empty
result instead of just returning one.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backup_paths

PASS = 0
FAIL = 0

VECTORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "backup_path_vectors.json")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def error_of(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as e:
        return str(e)
    return ""


def main():
    vectors = backup_paths.load_vectors(VECTORS_PATH)
    profiles = vectors["profiles"]

    print("\n== Shared expansion vectors ==")
    for case in vectors["expansions"]:
        got = backup_paths.expand(case["pattern"], profiles)
        check(f"{case['pattern']} -- {case['why']}", got == case["expect"])
        if got != case["expect"]:
            print(f"        expected {case['expect']}")
            print(f"        got      {got}")

    print("\n== Shared invalid-pattern vectors ==")
    for case in vectors["invalid"]:
        message = error_of(backup_paths.validate_pattern, case["pattern"], case["kind"])
        check(f"refused {case['pattern']!r} -- {case['why']}", message != "")

    print("\n== Shared exclude vectors ==")
    for case in vectors["excludes"]:
        matcher = backup_paths.compile_excludes(case["patterns"], profiles)
        for path in case["excluded"]:
            check(f"{case['patterns']} excludes {path}", matcher.matches(path))
        for path in case["kept"]:
            check(f"{case['patterns']} keeps {path}", not matcher.matches(path))

    print("\n== Shared archive-member vectors ==")
    # The other cross-implementation contract in this file. The AGENT names tar members
    # this way; the HUB names them again when planning a restore of an archive it never
    # wrote. If the two disagree the restore downloads the right archive, finds none of
    # the members it asked for, and reports every file as missing.
    for case in vectors["members"]:
        got = backup_paths.archive_member(case["path"])
        check(f"{case['path']} -> {case['member']} -- {case['why']}",
              got == case["member"])
        if got != case["member"]:
            print(f"        got      {got}")

    print("\n== archive_member round-trips back to a Windows path ==")
    # Needed by a restore writing files back to where they came from: the member is what
    # the archive holds, and the original path is where it has to land.
    check("a drive path survives the round trip",
          backup_paths.member_to_path(
              backup_paths.archive_member("C:\\Users\\bob\\a.txt"))
          == "C:\\Users\\bob\\a.txt")
    check("a bare drive keeps its root separator",
          backup_paths.member_to_path("C") == "C:\\")
    # A UNC source cannot be reconstructed -- \\srv\share\f and srv/share/f are the same
    # member -- so it stays relative rather than being guessed into a drive path.
    check("a UNC member stays relative rather than inventing a drive",
          backup_paths.member_to_path("srv/share/f.txt") == "srv\\share\\f.txt")

    print("\n== Unknown tokens are refused, never treated as literals ==")
    # The single most valuable rule in the module: a typo'd token that expanded to nothing
    # would produce a backup that runs green every night and contains nothing.
    message = error_of(backup_paths.validate_pattern, "%Desktopp%")
    check("an unknown token raises", message != "")
    check("the error names the offending token", "%desktopp%" in message.lower())
    check("the error lists what IS valid", "%users%" in message.lower())
    check("a valid token passes",
          backup_paths.validate_pattern("%Desktop%") == "%Desktop%")

    print("\n== Normalisation ==")
    check("forward slashes become backslashes",
          backup_paths.normalize("C:/Users/bob") == "C:\\Users\\bob")
    check("a trailing separator is dropped",
          backup_paths.normalize("C:\\Users\\bob\\") == "C:\\Users\\bob")
    check("doubled separators collapse",
          backup_paths.normalize("C:\\Users\\\\bob") == "C:\\Users\\bob")
    check("a drive root keeps its separator",
          backup_paths.normalize("C:\\") == "C:\\")
    check("a UNC path keeps its leading pair",
          backup_paths.normalize("\\\\srv\\share\\dir") == "\\\\srv\\share\\dir")
    check("a UNC include is accepted",
          backup_paths.validate_pattern("\\\\srv\\share\\finance")
          == "\\\\srv\\share\\finance")

    print("\n== Include vs exclude anchoring ==")
    check("a bare glob is fine as an exclude",
          backup_paths.validate_pattern("*.tmp", kind="exclude") == "*.tmp")
    check("...but refused as an include",
          error_of(backup_paths.validate_pattern, "*.tmp", "include") != "")
    check("the include error points at the exclude list",
          "exclude" in error_of(backup_paths.validate_pattern,
                                "C:\\Users\\*\\Desktop", "include"))

    print("\n== validate_patterns: order kept, blanks and duplicates dropped ==")
    cleaned = backup_paths.validate_patterns(
        ["%Desktop%", "", "  ", "%Documents%", "%desktop%", "C:/Data"])
    check("blanks dropped and order preserved",
          cleaned == ["%Desktop%", "%Documents%", "C:\\Data"])
    check("duplicates are case-insensitive", len(cleaned) == 3)
    check("an empty list is allowed (a legitimate exclude list)",
          backup_paths.validate_patterns([], kind="exclude") == [])

    print("\n== real_users skips everything that is not a person ==")
    names = [u["name"] for u in backup_paths.real_users(profiles)]
    check("only real profiles survive", names == ["bob", "carol"])
    check("no users at all is not a crash",
          backup_paths.real_users({"users": []}) == [])
    check("a profile with no path is skipped",
          backup_paths.real_users({"users": [{"name": "ghost", "path": ""}]}) == [])

    print("\n== preview explains an empty result rather than just returning one ==")
    result = backup_paths.preview(["%Desktop%", "%Documents%"], ["*.tmp"], profiles)
    check("preview resolves the roots it can", len(result["roots"]) == 3)
    check("preview attributes each root to a user",
          {r["user"] for r in result["roots"]} == {"bob", "carol"})
    check("preview names the user missing a folder",
          any("carol" in p and "documents" in p.lower() for p in result["problems"]))
    check("preview lists the machine's users", result["users"] == ["bob", "carol"])

    empty = backup_paths.preview(["%Desktop%"], [], {"users": [], "env": {}})
    check("a machine with no profiles is explained, not silently empty",
          empty["roots"] == [] and any("no user profiles" in p
                                       for p in empty["problems"]))

    unresolvable = backup_paths.preview(["%ProgramData%\\x"], [], {"users": [], "env": {}})
    check("an unreported machine token is explained",
          any("%programdata%" in p.lower() for p in unresolvable["problems"]))

    print("\n== preview catches an exclude that swallows an include ==")
    shadowed = backup_paths.preview(["%Users%\\Desktop"], ["%Users%\\Desktop"], profiles)
    check("an include fully covered by an exclude is flagged",
          any("also excluded" in p for p in shadowed["problems"]))
    check("...and it is flagged per affected root",
          sum("also excluded" in p for p in shadowed["problems"]) == 2)

    print("\n== An invalid pattern in preview is reported, not raised ==")
    # The Backup Settings tab previews as you type; a half-typed token must not 500.
    typing = backup_paths.preview(["%Deskt"], [], profiles)
    check("a malformed pattern comes back as a problem", len(typing["problems"]) == 1)
    check("and produces no roots", typing["roots"] == [])

    print("\n== Exclude matcher edge cases ==")
    empty_matcher = backup_paths.compile_excludes([], profiles)
    check("an empty exclude list is falsy", not empty_matcher)
    check("an empty exclude list matches nothing",
          not empty_matcher.matches("C:\\anything"))
    check("a token exclude on a machine with no profiles is dropped, not crashed",
          not backup_paths.compile_excludes(["%LocalAppData%\\Temp"],
                                            {"users": [], "env": {}}).matches("C:\\x"))
    check("matching an empty path is False", not
          backup_paths.compile_excludes(["*.tmp"], profiles).matches(""))

    # `a\**\b` should match `a\b` -- otherwise every operator has to write both forms.
    zero_dirs = backup_paths.compile_excludes(["C:\\a\\**\\b"], profiles)
    check("** matches zero directories", zero_dirs.matches("C:\\a\\b"))
    check("** matches many directories", zero_dirs.matches("C:\\a\\x\\y\\b"))

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


def test_backup_paths():
    main()


if __name__ == "__main__":
    sys.exit(main())
