"""The `event.*` rules namespace -- a rule condition over collected Windows event log
records (roadmap #17, the half #16 handed on).

**The silent failure this file exists to catch is a rule that reads zero off a machine nobody
is listening to.** Every variable here is a count, zero is a perfectly ordinary value, and
that makes three different kinds of "we do not know" indistinguishable from "it did not
happen" unless something asserts otherwise:

  * **A machine that has never reported events at all.** No `machine_event_state` row means
    an agent too old to know what a subscription is, or one that has not finished a
    heartbeat. Read as zero, `event.error_count == 0` becomes TRUE for every un-upgraded PC
    in the building -- so a rule written to find the healthy machines returns precisely the
    ones nobody can see. It must read UNKNOWN.
  * **An event id no subscription asks for.** The hub never told any agent to collect it, so
    it has no basis for saying it did not occur. `event.id_4625.count` must be absent, and
    `evaluate` must therefore refuse to settle, on a hub with no 4625 subscription.
  * **A collector that stopped.** A machine whose last event report is hours old is stale,
    not quiet, so the counts age out to UNKNOWN through the same `max_age` chokepoint every
    other variable uses. `events.STALE_AFTER_SECONDS` is the bound, because that is already
    what the console calls a stale event view.

The second thing asserted is that a rule stays SAVEABLE across subscription changes. The
per-id variables are matched structurally, like `disk.<letter>.*`, rather than validated
against the live subscription set -- otherwise deleting a subscription would make every rule
mentioning it unsaveable, and an operator editing an unrelated clause on an old rule would be
stopped by a message about a variable they never touched.

The third is the counting window. It comes from `events.summary_window_seconds`, the setting
the console's own event summary reads, so a threshold fires on the number the operator was
looking at when they chose it. `rules.py` cannot read settings (no model half does), so the
fallback constant in `events.py` has to match that setting's default -- two plausible numbers
that differ by accident would be invisible, which is why it is pinned here.
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import events
import rules
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


NOW = int(time.time())


def db_without_events():
    """The rules schema and `machine_info` -- a hub that never started the events feature.

    `machine_info` is created by hand, exactly as tests/test_rules.py does it and for the same
    reason: importing app here would pull Flask into a test of a module that is deliberately
    free of it. Every other table the resolver reads is behind a `sqlite3.Error` guard and so
    may be absent, which is what makes this a useful shape to test against.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE machine_info (
            machine TEXT PRIMARY KEY, asset_tag TEXT, serial_number TEXT, model TEXT,
            updated_at TEXT, companion_version TEXT, last_temp REAL,
            last_uptime_seconds INTEGER, primary_sensor_name TEXT, service_tag TEXT,
            manufacturer TEXT, boot_epoch INTEGER,
            ad_dn TEXT, ad_ou TEXT, ad_object_guid TEXT, ad_owner TEXT, ad_os TEXT,
            ad_disabled INTEGER, ad_last_logon TEXT, ad_synced_at INTEGER);
        """
    )
    conn.commit()
    conn.close()
    rules.init_rules_db(path)
    return path


def fresh_db():
    path = db_without_events()
    events.init_events_db(path)
    return path


def record(machine, db_path, *, event_id=4625, level="warning", count=1, age=60,
           message=None, log="Security"):
    """Put `count` occurrences of one event on a machine, `age` seconds ago.

    Written through `record_events` rather than INSERTed, so the roll-up this counts over is
    the real one: repeats inside ROLLUP_WINDOW_SECONDS land on one row with a count, which is
    exactly the shape `rule_counters` has to sum rather than count.
    """
    occurred = NOW - age
    entries = [{"log": log, "provider": "Microsoft-Windows-Security-Auditing",
                "event_id": event_id, "level": level,
                "message": message or f"event {event_id} on {machine}",
                "occurred_at": occurred} for _ in range(count)]
    return events.record_events(db_path, machine, {"events": entries})


def resolve(db_path, machine, *, now=None, window=None):
    """One machine's variables, the way app.resolve_rule_vars builds them."""
    return rules.resolve_machine_vars(
        db_path, machine, now=now if now is not None else NOW,
        event_context_=rules.event_context(db_path, window))


