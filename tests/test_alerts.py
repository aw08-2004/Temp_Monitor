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

        print("  -- rule kind --")
        rid = alerts.upsert_rule(db_path, "PC-RULE", 7, "Disk nearly full", "C: is at 96%")
        check("rule upsert opens an alert", rid is not None
              and alerts.count_open(db_path) == 1)
        got = alerts.get(db_path, rid)
        check("rule alert carries its machine, rule_id and decoded detail",
              got["machine"] == "PC-RULE" and got["rule_id"] == 7
              and got["detail"]["rule_name"] == "Disk nearly full"
              and got["detail"]["text"] == "C: is at 96%")
        # THE regression this file exists to pin now: a rule alert must never look like a
        # duplicate-serial one. `serial_number` NULL is what the Alerts tab keys its renderer
        # off, and a non-null value here is how the card came out titled "unknown serial"
        # with a Merge button on it.
        check("rule alert has NO serial_number", got["serial_number"] is None)
        check("rule alert has no machines list", got["machines"] == [])

        rid2 = alerts.upsert_rule(db_path, "PC-RULE", 7, "Disk nearly full", "C: is at 97%")
        check("re-upsert refreshes the SAME row", rid2 == rid
              and alerts.count_open(db_path) == 1)
        check("...and counts the refresh", alerts.get(db_path, rid)["detail"]["count"] == 2)

        # A DIFFERENT rule on the SAME machine is a separate alert, not an overwrite -- this
        # is what rule_id in the partial unique index buys.
        other = alerts.upsert_rule(db_path, "PC-RULE", 9, "Uptime > 7 days", "up 9 days")
        check("a second rule on one machine gets its own alert",
              other != rid and alerts.count_open(db_path) == 2)

        # rule and duplicate_serial share the table, not the open-per-subject index.
        alerts.upsert_duplicate(db_path, "S-COEXIST", ["x", "y"])
        check("rule and duplicate_serial coexist", alerts.count_open(db_path) == 3)

        # Ending an episode leaves the alert OPEN; the next match opens a new row beside it.
        check("end_rule_episode reports it ended one",
              alerts.end_rule_episode(db_path, "PC-RULE", 7) is True)
        check("...and the alert is still open", alerts.count_open(db_path) == 3
              and alerts.get(db_path, rid)["episode_ended_at"] is not None)
        rid3 = alerts.upsert_rule(db_path, "PC-RULE", 7, "Disk nearly full", "C: is at 98%")
        check("the next match accumulates a NEW alert", rid3 != rid
              and alerts.count_open(db_path) == 4)

        alerts.resolve_for_rule(db_path, 7)
        check("resolve_for_rule closes only that rule's alerts",
              alerts.count_open(db_path) == 2
              and alerts.get(db_path, rid)["status"] == "resolved"
              and alerts.get(db_path, other)["status"] == "open")
        listed = [a for a in alerts.list_open(db_path) if a["kind"] == alerts.KIND_RULE]
        check("list_open surfaces machine + detail on rule rows",
              listed and all(a.get("machine") and a.get("detail") for a in listed))
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
        # Nothing writes the kind any more, so there is no upsert to re-run here. What still
        # has to hold is that the migrated row remains readable and dismissable -- an alert
        # an operator has not yet acted on must not become unreachable just because the hub
        # stopped raising its kind.
        legacy = again[0]
        check("the migrated row still decodes its detail",
              legacy["detail"] == {"avg_temp": 92.0})
        check("the migrated row is still dismissable",
              alerts.dismiss(db_path, legacy["id"]) is True)
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


def _rule_alerts(machine):
    """Every open rule alert for `machine`, newest activity first."""
    return [a for a in alerts.list_open(app.DB_PATH)
            if a["kind"] == alerts.KIND_RULE and a.get("machine") == machine]


def test_rule_alert_api_and_scope():
    """A rule alert over /api/alerts: its own shape, and scoped to its one machine.

    Both halves are regressions. get_alerts used to special-case exactly ONE per-machine
    kind, so a rule alert took the duplicate_serial branch instead -- where `machines` is
    empty, `serial_number` is null, and the scope guard reads `if involved and not in_scope`.
    An empty `involved` makes that falsy, so the alert was handed to every operator whose
    scope excluded its machine, and the Alerts tab drew it as a duplicate-serial card titled
    "unknown serial" with a Merge button that had nothing to merge.
    """
    print("\n-- rule alerts over /api/alerts, with scope --")
    alerts.upsert_rule(app.DB_PATH, "ruleHot", 11, "Disk nearly full", "C: is at 96%")

    resp = client.get("/api/alerts")
    check("GET /api/alerts 200", resp.status_code == 200)
    row = next((x for x in resp.get_json()
                if x["kind"] == alerts.KIND_RULE and x["machine"] == "ruleHot"), None)
    check("rule alert is returned with its detail",
          row is not None and row["detail"]["rule_name"] == "Disk nearly full"
          and row["detail"]["text"] == "C: is at 96%")
    # What the Alerts tab keys its renderer off. A rule alert that carries a serial or a
    # machines list is one the duplicate-serial card would happily render.
    check("rule alert carries NO serial_number", row and row["serial_number"] is None)
    check("rule alert carries no machines list", row and not row["machines"])

    # Scope. The session user is a break-glass superuser, so machine_filter() is None and
    # everything is visible; narrow it by hand to prove the alert is actually filtered
    # rather than merely happening to be in scope.
    original = app.access.machine_filter
    try:
        app.access.machine_filter = lambda: (lambda m: m != "ruleHot")
        body = client.get("/api/alerts").get_json()
        check("an out-of-scope rule alert is withheld",
              not any(x["kind"] == alerts.KIND_RULE and x.get("machine") == "ruleHot"
                      for x in body))
        count = client.get("/api/alerts/count").get_json()
        check("...and is not counted in the badge either",
              count["count"] == len(body))
    finally:
        app.access.machine_filter = original

    check("it is visible again once in scope",
          any(x["kind"] == alerts.KIND_RULE and x.get("machine") == "ruleHot"
              for x in client.get("/api/alerts").get_json()))

    resp = client.post(f"/api/alerts/{row['id']}/dismiss", json={})
    check("a rule alert can be dismissed", resp.status_code == 200
          and not _rule_alerts("ruleHot"))


