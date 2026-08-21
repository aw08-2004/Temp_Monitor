"""Which operating system a machine is running, and where that answer came from.

THE ONE THING WORTH TESTING HERE. Microsoft shipped Windows 11 as build 22000 of the same
product and did not change the caption in WMI, so early Windows 11 machines report
themselves as "Windows 10". A console that trusted the caption would tell an IT group its
fleet was years behind on an upgrade it had already finished -- and it would be wrong in a
direction nobody double-checks, because "still on 10" is the answer people expect. So the
build number decides between 10 and 11, and the caption only decides what it alone can.

The second thing is precedence. Every machine has an AD-supplied `ad_os` (from the
directory sync) long before it has an agent new enough to report OS itself, and the two
disagree in both freshness and detail. The agent wins wherever both exist, and the answer
says which one it is, so the console can tell an operator when it is quoting last night's
directory sync rather than the machine.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-os-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@example.com"

import app

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_build_number_beats_the_caption():
    print("\n-- Windows 10 vs 11 is decided by the build, not the name --")
    # The whole reason this function exists. An early 11 machine, verbatim.
    r = app.normalize_os("Microsoft Windows 10 Pro", "22000", None)
    check("a build-22000 machine calling itself Windows 10 is Windows 11",
          r["bucket"] == "windows_11")
    check("...and the label is still what the machine said", r["label"] == "Microsoft Windows 10 Pro")

    check("a genuine Windows 10 build stays Windows 10",
          app.normalize_os("Microsoft Windows 10 Pro", "19045", None)["bucket"] == "windows_10")
    check("a modern Windows 11 build is Windows 11",
          app.normalize_os("Microsoft Windows 11 Pro", "26100", None)["bucket"] == "windows_11")
    # The boundary itself, both sides.
    check("build 21999 is Windows 10",
          app.normalize_os("Microsoft Windows 10 Pro", "21999", None)["bucket"] == "windows_10")

    # With no build to go on the caption is all there is, and it is better than nothing.
    check("no build number falls back to the caption",
          app.normalize_os("Microsoft Windows 11 Pro", None, None)["bucket"] == "windows_11")


def test_the_other_buckets():
    print("\n-- Server and Linux are decided by the caption alone --")
    # Server first, because a Server caption also contains "Windows" -- and a Server build
    # number is on its own scale entirely, so letting the build decide would file a 2022
    # server (build 20348) as Windows 10.
    check("Windows Server is its own bucket",
          app.normalize_os("Microsoft Windows Server 2022 Standard", "20348", None)["bucket"]
          == "windows_server")
    check("...and is not mistaken for a desktop by its build",
          app.normalize_os("Microsoft Windows Server 2019 Datacenter", "17763", None)["bucket"]
          == "windows_server")

    for caption in ("Ubuntu 24.04.1 LTS", "Debian GNU/Linux 12", "Red Hat Enterprise Linux 9",
                    "Rocky Linux 9.4"):
        check(f"{caption} buckets as linux",
              app.normalize_os(caption, None, None)["bucket"] == "linux")


def test_nonsense_is_unknown_rather_than_wrong():
    print("\n-- garbage in, 'unknown' out --")
    for caption, build in (("", None), (None, None), ("   ", "22000"),
                           ("Something Nobody Has Heard Of", None),
                           ("Microsoft Windows 10 Pro", "not-a-number")):
        r = app.normalize_os(caption, build, None)
        expected = "windows_10" if caption == "Microsoft Windows 10 Pro" else "unknown"
        check(f"caption={caption!r} build={build!r} -> {expected}", r["bucket"] == expected)

    check("every bucket this can return is a declared one",
          all(app.normalize_os(c, b, a)["bucket"] in app.OS_BUCKETS
              for c, b, a in (("Windows 11", "26100", None), ("", None, "Windows 10 Pro"),
                              (None, None, None), ("nonsense", "x", "y"))))


def test_the_directory_is_a_fallback_and_says_so():
    print("\n-- AD fills the gap until the agent can answer --")
    r = app.normalize_os(None, None, "Windows 10 Pro")
    check("with no agent answer the directory's is used", r["bucket"] == "windows_10")
    check("...and it is labelled as the directory's", r["source"] == "ad")
    check("...with the directory's own wording", r["label"] == "Windows 10 Pro")

    # The precedence that matters: AD is synced on a schedule and carries no build, so a
    # machine upgraded this morning is still "Windows 10" there for hours.
    r = app.normalize_os("Microsoft Windows 11 Pro", "26100", "Windows 10 Pro")
    check("the agent beats a stale directory record", r["bucket"] == "windows_11")
    check("...and is labelled as the agent's", r["source"] == "agent")

    r = app.normalize_os(None, None, None)
    check("knowing nothing is not attributed to anyone", r["source"] is None)
    check("...and offers no label to render", r["label"] is None)


def test_the_columns_and_the_wire_agree():
    print("\n-- the four fields survive the round trip --")
    # A report carrying ONLY an operating system has to land: that is the shape of the first
    # report from a machine whose identity the hub already knows, and save_machine_info's
    # "did this report say anything?" guard is easy to forget to extend.
    c = app.app.test_client()
    r = c.post("/api/report", json={"machine": "OSTEST-1", "temp": 41.0,
                                    "os_caption": "Microsoft Windows 11 Pro",
                                    "os_version": "10.0.26100", "os_build": "26100",
                                    "os_arch": "64-bit"})
    check("a report carrying an OS is accepted", r.status_code == 200)

    with c.session_transaction() as sess:
        sess["user"] = {"email": "root@example.com"}
    rows = c.get("/api/machines").get_json()
    row = next((m for m in rows if m["machine"] == "OSTEST-1"), None)
    check("the machine appears in the roster", row is not None)
    check("...with the caption it reported", row["os_caption"] == "Microsoft Windows 11 Pro")
    check("...the build", row["os_build"] == "26100")
    check("...the architecture", row["os_arch"] == "64-bit")
    check("...bucketed once, server-side", row["os"]["bucket"] == "windows_11")
    # inventory.js searches and sorts flat fields, so the label is flattened for it.
    check("...and flattened for the Inventory column", row["os_label"] == row["os"]["label"])

    detail = c.get("/api/machines/OSTEST-1").get_json()
    check("the machine page gets the same bucket from the same function",
          detail["os"]["bucket"] == "windows_11")
    check("...plus the build the fleet view throws away", detail["os_build"] == "26100")

    # An OS legitimately changes -- this is the one identity field that must not be
    # write-once, unlike serial_number.
    c.post("/api/report", json={"machine": "OSTEST-1", "temp": 42.0,
                                "os_caption": "Microsoft Windows 11 Enterprise",
                                "os_build": "26200"})
    detail = c.get("/api/machines/OSTEST-1").get_json()
    check("an upgraded machine can say so", detail["os_build"] == "26200"
          and detail["os_caption"] == "Microsoft Windows 11 Enterprise")


def main():
    test_the_build_number_beats_the_caption()
    test_the_other_buckets()
    test_nonsense_is_unknown_rather_than_wrong()
    test_the_directory_is_a_fallback_and_says_so()
    test_the_columns_and_the_wire_agree()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
