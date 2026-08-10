"""Tests the Alerts backend (alerts.py store + the app.py endpoints and the dedup hook
that raises/resolves duplicate_serial alerts).

Two machines online on one serial is a collision the hub won't auto-merge; it raises a
duplicate_serial alert instead. An operator merges from the Alerts tab, or the alert
auto-resolves once one machine goes offline and gets absorbed.

Run from the repo root so `import app` resolves.
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-alerts-test-")
# Point app.py's database at this module's own dir before importing it. app resolves its
# DB from HUB_LOG_DIR now, not the cwd, so a standalone `python tests/test_alerts.py`
# stays isolated from the real logs/. (Under `pytest tests/` app is imported once and
# cached; conftest.py re-points each module per-test.)
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
# The session user these tests sign in as has to be a break-glass superuser, or every
# console endpoint below now 403s on the permission-group layer. Set before importing
# app, which reads ALLOWED_EMAILS at import time; load_dotenv doesn't override an
# already-set env var, so this beats the real .env.
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app
import alerts
import settings

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


client = app.app.test_client()
with client.session_transaction() as sess:
    sess["user"] = {"email": "tester@example.com"}


def report(machine, serial, temp=42.0):
    return client.post("/api/report", json={
        "machine": machine, "temp": temp, "serial_number": serial, "model": "TestModel",
    })


def make_offline(machine, seconds_ago=None):
    if seconds_ago is None:
        seconds_ago = settings.get_int(
            app.DB_PATH, "fleet.dashboard_online_window_seconds") + 180
    ts = app.to_timestamp_str(datetime.now() - timedelta(seconds=seconds_ago))
    with app.get_db_conn() as conn:
        conn.execute("UPDATE machine_info SET updated_at=? WHERE machine=?", (ts, machine))


def open_alert_for(serial):
    return next((a for a in alerts.list_open(app.DB_PATH) if a["serial_number"] == serial), None)


# --------------------------------------------------------------------------- store unit
def test_store_lifecycle():
    print("\n-- alerts store: upsert / list / resolve / dismiss --")
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        alerts.init_alerts_db(db_path)
        aid = alerts.upsert_duplicate(db_path, "S-STORE", ["m1", "m2"])
        check("upsert creates an open alert", aid is not None)
        check("count_open == 1", alerts.count_open(db_path) == 1)

        aid2 = alerts.upsert_duplicate(db_path, "S-STORE", ["m1", "m2", "m3"])
        check("re-upsert refreshes the SAME row (no duplicate)", aid2 == aid)
        check("still only one open alert", alerts.count_open(db_path) == 1)
        got = alerts.get(db_path, aid)
        check("machines refreshed + decoded to a list", got["machines"] == ["m1", "m2", "m3"])

        alerts.resolve_for_serial(db_path, "S-STORE")
        check("resolve closes it", alerts.count_open(db_path) == 0)

        # After resolve, a new collision opens a fresh row (the partial unique index only
        # constrains OPEN rows, so resolved history doesn't block re-raising).
        aid3 = alerts.upsert_duplicate(db_path, "S-STORE", ["m1", "m2"])
        check("can re-raise after resolve", aid3 != aid and alerts.count_open(db_path) == 1)
        check("dismiss returns True and closes it", alerts.dismiss(db_path, aid3) is True)
        check("dismiss again returns False (already closed)", alerts.dismiss(db_path, aid3) is False)
        check("count_open back to 0", alerts.count_open(db_path) == 0)

        print("  -- high_temperature kind --")
        oid = alerts.upsert_high_temp(db_path, "PC-HOT", 91.4, 85, 300)
        check("high-temp upsert opens an alert", oid is not None
              and alerts.count_open(db_path) == 1)
        got = alerts.get(db_path, oid)
        check("high-temp alert carries its machine and decoded detail",
              got["machine"] == "PC-HOT" and got["detail"]["avg_temp"] == 91.4
              and got["detail"]["threshold"] == 85 and got["detail"]["window_seconds"] == 300)
        oid2 = alerts.upsert_high_temp(db_path, "PC-HOT", 88.0, 85, 300)
        check("re-upsert refreshes the SAME row", oid2 == oid
              and alerts.count_open(db_path) == 1)
        check("...and updates the detail", alerts.get(db_path, oid)["detail"]["avg_temp"] == 88.0)
        # A different machine is a separate subject -> its own open row.
        alerts.upsert_high_temp(db_path, "PC-HOT-2", 90.0, 85, 300)
        check("a second hot machine gets its own alert", alerts.count_open(db_path) == 2)
        # high_temperature and duplicate_serial share the table, not the open-per-subject index.
        alerts.upsert_duplicate(db_path, "S-COEXIST", ["x", "y"])
        check("high_temperature and duplicate_serial coexist", alerts.count_open(db_path) == 3)
        alerts.resolve_high_temp(db_path, "PC-HOT")
        check("resolve_high_temp closes only that machine", alerts.count_open(db_path) == 2
              and alerts.get(db_path, oid)["status"] == "resolved")
        listed = [a for a in alerts.list_open(db_path) if a["kind"] == "high_temperature"]
        check("list_open surfaces machine + detail on high-temp rows",
              all(a.get("machine") and a.get("detail") for a in listed))
    finally:
        # Best-effort: on Windows the WAL connections sqlite3 leaves open (a `with conn`
        # block commits but doesn't close) can still hold the temp file. It's in TEMP.
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_kind_rename_migration():
    print("\n-- kind='overheat' rows migrate to 'high_temperature' --")
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        alerts.init_alerts_db(db_path)
        # A row exactly as a pre-rename hub wrote it. Raw SQL on purpose: no current code
        # path can produce the old kind any more, which is the whole point of the test.
        with alerts.get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO alerts(kind, machine, detail, status, created_at, updated_at) "
                "VALUES ('overheat', 'PC-LEGACY', '{\"avg_temp\": 92.0}', 'open', 1, 1)")
        alerts.init_alerts_db(db_path)          # the migration runs on the next hub start
        listed = [a for a in alerts.list_open(db_path) if a.get("machine") == "PC-LEGACY"]
        check("the legacy row is still open, under the new kind",
              len(listed) == 1 and listed[0]["kind"] == alerts.KIND_HIGH_TEMP)
        with alerts.get_conn(db_path) as conn:
            left = conn.execute("SELECT COUNT(*) AS c FROM alerts WHERE kind='overheat'"
                                ).fetchone()["c"]
        check("no rows left under the old kind", left == 0)

        # An already-migrated hub must not be disturbed by a third start, and the machine
        # must still be upsertable -- i.e. the migrated row IS the active episode, not a
        # duplicate the unique index would reject.
        alerts.init_alerts_db(db_path)
        again = [a for a in alerts.list_open(db_path) if a.get("machine") == "PC-LEGACY"]
        check("re-running the migration changes nothing", len(again) == 1)
        alerts.upsert_high_temp(db_path, "PC-LEGACY", 95.0, 85, 300)
        check("the migrated row is refreshed, not duplicated",
              len([a for a in alerts.list_open(db_path)
                   if a.get("machine") == "PC-LEGACY"]) == 1)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


# ------------------------------------------------------------------- ingest raises alert
def test_both_online_raises_alert():
    print("\n-- two online machines on one serial raise a duplicate_serial alert --")
    report("alertA", "SER-AL-1")
    report("alertB", "SER-AL-1")            # ingest trigger sees two online -> alert
    a = open_alert_for("SER-AL-1")
    check("alert raised", a is not None)
    check("alert lists both machines", a and set(a["machines"]) == {"alertA", "alertB"})

    resp = client.get("/api/alerts")
    check("GET /api/alerts 200", resp.status_code == 200)
    payload = resp.get_json()
    row = next((x for x in payload if x["serial_number"] == "SER-AL-1"), None)
    check("api enriches machines with live status", row is not None
          and all(m["status"] == "online" for m in row["machines"]))


def test_merge_endpoint_resolves_alert():
    print("\n-- operator merge via endpoint absorbs the victim and clears the alert --")
    report("mergeKeep", "SER-AL-2")
    report("mergeDrop", "SER-AL-2")
    check("alert present before merge", open_alert_for("SER-AL-2") is not None)

    resp = client.post("/api/machines/merge",
                       json={"survivor": "mergeKeep", "victims": ["mergeDrop"]})
    check("merge 200", resp.status_code == 200)
    with app.get_db_conn() as conn:
        drop_gone = conn.execute(
            "SELECT COUNT(*) AS c FROM machine_info WHERE machine='mergeDrop'"
        ).fetchone()["c"] == 0
    check("victim merged away", drop_gone)
    check("alert resolved after merge", open_alert_for("SER-AL-2") is None)


def test_alert_auto_resolves_when_one_goes_offline():
    print("\n-- alert auto-resolves once a colliding machine goes offline + is merged --")
    report("flapA", "SER-AL-3")
    report("flapB", "SER-AL-3")
    check("alert raised while both online", open_alert_for("SER-AL-3") is not None)

    # The survivor has to be an ENROLLED machine or resolve_serial_group refuses to merge:
    # its trigger is the unauthenticated /api/report, so an unenrolled hostname is an
    # identity claim nobody vouched for. Real machines are always enrolled; see
    # test_dedup.test_unenrolled_survivor_never_absorbs_a_real_machine for the case this
    # guards against.
    with app.get_db_conn() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, machine, token_hash, enrolled_at, last_seen, "
            "revoked) VALUES ('agent-flapA', 'flapA', 'h', 0, 0, 0)")

    make_offline("flapB")
    report("flapA", "SER-AL-3")              # flapB now offline -> auto-merge, resolve
    check("alert cleared", open_alert_for("SER-AL-3") is None)
    with app.get_db_conn() as conn:
        b_gone = conn.execute(
            "SELECT COUNT(*) AS c FROM machine_info WHERE machine='flapB'"
        ).fetchone()["c"] == 0
    check("offline duplicate absorbed", b_gone)


def test_dismiss_endpoint():
    print("\n-- dismiss endpoint closes an alert --")
    report("dismA", "SER-AL-4")
    report("dismB", "SER-AL-4")
    a = open_alert_for("SER-AL-4")
    check("alert raised", a is not None)
    resp = client.post(f"/api/alerts/{a['id']}/dismiss", json={})
    check("dismiss 200", resp.status_code == 200)
    check("alert closed", open_alert_for("SER-AL-4") is None)
    resp = client.post(f"/api/alerts/{a['id']}/dismiss", json={})
    check("dismiss again -> 404", resp.status_code == 404)


def test_merge_endpoint_validation():
    print("\n-- merge endpoint input validation --")
    check("missing survivor -> 400",
          client.post("/api/machines/merge", json={"victims": ["x"]}).status_code == 400)
    check("empty victims -> 400",
          client.post("/api/machines/merge",
                     json={"survivor": "y", "victims": []}).status_code == 400)
    report("valSurv", "SER-AL-5")
    check("unknown victim -> 404",
          client.post("/api/machines/merge",
                     json={"survivor": "valSurv", "victims": ["ghost"]}).status_code == 404)
    check("unknown survivor -> 404",
          client.post("/api/machines/merge",
                     json={"survivor": "ghost", "victims": ["valSurv"]}).status_code == 404)


def test_auth_required():
    print("\n-- alerts endpoints require a session --")
    anon = app.app.test_client()
    check("GET /api/alerts unauthenticated -> 401", anon.get("/api/alerts").status_code == 401)
    # The count endpoint swallows its own errors to protect the badge; that must not
    # extend to answering an anonymous caller with a fleet-wide number.
    check("GET /api/alerts/count unauthenticated -> 401",
          anon.get("/api/alerts/count").status_code == 401)
    check("merge unauthenticated -> 401",
          anon.post("/api/machines/merge",
                    json={"survivor": "a", "victims": ["b"]}).status_code == 401)


def _seed_readings(machine, temps, base, step=5):
    """Insert `temps` for `machine`, one every `step`s ending at epoch `base` (newest
    first). Uses the same readings table the evaluator averages over."""
    with app.get_db_conn() as conn:
        for i, t in enumerate(temps):
            ts = base - i * step
            conn.execute(
                "INSERT OR IGNORE INTO readings(ts_text, ts_epoch, machine, temp) "
                "VALUES (?, ?, ?, ?)", (str(ts), ts, machine, t))


def _high_temps(machine):
    """Every open high-temperature alert for `machine`, newest activity first."""
    return [a for a in alerts.list_open(app.DB_PATH)
            if a["kind"] == "high_temperature" and a.get("machine") == machine]


def _open_high_temp(machine):
    """The ACTIVE episode for `machine` (the machine is hot right now), if any. Ended
    episodes stay open and visible, so this deliberately ignores them."""
    return next((a for a in _high_temps(machine) if not a.get("episode_ended_at")), None)


def test_high_temp_evaluator():
    print("\n-- high-temperature evaluator: average, spike immunity, episodes, offline --")
    settings.set_many(app.DB_PATH, {
        "hub.high_temp_threshold": 80,
        "hub.high_temp_avg_window_seconds": 300,
        "fleet.dashboard_online_window_seconds": 120,
    })
    now = 1_950_000_000

    # Sustained: 40 readings at 90 over ~200s -> average 90 -> alert.
    _seed_readings("ovHot", [90] * 40, base=now)
    app.evaluate_high_temp_once(app.DB_PATH, now=now)
    a = _open_high_temp("ovHot")
    check("a sustained hot average raises exactly one alert",
          a is not None and a["detail"]["avg_temp"] == 90.0)
    check("...carrying the threshold and window it was judged against",
          a["detail"]["threshold"] == 80 and a["detail"]["window_seconds"] == 300)

    # A single 120 spike in an otherwise-50 window averages ~51.8 -> NO alert. This is the
    # whole point of the feature over the old instantaneous flag.
    _seed_readings("ovSpike", [50] * 39 + [120], base=now)
    app.evaluate_high_temp_once(app.DB_PATH, now=now)
    check("a lone spike inside a cool window does NOT raise",
          _open_high_temp("ovSpike") is None)

    # Refreshing while still hot must not pile up rows, and must remember the peak.
    # Seeded past the 300s window so the average is purely the new (cooler but still hot)
    # readings, not a blend with the 90s above.
    _seed_readings("ovHot", [86] * 40, base=now + 400)
    app.evaluate_high_temp_once(app.DB_PATH, now=now + 400)
    check("staying hot refreshes the SAME episode", len(_high_temps("ovHot")) == 1)
    check("...tracking the current average and the episode's peak",
          _open_high_temp("ovHot")["detail"]["avg_temp"] == 86.0
          and _open_high_temp("ovHot")["detail"]["peak_temp"] == 90.0)

    # Newer cool readings pull the average down -> the episode ends, but the alert stays
    # open and visible until an operator dismisses it.
    _seed_readings("ovHot", [50] * 40, base=now + 1000)
    app.evaluate_high_temp_once(app.DB_PATH, now=now + 1000)
    check("cooling back down ends the episode without closing the alert",
          _open_high_temp("ovHot") is None and len(_high_temps("ovHot")) == 1
          and _high_temps("ovHot")[0]["episode_ended_at"] == now + 1000)

    # ...and heating up again ACCUMULATES a second alert instead of overwriting the first.
    _seed_readings("ovHot", [95] * 40, base=now + 5000)
    app.evaluate_high_temp_once(app.DB_PATH, now=now + 5000)
    episodes = _high_temps("ovHot")
    check("a second hot spell raises a NEW alert beside the old one", len(episodes) == 2)
    check("...the earlier episode keeping its own numbers",
          sorted(e["detail"]["peak_temp"] for e in episodes) == [90.0, 95.0])
    # Only one of them is live, so the unique index still bounds the table.
    check("...and only one episode is active at a time",
          sum(1 for e in episodes if not e["episode_ended_at"]) == 1)
    for e in episodes:
        alerts.dismiss(app.DB_PATH, e["id"])

    # Hot but last reading older than the online window -> not currently online, no alert.
    _seed_readings("ovGone", [95] * 40, base=now - 10_000)
    app.evaluate_high_temp_once(app.DB_PATH, now=now)
    check("a hot machine that stopped reporting is not alerted",
          _open_high_temp("ovGone") is None)

    # A machine that was hot and then goes offline entirely (drops out of the window) has
    # its episode ended, not left dangling open forever.
    _seed_readings("ovDrop", [95] * 40, base=now + 2000)
    app.evaluate_high_temp_once(app.DB_PATH, now=now + 2000)
    check("hot machine alerts while online", _open_high_temp("ovDrop") is not None)
    # Evaluate far in the future: ovDrop's readings are now well outside the window.
    app.evaluate_high_temp_once(app.DB_PATH, now=now + 2000 + 100_000)
    check("...and its episode ends once it drops out of the window entirely",
          _open_high_temp("ovDrop") is None and len(_high_temps("ovDrop")) == 1)


def test_high_temp_api_and_scope():
    print("\n-- high-temperature alerts over /api/alerts, with scope --")
    settings.set_many(app.DB_PATH, {
        "hub.high_temp_threshold": 80,
        "hub.high_temp_avg_window_seconds": 300,
        "fleet.dashboard_online_window_seconds": 120,
    })
    now = 1_960_000_000
    _seed_readings("apiHot", [88] * 40, base=now)
    app.evaluate_high_temp_once(app.DB_PATH, now=now)

    resp = client.get("/api/alerts")
    check("GET /api/alerts 200", resp.status_code == 200)
    row = next((x for x in resp.get_json()
                if x["kind"] == "high_temperature" and x["machine"] == "apiHot"), None)
    check("high-temp alert is returned with its detail",
          row is not None and row["detail"]["avg_temp"] == 88.0)

    resp = client.post(f"/api/alerts/{row['id']}/dismiss", json={})
    check("a high-temp alert can be dismissed", resp.status_code == 200
          and _open_high_temp("apiHot") is None)


def test_sidebar_badge_renders():
    print("\n-- sidebar shows the open-alert badge --")
    report("badgeA", "SER-AL-6")
    report("badgeB", "SER-AL-6")             # ensure at least one open alert
    resp = client.get("/alerts")
    check("GET /alerts page 200", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("Alerts page renders", "Alerts" in body)
    # The span is now always in the markup (the Alerts page rewrites it live after a
    # dismiss), so presence alone proves nothing -- it has to be the visible, counted one.
    badge = re.search(r'<span class="sidebar__badge"[^>]*>([^<]*)</span>', body)
    check("badge shown when alerts are open",
          badge is not None and "hidden" not in badge.group(0)
          and badge.group(1).strip().isdigit())


def test_alert_count_endpoint():
    # The badge poller's endpoint. What matters is that it agrees with the number the
    # server rendered into the sidebar and with the list the Alerts page shows -- a count
    # computed a second way is a count that drifts.
    print("\n-- /api/alerts/count backs the badge poller --")
    report("cntA", "SER-AL-7")
    report("cntB", "SER-AL-7")

    resp = client.get("/api/alerts/count")
    check("GET /api/alerts/count 200", resp.status_code == 200)
    count = resp.get_json()["count"]
    check("count is a positive number", isinstance(count, int) and count > 0)

    listed = client.get("/api/alerts").get_json()
    check("count matches the alert list", count == len(listed))

    body = client.get("/alerts").get_data(as_text=True)
    badge = re.search(r'<span class="sidebar__badge"[^>]*>([^<]*)</span>', body)
    check("count matches the server-rendered badge",
          badge is not None and badge.group(1).strip() == str(count))

    alerts.dismiss(app.DB_PATH, listed[0]["id"])
    after = client.get("/api/alerts/count").get_json()["count"]
    check("dismissing an alert lowers the count", after == count - 1)


if __name__ == "__main__":
    test_store_lifecycle()
    test_kind_rename_migration()
    test_both_online_raises_alert()
    test_merge_endpoint_resolves_alert()
    test_alert_auto_resolves_when_one_goes_offline()
    test_dismiss_endpoint()
    test_merge_endpoint_validation()
    test_auth_required()
    test_high_temp_evaluator()
    test_high_temp_api_and_scope()
    test_sidebar_badge_renders()
    test_alert_count_endpoint()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