def test_retired_high_temp_alert_still_renders():
    """A high_temperature row an operator never dismissed survives the kind's retirement.

    Nothing raises the kind any more, so this writes one the way the old evaluator did. The
    point is that it stays visible, stays scoped on its machine, and stays dismissable --
    retiring a feature must not strand the alerts it already raised.
    """
    print("\n-- retired high_temperature rows stay readable --")
    with alerts.get_conn(app.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO alerts(kind, machine, detail, status, created_at, updated_at) "
            "VALUES (?, 'oldHot', '{\"avg_temp\": 91.4, \"threshold\": 85, "
            "\"window_seconds\": 300}', 'open', 1, 1)", (alerts.KIND_HIGH_TEMP,))

    row = next((x for x in client.get("/api/alerts").get_json()
                if x["kind"] == alerts.KIND_HIGH_TEMP and x["machine"] == "oldHot"), None)
    check("the legacy alert is still served with its detail",
          row is not None and row["detail"]["avg_temp"] == 91.4)

    original = app.access.machine_filter
    try:
        app.access.machine_filter = lambda: (lambda m: m != "oldHot")
        check("...and is still scoped on its machine",
              not any(x.get("machine") == "oldHot"
                      for x in client.get("/api/alerts").get_json()))
    finally:
        app.access.machine_filter = original

    check("...and can still be dismissed",
          client.post(f"/api/alerts/{row['id']}/dismiss", json={}).status_code == 200)


def test_nothing_raises_high_temperature_any_more():
    """The built-in evaluator is gone: hot readings alone must not produce an alert.

    Temperature alerting is an operator-written rule now. The seeded 'High temperature' rule
    ships DISABLED, so a fresh hub that ingests a scorching reading should raise nothing at
    all -- if this fails, some path is still auto-alerting behind the rules engine's back.
    """
    print("\n-- no automatic temperature alert is raised --")
    check("the evaluator function is gone", not hasattr(app, "evaluate_high_temp_once"))
    check("the store cannot write the kind", not hasattr(alerts, "upsert_high_temp"))

    before = alerts.count_open(app.DB_PATH)
    for _ in range(12):
        report("scorching", "SER-HOT-NEW", temp=99.0)
    check("a very hot machine raises no alert by itself",
          alerts.count_open(app.DB_PATH) == before
          and not [a for a in alerts.list_open(app.DB_PATH)
                   if a["kind"] == alerts.KIND_HIGH_TEMP and a.get("machine") == "scorching"])


def test_seeded_high_temp_rule():
    """The migration that carries an upgrading hub across. It runs at import, so by now the
    rule exists -- disabled, targeting everything, alerting via the rules engine."""
    print("\n-- the retired alerter was seeded as a disabled rule --")
    import rules
    seeded = next((r for r in rules.list_rules(app.DB_PATH)
                   if r["name"] == app.HIGH_TEMP_RULE_NAME), None)
    check("the rule exists", seeded is not None)
    check("it is DISABLED, so an operator reviews it before it fires",
          seeded and not seeded["enabled"])
    check("it conditions on the temperature metric",
          seeded and "metric.cpu_temp" in seeded["condition_text"])
    check("it holds for the old averaging window rather than firing on a spike",
          seeded and seeded["for_seconds"] > 0)
    check("it raises an alert", seeded
          and [a for a in seeded["actions"] if a["type"] == "alert"])
    check("it targets the whole fleet by default",
          seeded and seeded["target"]["include"] == [{"kind": "all"}])
    check("re-running the migration is a no-op",
          app.seed_high_temp_rule(app.DB_PATH) is None
          and len([r for r in rules.list_rules(app.DB_PATH)
                   if r["name"] == app.HIGH_TEMP_RULE_NAME]) == 1)


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
    test_rule_alert_api_and_scope()
    test_retired_high_temp_alert_still_renders()
    test_nothing_raises_high_temperature_any_more()
    test_seeded_high_temp_rule()
    test_sidebar_badge_renders()
    test_alert_count_endpoint()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
