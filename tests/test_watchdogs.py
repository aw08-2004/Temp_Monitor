"""watchdogs.py -- agent-local self-healing watchdogs (roadmap #20).

The silent failures this file exists to catch. Every one of them looks, from the console,
exactly like a fleet that is fine -- which is the whole problem with testing a feature whose
success condition is that nothing happens:

  * **A document that stops being sent.** The heartbeat only sends one when the version
    differs, so a `document_version` that is not stable across two identical resolutions costs
    every machine a re-application every ten seconds, and one that is not sensitive to an edit
    leaves every machine holding the OLD watchdog forever. Both read as "watchdogs are
    working".
  * **An empty document degrading into no document.** Removing a machine from a watchdog's
    target has to be able to STOP it watching. If `resolve_for` answered with nothing rather
    than with `watchdogs: []`, the agent would read it as "the hub had nothing to say" and go
    on restarting a service somebody deliberately stopped watching -- with no way to stop it
    short of uninstalling the agent.
  * **A watchdog that outgrows its author's scope.** The save-time check cannot bind a DYNAMIC
    target, so a site-scoped operator's `all` would start restarting services on machines they
    have never been able to see, with the next enrolment and with no action from them.
  * **A protected service accepted.** `RpcSs` restarted every thirty seconds is a machine
    spending its life on refused SCM calls; a watchdog on the agent itself can never fire at
    all, because a stopped agent evaluates nothing.
  * **An escalation that never escalates, or never clears.** A machine reporting `given_up` on
    every heartbeat must be ONE alert, not one per tick; and a machine that recovers must end
    the episode, or the next real failure is indistinguishable from the old one.
  * **Two watchdogs colliding in the alerts table.** Both raise a per-machine alert on one PC,
    and before `watchdog_id` widened the partial unique index the second would silently never
    have been raised.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import alerts
import rules
import watchdogs

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


MACHINES = [("PC-1", "OU=Sales,DC=corp"), ("PC-2", "OU=Sales,DC=corp"),
            ("PC-3", "OU=Lab,DC=corp")]


def seed(db):
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS machine_info "
                     "(machine TEXT PRIMARY KEY, ad_ou TEXT, ad_dn TEXT)")
        for machine, ou in MACHINES:
            conn.execute("INSERT OR IGNORE INTO machine_info VALUES (?, ?, '')", (machine, ou))
    alerts.init_alerts_db(db)
    rules.init_rules_db(db)
    watchdogs.init_watchdogs_db(db)
    watchdogs.init_watchdogs_db(db)     # idempotent


def all_machines():
    return {"include": [{"kind": "all"}], "exclude": []}


def one(*names):
    return {"include": [{"kind": "machines", "machines": list(names)}], "exclude": []}


def save(db, **payload):
    body = {"name": "Spooler", "service": "Spooler", "target": all_machines()}
    scope = payload.pop("author_scope", None)
    body.update(payload)
    return watchdogs.save_watchdog(db, body, actor="root@example.com", author_scope=scope)


def test_validation(db):
    print("\n-- validation --")
    err, _ = save(db, name="")
    check("a nameless watchdog is refused", err is not None)
    err, _ = save(db, service="")
    check("one with no service is refused", err is not None)
    err, _ = save(db, target={"include": [], "exclude": []})
    check("one with no include selector is refused, not defaulted to every PC", err is not None)

    for service in ("RpcSs", "rpcss", " WinMgmt ", "TempMonitorAgent"):
        err, _ = save(db, service=service)
        check(f"'{service.strip()}' is refused however it is spelled", err is not None)

    err, _ = save(db, grace_seconds=watchdogs.MAX_GRACE_SECONDS + 1)
    check("a grace period past the bound is refused, not clamped", err is not None)
    err, _ = save(db, max_restarts=1)
    check("a restart limit of 1 is refused -- it would escalate on the first crash",
          err is not None)
    err, _ = save(db, window_seconds=60)
    check("a flap window under the floor is refused", err is not None)

    err, watchdog_id = save(db, grace_seconds=None, max_restarts=None, window_seconds=None)
    stored = watchdogs.get_watchdog(db, watchdog_id)
    check("the defaults fill in when the limits are left out",
          err is None and stored["grace_seconds"] == watchdogs.DEFAULT_GRACE_SECONDS
          and stored["max_restarts"] == watchdogs.DEFAULT_MAX_RESTARTS
          and stored["window_seconds"] == watchdogs.DEFAULT_WINDOW_SECONDS)
    watchdogs.delete_watchdog(db, watchdog_id)


def test_document(db):
    print("\n-- the document a machine holds --")
    err, wid = save(db, name="Spooler everywhere")
    check("a fleet-wide watchdog saves", err is None)

    document = watchdogs.resolve_for(db, "PC-1")
    check("it reaches PC-1", [e["id"] for e in document["watchdogs"]] == [wid])
    check("the version is a short stable hash",
          document["version"] and document["version"] == watchdogs.resolve_for(db, "PC-1")["version"])
    check("...and is the same on every machine the same document reaches",
          document["version"] == watchdogs.resolve_for(db, "PC-2")["version"])

    before = document["version"]
    watchdogs.save_watchdog(db, {"name": "Spooler everywhere", "service": "Spooler",
                                 "target": all_machines(), "grace_seconds": 120},
                            watchdog_id=wid, actor="root@example.com")
    after = watchdogs.resolve_for(db, "PC-1")["version"]
    check("editing a limit changes the version, so the machine re-applies", before != after)

    watchdogs.set_enabled(db, wid, False)
    empty = watchdogs.resolve_for(db, "PC-1")
    check("a disabled watchdog leaves the machine an EMPTY document, not no document",
          empty["watchdogs"] == [] and isinstance(empty.get("version"), str) and empty["version"])
    check("...and that empty document has its own version, so the machine notices",
          empty["version"] != after)
    watchdogs.set_enabled(db, wid, True)

    # A machine the hub has no row for yet: mid-enrolment, and `all` is answerable without one.
    check("a machine with no machine_info row still resolves",
          [e["id"] for e in watchdogs.resolve_for(db, "PC-NEW")["watchdogs"]] == [wid])

    err, narrow = save(db, name="Lab only", service="W32Time",
                       target={"include": [{"kind": "ad_ou", "ou": "OU=Lab,DC=corp"}],
                               "exclude": []})
    check("an OU-targeted watchdog reaches only that OU",
          err is None
          and [e["id"] for e in watchdogs.resolve_for(db, "PC-3")["watchdogs"]] == [wid, narrow]
          and [e["id"] for e in watchdogs.resolve_for(db, "PC-1")["watchdogs"]] == [wid])
    watchdogs.delete_watchdog(db, narrow)
    return wid


def test_author_scope(db):
    print("\n-- a watchdog cannot outgrow its author --")
    err, scoped = save(db, name="Scoped", service="W32Time", target=all_machines(),
                       author_scope=["PC-1"])
    check("a scoped author may save a dynamic target", err is None)
    check("...but it only reaches the machines they could see",
          [e["id"] for e in watchdogs.resolve_for(db, "PC-1")["watchdogs"]].count(scoped) == 1
          and scoped not in [e["id"] for e in watchdogs.resolve_for(db, "PC-2")["watchdogs"]])

    # The failure this exists for: a machine that did not exist when the watchdog was saved.
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR IGNORE INTO machine_info VALUES ('PC-NEW-2', '', '')")
    check("...including one enrolled afterwards, which the save-time check could not have seen",
          scoped not in [e["id"] for e in watchdogs.resolve_for(db, "PC-NEW-2")["watchdogs"]])

    err, wide = save(db, name="Unrestricted", service="W32Time", author_scope=None)
    check("an unrestricted author stays fully dynamic",
          err is None and wide in [e["id"] for e in watchdogs.resolve_for(db, "PC-NEW-2")["watchdogs"]])
    watchdogs.delete_watchdog(db, scoped)
    watchdogs.delete_watchdog(db, wide)


def test_reports(db, wid):
    print("\n-- what a machine reports back --")
    now = 1_700_000_000
    summary = watchdogs.record_report(db, "PC-1", {"states": [
        {"id": wid, "status": "ok"}]}, now=now)
    check("a healthy report is stored and written as an event the first time",
          summary["stored"] == 1 and summary["events"] == 1 and summary["escalated"] == 0)

    summary = watchdogs.record_report(db, "PC-1", {"states": [
        {"id": wid, "status": "ok"}]}, now=now + 30)
    check("...and repeating it writes no second event", summary["events"] == 0)

    summary = watchdogs.record_report(db, "PC-1", {"states": [
        {"id": wid, "status": "restarted", "restarts": 1, "last_restart_at": now + 60,
         "detail": "restarted the Print Spooler service"}]}, now=now + 60)
    check("a restart is an event", summary["events"] == 1 and summary["escalated"] == 0)

    summary = watchdogs.record_report(db, "PC-1", {"states": [
        {"id": wid, "status": "restarted", "restarts": 2, "last_restart_at": now + 200,
         "detail": "restarted the Print Spooler service"}]}, now=now + 200)
    check("a SECOND restart is a second event, though the status word did not move",
          summary["events"] == 1)

    summary = watchdogs.record_report(db, "PC-1", {"states": [
        {"id": wid, "status": "given_up", "restarts": 3, "last_restart_at": now + 300,
         "detail": "keeps stopping"}]}, now=now + 300)
    check("giving up escalates", summary["escalated"] == 1)
    open_alerts = [a for a in alerts.list_open(db) if a["kind"] == alerts.KIND_WATCHDOG]
    check("...as exactly one open alert naming the watchdog and the service",
          len(open_alerts) == 1
          and open_alerts[0]["detail"]["service"] == "Spooler"
          and open_alerts[0]["detail"]["watchdog_id"] == wid)

    watchdogs.record_report(db, "PC-1", {"states": [
        {"id": wid, "status": "given_up", "restarts": 3, "last_restart_at": now + 300}]},
        now=now + 330)
    open_alerts = [a for a in alerts.list_open(db) if a["kind"] == alerts.KIND_WATCHDOG]
    check("repeating it refreshes the SAME episode rather than piling up rows",
          len(open_alerts) == 1 and open_alerts[0]["detail"]["count"] == 2)

    summary = watchdogs.record_report(db, "PC-1", {"states": [
        {"id": wid, "status": "ok"}]}, now=now + 400)
    check("recovering ends the episode", summary["cleared"] == 1)
    open_alerts = [a for a in alerts.list_open(db) if a["kind"] == alerts.KIND_WATCHDOG]
    check("...leaving the alert open and visible, with the episode closed",
          len(open_alerts) == 1 and open_alerts[0]["episode_ended_at"])

    watchdogs.record_report(db, "PC-1", {"states": [
        {"id": wid, "status": "failed", "detail": "did not come back"}]}, now=now + 500)
    open_alerts = [a for a in alerts.list_open(db) if a["kind"] == alerts.KIND_WATCHDOG]
    check("the next failure raises a FRESH alert beside the old one", len(open_alerts) == 2)

    print("\n-- two watchdogs on one machine --")
    err, second = save(db, name="Time service", service="W32Time")
    watchdogs.record_report(db, "PC-1", {"states": [
        {"id": second, "status": "given_up", "detail": "keeps stopping"}]}, now=now + 600)
    ids = sorted(a["detail"]["watchdog_id"] for a in alerts.list_open(db)
                 if a["kind"] == alerts.KIND_WATCHDOG and not a["episode_ended_at"])
    check("both raise their own alert -- the index is keyed on the watchdog, not just the PC",
          ids == sorted([wid, second]))

    print("\n-- what a machine cannot make the hub do --")
    summary = watchdogs.record_report(db, "PC-2", {"states": [
        {"id": 9999, "status": "given_up"}]}, now=now + 700)
    check("a report for a watchdog the hub does not hold is dropped, not stored",
          summary["stored"] == 0 and summary["escalated"] == 0)
    summary = watchdogs.record_report(db, "PC-2", {"states": [
        {"id": wid, "status": "banana", "restarts": "lots", "last_restart_at": "soon"}]},
        now=now + 700)
    check("an unrecognised status is stored but never escalates",
          summary["stored"] == 1 and summary["escalated"] == 0)
    check("...with its unparseable numbers coerced rather than thrown",
          watchdogs.machine_states(db, "PC-2")[0]["restarts"] == 0)
    check("junk instead of a state list does not raise",
          watchdogs.record_report(db, "PC-2", {"states": "nope"})["stored"] == 0
          and watchdogs.record_report(db, "PC-2", None)["stored"] == 0)

    print("\n-- reading it back --")
    states = watchdogs.states_for(db, wid)
    check("the state table puts trouble first",
          states and states[0]["status"] in watchdogs.ESCALATING_STATUSES)
    check("a machine's own view names the watchdog and its service",
          any(s["service"] == "Spooler" for s in watchdogs.machine_states(db, "PC-1")))
    check("events are newest first", [e["at"] for e in watchdogs.list_events(db)]
          == sorted((e["at"] for e in watchdogs.list_events(db)), reverse=True))
    watchdogs.delete_watchdog(db, second)


def test_lifecycle(db, wid):
    print("\n-- disabling and deleting --")
    watchdogs.record_report(db, "PC-2", {"states": [
        {"id": wid, "status": "given_up"}]}, now=1_700_001_000)
    watchdogs.set_enabled(db, wid, False)
    check("disabling resolves the alerts it raised -- nobody can act on them now",
          not [a for a in alerts.list_open(db) if a["kind"] == alerts.KIND_WATCHDOG
               and a["detail"]["watchdog_id"] == wid])
    watchdogs.set_enabled(db, wid, True)

    watchdogs.record_report(db, "PC-2", {"states": [
        {"id": wid, "status": "given_up"}]}, now=1_700_002_000)
    watchdogs.delete_watchdog(db, wid)
    check("deleting takes its state and its events with it",
          not watchdogs.states_for(db, wid) and not watchdogs.list_events(db, watchdog_id=wid))
    check("...and its alerts", not [a for a in alerts.list_open(db)
                                    if a["kind"] == alerts.KIND_WATCHDOG
                                    and a["detail"]["watchdog_id"] == wid])


def test_machine_moves(db):
    print("\n-- a machine renamed or deleted --")
    err, wid = save(db, name="Spooler", service="Spooler")
    watchdogs.record_report(db, "PC-1", {"states": [{"id": wid, "status": "restarted",
                                                     "restarts": 1, "last_restart_at": 1}]})
    watchdogs.rename_machine(db, "PC-1", "PC-RENAMED")
    check("a rename carries the state and the history onto the surviving name",
          [s["machine"] for s in watchdogs.states_for(db, wid)] == ["PC-RENAMED"]
          and all(e["machine"] == "PC-RENAMED" for e in watchdogs.list_events(db)))
    watchdogs.forget_machine(db, "PC-RENAMED")
    check("deleting a machine drops both", not watchdogs.states_for(db, wid)
          and not watchdogs.list_events(db))
    watchdogs.delete_watchdog(db, wid)


def test_pruning(db):
    print("\n-- retention --")
    err, wid = save(db, name="Spooler", service="Spooler")
    now = 1_700_000_000
    for age_days, status in ((60, "restarted"), (1, "restarted")):
        watchdogs.record_report(db, "PC-1", {"states": [
            {"id": wid, "status": status, "restarts": age_days,
             "last_restart_at": now - age_days * 86400}]}, now=now - age_days * 86400)
    check("both events are there", len(watchdogs.list_events(db, watchdog_id=wid)) == 2)
    check("a retention of 0 prunes nothing -- it means 'keep everything'",
          watchdogs.prune_events(db, 0, now=now) == 0)
    check("30 days drops the old one and keeps the recent one",
          watchdogs.prune_events(db, 30, now=now) == 1
          and len(watchdogs.list_events(db, watchdog_id=wid)) == 1)
    watchdogs.delete_watchdog(db, wid)


def main():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        seed(db)
        test_validation(db)
        wid = test_document(db)
        test_author_scope(db)
        test_reports(db, wid)
        test_lifecycle(db, wid)
        test_machine_moves(db)
        test_pruning(db)
    finally:
        try:
            os.unlink(db)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