def value_of(resolved, name):
    entry = resolved.get(name)
    if entry is None:
        return None
    return entry.value


# --------------------------------------------------------------------- the namespace itself
def test_the_catalog_offers_what_is_subscribed_and_accepts_what_is_not():
    """A picker shows the collected ids; `lookup_variable` accepts any id Windows allows.

    These are deliberately different sets. Offering all 65536 ids would be a scroll bar
    rather than a catalog, but refusing to STORE a rule about an id that no subscription
    covers yet would impose an order on two independent pieces of setup.
    """
    print("\n-- catalog and lookup --")
    db = fresh_db()
    try:
        names = [v.name for v in rules.catalog(db)]
        check("no per-id variable before any subscription",
              not any(name.startswith("event.id_") for name in names))
        check("the totals are always in the catalog",
              {"event.count", "event.critical_count", "event.error_count",
               "event.warning_count"} <= set(names))

        events.create_subscription(db, name="Failed logons", log="Security",
                                   event_ids=[4625, 4740], levels=["warning"])
        names = [v.name for v in rules.catalog(db)]
        check("a subscribed id is offered",
              "event.id_4625.count" in names and "event.id_4740.count" in names)
        check("an id nobody subscribed to is not offered",
              "event.id_1000.count" not in names)

        check("but an unsubscribed id is still a valid variable",
              rules.lookup_variable("event.id_1000.count") is not None)
        check("and it is a number",
              rules.lookup_variable("event.id_1000.count").kind == rules.KIND_NUMBER)
        check("an id outside Windows' range is not a variable",
              rules.lookup_variable("event.id_99999.count") is None)
        check("a malformed event name is not a variable",
              rules.lookup_variable("event.id_4625.total") is None and
              rules.lookup_variable("event.id_.count") is None)
        # A second spelling of one id would validate, save, and then never settle, because
        # the resolver only ever emits the canonical one.
        check("a padded id is not a second name for the same variable",
              rules.lookup_variable("event.id_04625.count") is None)
        check("the catalog carries the event group",
              any(v.group == rules.GROUP_EVENT for v in rules.catalog(db)))
    finally:
        os.unlink(db)


def test_a_rule_survives_the_subscription_being_deleted():
    """The reason the per-id family is structural rather than validated against `extra`.

    A condition naming 4625 must validate identically before the subscription exists, while
    it exists, and after it is deleted. Anything else means an operator who tidies up their
    subscriptions cannot save the rule they came to edit.
    """
    print("\n-- a rule outlives its subscription --")
    db = fresh_db()
    try:
        condition = {"var": "event.id_4625.count", "cmp": ">", "value": 20}
        before = rules.validate_condition(condition, rules.all_extra_variables(db))
        sub = events.create_subscription(db, name="Failed logons", log="Security",
                                         event_ids=[4625], levels=["warning"])
        during = rules.validate_condition(condition, rules.all_extra_variables(db))
        events.delete_subscription(db, sub["id"])
        after = rules.validate_condition(condition, rules.all_extra_variables(db))
        check("valid before the subscription exists", before[0] is None)
        check("valid while it exists", during[0] is None)
        check("still valid once it is deleted", after[0] is None)
    finally:
        os.unlink(db)


# ------------------------------------------------------------------- unknown is not zero
def test_a_machine_that_never_reported_is_unknown_not_zero():
    """The failure this whole module is arranged against.

    An un-upgraded agent produces no `machine_event_state` row. If that read as zero,
    `event.error_count == 0` would be TRUE across every machine the events feature has never
    reached, and the rule would fire on the fleet it knows least about.
    """
    print("\n-- never reported --")
    db = fresh_db()
    try:
        resolved = resolve(db, "NEVER-REPORTED")
        for name in ("event.count", "event.critical_count", "event.error_count",
                     "event.warning_count"):
            check(f"{name} is UNKNOWN", value_of(resolved, name) is rules.UNKNOWN)
        check("and == 0 does not settle",
              rules.evaluate({"var": "event.error_count", "cmp": "==", "value": 0},
                             resolved) is rules.UNKNOWN)
        check("while is_known answers definitely",
              rules.evaluate({"var": "event.error_count", "cmp": "is_known"},
                             resolved) is False)
    finally:
        os.unlink(db)


