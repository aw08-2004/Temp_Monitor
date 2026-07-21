"""Tests the Alerts backend (alerts.py store + the app.py endpoints and the dedup hook
that raises/resolves duplicate_serial alerts).

Two machines online on one serial is a collision the hub won't auto-merge; it raises a
duplicate_serial alert instead. An operator merges from the Alerts tab, or the alert
auto-resolves once one machine goes offline and gets absorbed.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    finally:
        # Best-effort: on Windows the WAL connections sqlite3 leaves open (a `with conn`
        # block commits but doesn't close) can still hold the temp file. It's in TEMP.
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
    resp = client.post(f"/api/alerts/{a['id']}/dismiss")
    check("dismiss 200", resp.status_code == 200)
    check("alert closed", open_alert_for("SER-AL-4") is None)
    resp = client.post(f"/api/alerts/{a['id']}/dismiss")
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
    check("merge unauthenticated -> 401",
          anon.post("/api/machines/merge",
                    json={"survivor": "a", "victims": ["b"]}).status_code == 401)


def test_sidebar_badge_renders():
    print("\n-- sidebar shows the open-alert badge --")
    report("badgeA", "SER-AL-6")
    report("badgeB", "SER-AL-6")             # ensure at least one open alert
    resp = client.get("/alerts")
    check("GET /alerts page 200", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("Alerts page renders", "Alerts" in body)
    check("badge shown when alerts are open", "sidebar__badge" in body)


if __name__ == "__main__":
    test_store_lifecycle()
    test_both_online_raises_alert()
    test_merge_endpoint_resolves_alert()
    test_alert_auto_resolves_when_one_goes_offline()
    test_dismiss_endpoint()
    test_merge_endpoint_validation()
    test_auth_required()
    test_sidebar_badge_renders()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
