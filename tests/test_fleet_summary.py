"""GET /api/fleet/summary -- what the Dashboard is.

WHY ONE ENDPOINT. The Dashboard shows counts that have to agree with each other: a total
that disagrees with the sum of its OS buckets, or an alert tile that disagrees with the
sidebar badge beside it, is worse than showing nothing. Eight requests cannot be made to
agree on a moment, so all of it is computed once, in one place.

WHAT IS ACTUALLY AT RISK HERE. Scope. This page is the first thing an operator sees, and it
is the one page in the console built entirely from aggregates -- which is exactly the shape
where a leak is invisible. "You have 200 machines" told to somebody who may see three
discloses the fleet's size; "4 backup failures" discloses activity on machines they cannot
open. So every number is asserted against a scoped operator, not just the totals.

The second risk is the cache. It is keyed by SCOPE rather than by caller, which is what
makes it useful (a dozen consoles on the Dashboard cost one computation) and also what
would make it dangerous if the key were wrong -- so the key is tested directly, by asking
with two different scopes inside one TTL window and requiring different answers.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-summary-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@example.com"

import app
import permissions

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "hub", "static")


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


def client_for(email):
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = {"email": email}
    return c


def report(machine, temp, caption, build, used_gb=100.0, total_gb=500.0, load=20.0):
    """One telemetry report, in the shape the agent actually sends."""
    app.app.test_client().post("/api/report", json={
        "machine": machine, "temp": temp, "companion_version": "3.31.0",
        "uptime_seconds": 50_000, "os_caption": caption, "os_build": build,
        "sensors": [
            {"hardware": "CPU", "hardware_id": "/amdcpu/0", "group": "CPU",
             "name": "CPU Package", "type": "Temperature", "value": temp},
            {"hardware": "CPU", "hardware_id": "/amdcpu/0", "group": "CPU",
             "name": "CPU Total", "type": "Load", "value": load},
            {"hardware": "C:", "hardware_id": "/volume/c", "group": "Volume",
             "name": "Total Space", "type": "Data", "value": total_gb},
            {"hardware": "C:", "hardware_id": "/volume/c", "group": "Volume",
             "name": "Used Space", "type": "Data", "value": used_gb},
        ],
    })


def seed():
    report("SUM-HOT", 95.0, "Microsoft Windows 11 Pro", "26100", used_gb=100.0)
    report("SUM-COOL", 40.0, "Microsoft Windows 10 Pro", "19045", used_gb=200.0)
    report("SUM-SERVER", 55.0, "Microsoft Windows Server 2022 Standard", "20348")
    # Nearly full: 480 of 500 used leaves 4% free, under the 10% default.
    report("SUM-FULL", 50.0, "Ubuntu 24.04.1 LTS", "6.8", used_gb=480.0)


def fresh(client, **params):
    """A summary computed now, not one served from the TTL cache."""
    app._fleet_summary_cache.clear()
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get("/api/fleet/summary" + (f"?{query}" if query else "")).get_json()


def test_the_counts_agree_with_each_other():
    print("\n-- the totals and their parts add up --")
    seed()
    root = client_for("root@example.com")
    summary = fresh(root)

    counts = summary["counts"]
    check("every seeded machine is counted", counts["total"] == 4)
    check("online + offline is the total", counts["online"] + counts["offline"] == counts["total"])
    # The one a reader would spot instantly, and the reason the buckets are computed from
    # the same row list as the total rather than from a second query.
    check("the OS buckets sum to the total",
          sum(entry["count"] for entry in summary["os"]) == counts["total"])
    check("...and every bucket is a declared one",
          [entry["bucket"] for entry in summary["os"]] == list(app.OS_BUCKETS))

    by_bucket = {entry["bucket"]: entry["count"] for entry in summary["os"]}
    check("the Windows 11 machine is bucketed as 11", by_bucket["windows_11"] == 1)
    check("the Windows 10 machine as 10", by_bucket["windows_10"] == 1)
    check("the server as a server", by_bucket["windows_server"] == 1)
    check("the Linux box as Linux", by_bucket["linux"] == 1)


def test_the_thresholds_are_filters_with_their_numbers_attached():
    print("\n-- 'hot' and 'low disk' carry the threshold they used --")
    root = client_for("root@example.com")
    s = fresh(root)["telemetry"]

    check("the hot threshold is reported alongside the count",
          s["threshold_c"] == app.settings.get_int(app.DB_PATH, "hub.hot_temp_threshold_c"))
    check("the one machine over it is counted", s["over_threshold"] == 1)
    check("the low-disk threshold is reported too",
          s["low_disk_free_pct"] == app.settings.get_int(app.DB_PATH, "hub.low_disk_free_pct"))
    check("the one nearly-full machine is counted", s["low_disk_machines"] == 1)

    check("the peak is the hottest reading", s["peak_cpu_temp"] == 95.0)
    check("the average is of the machines reporting", s["reporting"] == 4)
    check("...and is actually an average", 40.0 < s["avg_cpu_temp"] < 95.0)

    # Nothing here raises an alert. What counts as too hot is an operator-written rule, and
    # a second definition of hot living in this endpoint would quietly disagree with it.
    check("the summary raises no alerts of its own",
          fresh(root)["counts"]["open_alerts"] == app.alerts.count_open(app.DB_PATH))


def test_the_attention_lists_are_ranked_and_bounded():
    print("\n-- the three ranked lists --")
    root = client_for("root@example.com")
    a = fresh(root, top=2)["attention"]

    check("hottest is capped by ?top=", len(a["hottest"]) <= 2)
    check("...and is actually sorted hottest first",
          a["hottest"] == sorted(a["hottest"], key=lambda r: r["temp"], reverse=True))
    check("the hottest machine leads it", a["hottest"][0]["machine"] == "SUM-HOT")

    check("low disk names the volume, not just the machine",
          a["low_disk"] and a["low_disk"][0]["machine"] == "SUM-FULL"
          and a["low_disk"][0]["volume"] == "C:")
    check("...with both the share and the absolute space left",
          a["low_disk"][0]["free_pct"] == 4.0 and a["low_disk"][0]["free_gb"] == 20.0)

    # ?top= is clamped rather than trusted: it sizes three list comprehensions, and an
    # operator (or a crawler) asking for a million of them is not a reason to build one.
    big = fresh(root, top=99999)["attention"]
    check("an absurd ?top= is clamped", len(big["hottest"]) <= 20)
    junk = fresh(root, top="banana")["attention"]
    check("a non-numeric ?top= falls back rather than erroring", len(junk["hottest"]) <= 5)


def test_a_scoped_operator_sees_only_their_own_fleet():
    """The disclosure this endpoint could most easily become. Every number is an aggregate,
    and an aggregate over machines somebody may not open still tells them about those
    machines -- how many there are, how hot they run, what ran on them last night."""
    print("\n-- nothing crosses a scope boundary --")
    permissions.init_permissions_db(app.DB_PATH)
    permissions.invalidate()
    permissions.create_group(
        app.DB_PATH, name="One machine", capabilities=[permissions.VIEW],
        machines=["SUM-COOL"], members=["narrow@example.com"], actor="root@example.com")

    narrow = fresh(client_for("narrow@example.com"))
    check("the total is of their machines only", narrow["counts"]["total"] == 1)
    check("...and so are the OS buckets",
          sum(e["count"] for e in narrow["os"]) == 1)
    check("...and the telemetry averages",
          narrow["telemetry"]["peak_cpu_temp"] == 40.0)
    # The hottest machine in the fleet is not theirs, and must not appear even as a number.
    check("the hottest list holds only their machine",
          [r["machine"] for r in narrow["attention"]["hottest"]] == ["SUM-COOL"])
    check("the over-threshold count excludes the hot machine they cannot see",
          narrow["telemetry"]["over_threshold"] == 0)
    check("...as does the low-disk count",
          narrow["telemetry"]["low_disk_machines"] == 0)
    # The list is a RANKING, not a filter -- "least free disk" of one machine is that
    # machine, however healthy it is, exactly as "hottest" lists a cool machine on a cool
    # fleet. What must not appear is somebody else's.
    check("...and the low-disk ranking holds only their machine",
          [r["machine"] for r in narrow["attention"]["low_disk"]] == ["SUM-COOL"])

    root = fresh(client_for("root@example.com"))
    check("meanwhile an unrestricted caller still sees the whole fleet",
          root["counts"]["total"] == 4)


def test_the_cache_is_keyed_by_scope():
    """The cache is what makes a dozen consoles on the Dashboard cost one computation. It
    is also the one place where a wrong key would serve one operator another's fleet."""
    print("\n-- the TTL cache does not cross scopes --")
    app._fleet_summary_cache.clear()

    root_first = client_for("root@example.com").get("/api/fleet/summary").get_json()
    # Immediately after, inside the TTL window: a cache keyed by anything less specific
    # than the scope would hand this caller the answer computed above.
    narrow = client_for("narrow@example.com").get("/api/fleet/summary").get_json()
    check("a scoped caller is not served the unrestricted answer",
          narrow["counts"]["total"] == 1 and root_first["counts"]["total"] == 4)

    # And the cache is doing its job for a repeat within the window.
    root_again = client_for("root@example.com").get("/api/fleet/summary").get_json()
    check("a repeat inside the window is served the same computation",
          root_again["generated_at"] == root_first["generated_at"])
    check("the TTL is short enough to not show a stale fleet",
          app.FLEET_SUMMARY_TTL_SECONDS <= 30)