def test_a_machine_that_reported_nothing_is_zero():
    """The mirror image, and the reason the state table exists at all.

    A healthy machine's report is an EMPTY list. Having reported, its counts are real zeros,
    and a rule must be able to conclude something from them -- otherwise "quiet" and "gone"
    stay the same answer and the feature has told nobody anything.
    """
    print("\n-- reported, nothing to say --")
    db = fresh_db()
    try:
        events.record_events(db, "QUIET-PC", {"events": []})
        resolved = resolve(db, "QUIET-PC")
        check("event.count is 0", value_of(resolved, "event.count") == 0)
        check("event.error_count is 0", value_of(resolved, "event.error_count") == 0)
        check("and == 0 settles TRUE",
              rules.evaluate({"var": "event.error_count", "cmp": "==", "value": 0},
                             resolved) is True)
    finally:
        os.unlink(db)


def test_an_unsubscribed_id_is_absent_rather_than_zero():
    """A hub that never asked cannot report that it did not happen.

    The machine here is reporting perfectly and its totals are real; only the id nobody
    subscribed to is unanswerable. A zero would be the hub asserting the absence of something
    it never looked for, which is the same class of mistake as the one above and easier to
    make, because the surrounding variables all have values.
    """
    print("\n-- an id nobody asked for --")
    db = fresh_db()
    try:
        events.create_subscription(db, name="Failed logons", log="Security",
                                   event_ids=[4625], levels=["warning"])
        record("PC-1", db, event_id=4625, count=3)
        resolved = resolve(db, "PC-1")
        check("the subscribed id has a count",
              value_of(resolved, "event.id_4625.count") == 3)
        check("an unsubscribed id is absent entirely",
              "event.id_1000.count" not in resolved)
        check("so a rule about it does not settle",
              rules.evaluate({"var": "event.id_1000.count", "cmp": ">", "value": 0},
                             resolved) is rules.UNKNOWN)
        check("even though the machine is reporting fine",
              value_of(resolved, "event.count") == 3)
    finally:
        os.unlink(db)


def test_a_subscribed_id_with_no_records_is_zero():
    """Subscribed and silent is the answer a threshold rule needs.

    The distinction that matters is between the two zeros-that-are-not: this one IS a zero,
    because the hub asked, the machine answered, and the event did not happen.
    """
    print("\n-- subscribed, nothing arrived --")
    db = fresh_db()
    try:
        events.create_subscription(db, name="Failed logons", log="Security",
                                   event_ids=[4625, 4740], levels=["warning"])
        record("PC-1", db, event_id=4625, count=2)
        resolved = resolve(db, "PC-1")
        check("the id that arrived counts",
              value_of(resolved, "event.id_4625.count") == 2)
        check("the id that did not is 0, not absent",
              value_of(resolved, "event.id_4740.count") == 0)
        check("so 'no account lockouts' settles TRUE",
              rules.evaluate({"var": "event.id_4740.count", "cmp": "==", "value": 0},
                             resolved) is True)
    finally:
        os.unlink(db)


# ------------------------------------------------------------------------------- counting
def test_occurrences_are_counted_not_rows():
    """Four hundred failed logons are four hundred, not one row.

    This is the whole point of counting `SUM(count)`: the roll-up that makes the table
    affordable would otherwise make every threshold rule read one.
    """
    print("\n-- occurrences, not rows --")
    db = fresh_db()
    try:
        events.create_subscription(db, name="Failed logons", log="Security",
                                   event_ids=[4625], levels=["warning"])
        stored, rolled = record("PC-1", db, event_id=4625, count=50, message="same account")
        counters = events.rule_counters(db, "PC-1", event_ids=[4625], now=NOW)
        check("the repeats rolled onto fewer rows than occurrences", rolled > 0)
        check("the count is the occurrences", counters["by_event_id"][4625] == 50)
        check("and the total agrees", counters["total"] == 50)
        resolved = resolve(db, "PC-1")
        check("the variable carries the occurrences",
              value_of(resolved, "event.id_4625.count") == 50)
    finally:
        os.unlink(db)


