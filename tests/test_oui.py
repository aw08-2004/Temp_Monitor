"""MAC vendor lookup for network discovery (hub/oui.py, tools/refresh_oui.py; roadmap #18).

The silent failure this file exists to catch is **a sweep that names the wrong maker with
confidence**: a phone's randomized address matched against the registry as if it were real, a
small vendor's 36-bit block reported as "IEEE Registration Authority" because only the 24-bit
table was consulted, or a refresh that wrote a truncated download over the snapshot and quietly
stripped names off every sweep. None of those errors -- each is a plausible-looking line under
a MAC -- which is why they need asserting.
"""
import gzip
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import oui
import refresh_oui

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


def test_lookup():
    print("lookup")
    oui.use_table({
        "3C2AF4": "Brother Industries, LTD.",
        "70B3D5": "IEEE Registration Authority",
        "70B3D5123": "Tiny Sensor Co",
        "0050C2": "IEEE Registration Authority",
        "0050C2A": "Medium Block GmbH",
    })
    check("a 24-bit block names its vendor",
          oui.lookup("3c:2a:f4:01:02:03") == {"vendor": "Brother Industries, LTD.",
                                               "private": False})
    check("separators and case do not matter",
          oui.lookup("3C-2A-F4-01-02-03")["vendor"] == "Brother Industries, LTD.")
    check("a 36-bit block wins over the registry's own 24-bit entry",
          oui.lookup("70:B3:D5:12:3F:FF")["vendor"] == "Tiny Sensor Co")
    check("a 28-bit block wins too",
          oui.lookup("00:50:C2:A1:00:01")["vendor"] == "Medium Block GmbH")
    check("outside the carved-out block, the 24-bit entry still answers",
          oui.lookup("70:B3:D5:99:00:01")["vendor"] == "IEEE Registration Authority")
    check("an unknown block has no vendor and is not private",
          oui.lookup("00:11:22:33:44:55") == {"vendor": None, "private": False})
    check("a malformed MAC is no vendor, not an error",
          oui.lookup("not a mac") == {"vendor": None, "private": False})
    # 0x3E has the locally-administered bit (0x02) set. Put it IN the table to prove the bit
    # is checked before the lookup, not after a miss.
    oui.use_table({"3E2AF4": "Should Never Be Shown"})
    check("a private (locally administered) address is labelled, never looked up",
          oui.lookup("3e:2a:f4:01:02:03") == {"vendor": None, "private": True})
    check("is_private reads the same bit", oui.is_private("3E:2A:F4:01:02:03")
          and not oui.is_private("3C:2A:F4:01:02:03"))


def test_missing_snapshot_is_empty_not_fatal():
    print("a missing snapshot")
    fd, path = tempfile.mkstemp(suffix=".gz")
    os.close(fd)
    os.unlink(path)
    check("loading a file that is not there is an empty table", oui._load(path) == {})
    with open(path, "wb") as fh:
        fh.write(b"not gzip at all")
    check("so is a corrupt one", oui._load(path) == {})
    os.unlink(path)


MAL = ("Registry,Assignment,Organization Name,Organization Address\n"
       'MA-L,3C2AF4,"Brother  Industries, LTD.","1-1-1 Nagoya JP"\n'
       "MA-L,ZZZZZZ,Not Hex,Nowhere\n"
       "MA-L,70B3D5,IEEE Registration Authority,Piscataway\n")
MAM = ("Registry,Assignment,Organization Name,Organization Address\n"
       "MA-M,0050C2A,Medium Block GmbH,Berlin\n")
MAS = ("Registry,Assignment,Organization Name,Organization Address\n"
       "MA-S,70B3D5123,Tiny Sensor Co,Austin\n")


def test_refresh_builds_what_lookup_reads():
    print("the refresh script")
    data, count = refresh_oui.build([MAL, MAM, MAS])
    check("rows that are not hex prefixes are skipped", count == 4)
    again, _ = refresh_oui.build([MAL, MAM, MAS])
    check("the output is byte-identical run to run (no diff without a registry change)",
          data == again)
    text = gzip.decompress(data).decode("utf-8")
    check("internal whitespace in names is collapsed",
          "3C2AF4\tBrother Industries, LTD." in text)
    check("the postal address is not kept", "Nagoya" not in text)

    fd, path = tempfile.mkstemp(suffix=".gz")
    os.close(fd)
    with open(path, "wb") as fh:
        fh.write(data)
    table = oui._load(path)
    os.unlink(path)
    oui.use_table(table)
    check("the lookup reads what the refresh wrote, longest prefix first",
          oui.lookup("70:B3:D5:12:30:00")["vendor"] == "Tiny Sensor Co"
          and oui.lookup("3C:2A:F4:00:00:01")["vendor"] == "Brother Industries, LTD.")


def test_refresh_refuses_a_truncated_download():
    print("a truncated registry is refused")
    with tempfile.TemporaryDirectory() as d:
        for name, text in (("oui.csv", MAL), ("mam.csv", MAM), ("oui36.csv", MAS)):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        target = os.path.join(d, "out", "oui.tsv.gz")
        code = refresh_oui.main(["--from", d], out=target)
        check("four prefixes is not a registry: exit non-zero", code == 1)
        check("and nothing was written over the snapshot", not os.path.exists(target))


def main():
    test_lookup()
    test_missing_snapshot_is_empty_not_fatal()
    test_refresh_builds_what_lookup_reads()
    test_refresh_refuses_a_truncated_download()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