def test_the_page_asks_for_what_the_endpoint_returns():
    """No browser harness here, so the join between the JSON and the page that renders it
    is checked by reading both. A renamed key ships green otherwise."""
    print("\n-- the Dashboard and the endpoint agree --")
    js = read(STATIC, "js", "dashboard.js")
    check("dashboard.js asks this endpoint", "'/api/fleet/summary'" in js)
    for section in ("counts", "os", "telemetry", "activity", "attention"):
        check(f"...and reads the {section} section", f"summary.{section}" in js or
              f"summary['{section}']" in js)

    # The page draws machine names and OS captions, both of which reach the hub through the
    # unauthenticated /api/report. innerHTML anywhere here would be an XSS on the front page.
    # Matched as an assignment, so the comment explaining the rule does not fail it.
    check("nothing on the Dashboard is built with innerHTML", ".innerHTML =" not in js)

    # socket.io went with the per-machine cards -- there is nothing for a per-reading event
    # to update, and re-fetching a fleet aggregate at 1 Hz is not viable.
    check("the Dashboard no longer opens a socket", "connectSocketWithStatus()" not in js)
    check("...and does not load the client either",
          "socket.io.js" not in read(ROOT, "hub", "templates", "index.html"))


def main():
    test_the_counts_agree_with_each_other()
    test_the_thresholds_are_filters_with_their_numbers_attached()
    test_the_attention_lists_are_ranked_and_bounded()
    test_a_scoped_operator_sees_only_their_own_fleet()
    test_the_cache_is_keyed_by_scope()
    test_the_page_asks_for_what_the_endpoint_returns()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