def test_levels_are_counted_separately():
    print("\n-- per level --")
    db = fresh_db()
    try:
        record("PC-1", db, event_id=1001, level="critical", count=1)
        record("PC-1", db, event_id=1002, level="error", count=4)
        record("PC-1", db, event_id=1003, level="warning", count=2)
        record("PC-1", db, event_id=1004, level="information", count=9)
        resolved = resolve(db, "PC-1")
        check("critical", value_of(resolved, "event.critical_count") == 1)
        check("error", value_of(resolved, "event.error_count") == 4)
        check("warning", value_of(resolved, "event.warning_count") == 2)
        check("the total carries every level, information included",
              value_of(resolved, "event.count") == 16)
    finally:
        os.unlink(db)


def test_another_machines_events_are_not_counted():
    """Per machine, not per fleet -- the variables are resolved inside a per-machine loop and
    a missing WHERE would make every rule fire on the noisiest PC in the building."""
    print("\n-- one machine at a time --")
    db = fresh_db()
    try:
        events.create_subscription(db, name="Failed logons", log="Security",
                                   event_ids=[4625], levels=["warning"])
        record("PC-1", db, event_id=4625, count=30)
        record("PC-2", db, event_id=4625, count=1)
        check("PC-1 counts its own",
              value_of(resolve(db, "PC-1"), "event.id_4625.count") == 30)
        check("PC-2 counts its own",
              value_of(resolve(db, "PC-2"), "event.id_4625.count") == 1)
    finally:
        os.unlink(db)


def test_the_window_bounds_the_count():
    """Outside the window is not counted, and the window is the operator's setting.

    The second check is the one that matters: a caller passing a different window has to get
    a different answer, or the setting is decoration and every rule silently counts a day.
    """
    print("\n-- the counting window --")
    db = fresh_db()
    try:
        events.create_subscription(db, name="Failed logons", log="Security",
                                   event_ids=[4625], levels=["warning"])
        record("PC-1", db, event_id=4625, count=2, age=120, message="recent")
        record("PC-1", db, event_id=4625, count=5, age=40 * 3600, message="two days ago")
        day = events.rule_counters(db, "PC-1", event_ids=[4625],
                                   window_seconds=86400, now=NOW)
        week = events.rule_counters(db, "PC-1", event_ids=[4625],
                                    window_seconds=7 * 86400, now=NOW)
        check("a day counts only the recent run", day["by_event_id"][4625] == 2)
        check("a week counts both", week["by_event_id"][4625] == 7)
        check("the variable follows the window it was given",
              value_of(resolve(db, "PC-1", window=7 * 86400), "event.id_4625.count") == 7)
        check("and the default window is the shorter answer",
              value_of(resolve(db, "PC-1"), "event.id_4625.count") == 2)
    finally:
        os.unlink(db)


def test_the_default_window_matches_the_setting_the_console_uses():
    """`rules.py` cannot read settings, so the fallback has to be pinned to the real default.

    Both numbers are plausible, neither raises when they differ, and the visible symptom is a
    rule firing on a count nobody can reproduce from the page they set the threshold on.
    """
    print("\n-- one knob --")
    db = fresh_db()
    try:
        settings.init_settings_db(db)
        registry_default = settings.BY_KEY["events.summary_window_seconds"].default
        check("events.DEFAULT_RULE_WINDOW_SECONDS equals the setting's default",
              events.DEFAULT_RULE_WINDOW_SECONDS == registry_default)
        check("and event_context uses it when given nothing",
              rules.event_context(db)["window_seconds"] == registry_default)
        check("while an explicit window wins",
              rules.event_context(db, 3600)["window_seconds"] == 3600)
    finally:
        os.unlink(db)


# ----------------------------------------------------------------------------- staleness
def test_a_stopped_collector_ages_out_to_unknown():
    """A machine whose last event report is hours old is stale, not quiet.

    Same `max_age` chokepoint as every other variable, so this cannot be forgotten for the
    event family alone -- but it is asserted because the value it hides is a zero, and a zero
    looks like an answer.
    """
    print("\n-- stale collector --")
    db = fresh_db()
    try:
        events.create_subscription(db, name="Failed logons", log="Security",
                                   event_ids=[4625], levels=["warning"])
        record("PC-1", db, event_id=4625, count=3)
        fresh = resolve(db, "PC-1")
        check("fresh: the count is there", value_of(fresh, "event.count") == 3)
        check("fresh: and carries its age", fresh["event.count"].age_seconds is not None)

        later = NOW + events.STALE_AFTER_SECONDS + 60
        stale = resolve(db, "PC-1", now=later)
        check("stale: the total is UNKNOWN",
              value_of(stale, "event.count") is rules.UNKNOWN)
        check("stale: the per-id count is UNKNOWN too",
              value_of(stale, "event.id_4625.count") is rules.UNKNOWN)
        check("stale: so a threshold does not fire",
              rules.evaluate({"var": "event.id_4625.count", "cmp": ">", "value": 1},
                             stale) is rules.UNKNOWN)
        check("the staleness bound is the console's own",
              rules.AGE_EVENT == events.STALE_AFTER_SECONDS)
    finally:
        os.unlink(db)


def test_a_database_with_no_event_tables_resolves_rather_than_raising():
    """A hub that has never started the events feature still evaluates its other rules.

    `resolve_machine_vars` builds every family in one pass, so an exception out of this one
    would take the whole tick with it -- every rule in the fleet stops firing because nobody
    ever created a subscription.
    """
    print("\n-- no event tables at all --")
    db = db_without_events()
    try:
        resolved = rules.resolve_machine_vars(db, "PC-1", now=NOW)
        check("it resolved", isinstance(resolved, dict) and resolved)
        check("event.count is UNKNOWN",
              value_of(resolved, "event.count") is rules.UNKNOWN)
        check("no per-id variable is offered",
              not any(name.startswith("event.id_") for name in resolved))
        check("and the catalog is still readable",
              any(v.name == "event.count" for v in rules.catalog(db)))
    finally:
        os.unlink(db)


# ------------------------------------------------------------------- the rule that started it
def test_the_roadmaps_own_example_is_writable():
    """"More than twenty failed logons on one machine in the window", end to end.

    The sentence roadmap #16 handed to #17, asserted as a condition that validates, stores
    and fires -- because every piece above can be right while the thing an operator actually
    wanted to write remains inexpressible.
    """
    print("\n-- the rule this was all for --")
    db = fresh_db()
    try:
        events.create_subscription(db, name="Failed logons", log="Security",
                                   event_ids=[4625], levels=["warning", "information"])
        condition = {"op": "and", "nodes": [
            {"var": "event.id_4625.count", "cmp": ">", "value": 20},
            {"var": "sys.online", "cmp": "==", "value": True},
        ]}
        error, _ = rules.validate_condition(condition, rules.all_extra_variables(db))
        check("the condition validates", error is None)

        record("PC-1", db, event_id=4625, count=5, message="one account")
        check("five failed logons do not fire it",
              rules.evaluate(condition["nodes"][0], resolve(db, "PC-1")) is False)

        record("PC-1", db, event_id=4625, count=30, message="a spray")
        check("thirty-five do",
              rules.evaluate(condition["nodes"][0], resolve(db, "PC-1")) is True)
        check("and the count is readable in a message",
              rules.render_template("{{event.id_4625.count}} failed logons",
                                    resolve(db, "PC-1")) == "35 failed logons")
    finally:
        os.unlink(db)


def main():
    test_the_catalog_offers_what_is_subscribed_and_accepts_what_is_not()
    test_a_rule_survives_the_subscription_being_deleted()
    test_a_machine_that_never_reported_is_unknown_not_zero()
    test_a_machine_that_reported_nothing_is_zero()
    test_an_unsubscribed_id_is_absent_rather_than_zero()
    test_a_subscribed_id_with_no_records_is_zero()
    test_occurrences_are_counted_not_rows()
    test_levels_are_counted_separately()
    test_another_machines_events_are_not_counted()
    test_the_window_bounds_the_count()
    test_the_default_window_matches_the_setting_the_console_uses()
    test_a_stopped_collector_ages_out_to_unknown()
    test_a_database_with_no_event_tables_resolves_rather_than_raising()
    test_the_roadmaps_own_example_is_writable()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
