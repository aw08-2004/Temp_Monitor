"""Tests the Alert Brain (correlate.py + correlate_web.py, roadmap #17).

**The silent failure this file exists to catch is a cause the hub invented.** Every other
failure in this feature is visible: a bundle that did not group looks like today's Alerts
tab, and a provider that refused shows an error. A bundle that names a disk as the cause of a
process failure it did not cause looks exactly like the feature working, an operator acts on
it, and nothing in the console ever says otherwise. So the bulk of what is asserted here is
the cases where correlation must STAY SILENT: no causal pair in the table, the "cause" that
started second, a baseline with too few samples, a flat metric whose MAD is zero.

The second thing it catches is scope leaking through the grouping. `/api/alerts` filters
alerts before returning them; bundling happens on top of that filter, and a bundle assembled
before the filter would put a hostname from outside an operator's scope into a card, a causal
claim and a recommendation. There is a test that a scoped operator's bundles contain only
their own machines.

Run from the repo root so `import app` resolves.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-correlate-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import alerts
import app
import console_session
import correlate
import events
import rules
import scripts

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
console_session.sign_in(client, "tester@example.com")

NOW = 1_700_000_000


def alert_row(alert_id, machine, started, ended=None, kind=alerts.KIND_RULE, rule_id=None):
    """One row in `alerts.list_open()`'s shape. Built by hand rather than raised through the
    store, because these tests are about the grouping function and the store has its own."""
    return {
        "id": alert_id,
        "kind": kind,
        "machine": machine,
        "machines": [],
        "rule_id": rule_id,
        "detail": {"rule_id": rule_id, "rule_name": f"rule-{rule_id}", "text": "", "count": 1},
        "status": alerts.STATUS_OPEN,
        "created_at": started,
        "updated_at": ended or started,
        "episode_ended_at": ended,
    }


def fake_rule(rule_id, variable):
    return {"id": rule_id, "name": f"rule-{rule_id}",
            "condition": {"var": variable, "cmp": ">=", "value": 90}}


# ---------------------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------------------
def test_facets_come_from_the_condition_not_the_name():
    """A rule's facet is derived from the variables it reads, so it survives the rule being
    renamed or translated. A keyword list over rule names would pass on 'Disk nearly full'
    and silently fail on 'Laufwerk fast voll'."""
    disk_rule = {"id": 1, "name": "Laufwerk fast voll",
                 "condition": {"var": "disk.max_used_pct", "cmp": ">=", "value": 95}}
    check("a disk rule is a disk rule whatever it is called",
          correlate.FACET_DISK in correlate.facets_for_rule(disk_rule))

    nested = {"id": 2, "name": "x", "condition": {"op": "and", "nodes": [
        {"var": "metric.memory_load_pct", "cmp": ">=", "value": 95},
        {"op": "not", "nodes": [{"var": "proc.count", "cmp": "<", "value": 10}]},
    ]}}
    found = correlate.facets_for_rule(nested)
    check("a nested condition contributes every variable's facet",
          correlate.FACET_MEMORY in found and correlate.FACET_PROCESS in found)

    check("metric.* splits across facets rather than lumping",
          correlate.facet_for_variable("metric.cpu_temp") == correlate.FACET_THERMAL
          and correlate.facet_for_variable("metric.disk_load_pct") == correlate.FACET_DISK)

    # The load-bearing half: an unknown variable must not raise. This runs over every enabled
    # rule on every read of the Alerts tab, so a custom field must not switch grouping off.
    check("an unknown variable is `other`, not an error",
          correlate.facet_for_variable("field.headcount") == correlate.FACET_OTHER)
    check("a rule with no condition still has a facet",
          correlate.facets_for_rule({"id": 3}) == frozenset({correlate.FACET_OTHER}))


# ---------------------------------------------------------------------------------------
# Bundling
# ---------------------------------------------------------------------------------------
def test_overlapping_episodes_on_one_machine_are_one_bundle():
    rows = [
        alert_row(1, "PC-01", NOW - 3600, rule_id=10),
        alert_row(2, "PC-01", NOW - 3000, rule_id=11),
        alert_row(3, "PC-01", NOW - 2400, rule_id=12),
    ]
    rules_by_id = {10: fake_rule(10, "disk.max_used_pct"),
                   11: fake_rule(11, "proc.count"),
                   12: fake_rule(12, "metric.cpu_load_pct")}
    bundles = correlate.bundle_alerts(rows, rules_by_id=rules_by_id, now=NOW)
    check("three overlapping episodes make ONE bundle", len(bundles) == 1)
    check("the bundle holds all three alerts", bundles[0]["alert_ids"] == [1, 2, 3])
    check("the anchor is the lowest member id", bundles[0]["anchor"] == 1)

    # Transitivity is the point: 1 and 3 need not overlap each other as long as 2 bridges
    # them. Without the connected-component pass this would be two bundles.
    spread = [
        alert_row(1, "PC-01", NOW - 3600, ended=NOW - 3000, rule_id=10),
        alert_row(2, "PC-01", NOW - 3100, ended=NOW - 2000, rule_id=11),
        alert_row(3, "PC-01", NOW - 2100, ended=NOW - 1000, rule_id=12),
    ]
    bundles = correlate.bundle_alerts(spread, rules_by_id=rules_by_id, now=NOW)
    check("grouping is transitive across a bridging episode",
          len(bundles) == 1 and bundles[0]["alert_ids"] == [1, 2, 3])


def test_separate_machines_and_separate_times_stay_separate():
    rows = [alert_row(1, "PC-01", NOW - 600, rule_id=10),
            alert_row(2, "PC-02", NOW - 600, rule_id=10)]
    bundles = correlate.bundle_alerts(rows, rules_by_id={10: fake_rule(10, "disk.count")},
                                      now=NOW)
    check("two machines are never one bundle", len(bundles) == 2)

    apart = [alert_row(1, "PC-01", NOW - 7200, ended=NOW - 7000, rule_id=10),
             alert_row(2, "PC-01", NOW - 600, ended=NOW - 300, rule_id=10)]
    bundles = correlate.bundle_alerts(apart, rules_by_id={10: fake_rule(10, "disk.count")},
                                      now=NOW)
    check("episodes an hour apart stay two bundles", len(bundles) == 2)


def test_an_active_episode_ends_at_now():
    """An episode still matching must overlap anything raised today. Treating `updated_at` as
    its end would stop a week-old ongoing problem grouping with the alert it caused this
    morning -- which is the exact case bundling exists for."""
    rows = [alert_row(1, "PC-01", NOW - 7 * 86400, rule_id=10),
            alert_row(2, "PC-01", NOW - 60, rule_id=11)]
    rows[0]["updated_at"] = NOW - 7 * 86400  # never refreshed since it opened
    bundles = correlate.bundle_alerts(
        rows, rules_by_id={10: fake_rule(10, "disk.max_used_pct"),
                           11: fake_rule(11, "proc.count")}, now=NOW)
    check("an open episode reaches forward to now", len(bundles) == 1)


def test_duplicate_serial_is_never_bundled():
    """Its subject is a SET of machines, so there is no one machine whose story it is part
    of. It comes back as its own single-alert bundle with machine None."""
    rows = [alert_row(1, "PC-01", NOW - 600, rule_id=10),
            {"id": 2, "kind": alerts.KIND_DUPLICATE_SERIAL, "machine": None,
             "machines": ["PC-01", "PC-09"], "rule_id": None, "detail": None,
             "status": alerts.STATUS_OPEN, "created_at": NOW - 600,
             "updated_at": NOW - 600, "episode_ended_at": None}]
    bundles = correlate.bundle_alerts(rows, rules_by_id={10: fake_rule(10, "disk.count")},
                                      now=NOW)
    check("a duplicate_serial alert stays on its own", len(bundles) == 2)
    check("every alert appears in exactly one bundle",
          sorted(i for b in bundles for i in b["alert_ids"]) == [1, 2])


def test_bundling_is_deterministic():
    """Same input, same bundles -- whatever order the rows arrive in. The key is stored as
    the primary key of a recommendation, so a key that depended on iteration order would
    quietly orphan the recommendation it was written for."""
    rows = [alert_row(3, "PC-01", NOW - 2400, rule_id=12),
            alert_row(1, "PC-01", NOW - 3600, rule_id=10),
            alert_row(2, "PC-01", NOW - 3000, rule_id=11)]
    rules_by_id = {10: fake_rule(10, "disk.max_used_pct"),
                   11: fake_rule(11, "proc.count"),
                   12: fake_rule(12, "metric.cpu_load_pct")}
    first = correlate.bundle_alerts(rows, rules_by_id=rules_by_id, now=NOW)
    second = correlate.bundle_alerts(list(reversed(rows)), rules_by_id=rules_by_id, now=NOW)
    check("bundling does not depend on row order",
          json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True))


# ---------------------------------------------------------------------------------------
# The causal claim -- the half that must stay silent
# ---------------------------------------------------------------------------------------
def test_a_known_pair_in_the_right_order_names_a_cause():
    rows = [alert_row(1, "PC-01", NOW - 3600, rule_id=10),   # disk filled first
            alert_row(2, "PC-01", NOW - 3000, rule_id=11)]   # then a process failed
    bundles = correlate.bundle_alerts(
        rows, rules_by_id={10: fake_rule(10, "disk.max_used_pct"),
                           11: fake_rule(11, "proc.count")}, now=NOW)
    cause = bundles[0]["cause"]
    check("the roadmap's own example produces a cause", cause is not None)
    check("the earlier disk alert is named as the cause",
          cause and cause["alert_id"] == 1 and cause["facet"] == correlate.FACET_DISK)
    check("the later process alert is named as the effect",
          cause and cause["effect_alert_id"] == 2)
    check("the reason is one of the table's own keys",
          cause and cause["reason"] in correlate.CAUSE_REASONS)


def test_no_cause_is_claimed_without_one():
    """Three ways the hub must stay silent, all of which would look like the feature working
    if it guessed instead."""
    # 1. The facets are not a pair in the table.
    rows = [alert_row(1, "PC-01", NOW - 3600, rule_id=10),
            alert_row(2, "PC-01", NOW - 3000, rule_id=11)]
    bundles = correlate.bundle_alerts(
        rows, rules_by_id={10: fake_rule(10, "session.count"),
                           11: fake_rule(11, "bios.version")}, now=NOW)
    check("an unknown facet pair claims no cause", bundles[0]["cause"] is None)

    # 2. The known cause started SECOND. A consequence that came first is not a consequence,
    #    and picking it anyway is the invented cause this whole module guards against.
    reversed_rows = [alert_row(1, "PC-01", NOW - 3600, rule_id=11),   # process failed first
                     alert_row(2, "PC-01", NOW - 3000, rule_id=10)]   # disk filled after
    bundles = correlate.bundle_alerts(
        reversed_rows, rules_by_id={10: fake_rule(10, "disk.max_used_pct"),
                                    11: fake_rule(11, "proc.count")}, now=NOW)
    check("a cause that started second is not a cause", bundles[0]["cause"] is None)

    # 3. Simultaneous. Two episodes opened on one evaluation tick are simultaneous as far as
    #    anything here can see, so picking one would be picking on tie-break order.
    same = [alert_row(1, "PC-01", NOW - 3600, rule_id=10),
            alert_row(2, "PC-01", NOW - 3600, rule_id=11)]
    bundles = correlate.bundle_alerts(
        same, rules_by_id={10: fake_rule(10, "disk.max_used_pct"),
                           11: fake_rule(11, "proc.count")}, now=NOW)
    check("simultaneous episodes claim no cause", bundles[0]["cause"] is None)


def test_the_causal_table_is_a_dag_of_defensible_pairs():
    """No pair and its reverse both in the table. A table claiming A causes B and B causes A
    produces a cause for every ordering of the same two alerts, which is the same thing as
    having no rule at all."""
    pairs = {(cause, effect) for cause, effect, _ in correlate.CAUSAL_PAIRS}
    check("no causal pair is also its own reverse",
          not any((effect, cause) in pairs for cause, effect in pairs))
    check("`other` is never a cause or an effect",
          correlate.FACET_OTHER not in {f for pair in pairs for f in pair})
    check("every reason key is unique",
          len(correlate.CAUSE_REASONS) == len(set(correlate.CAUSE_REASONS)))


# ---------------------------------------------------------------------------------------
# Anomaly baselining
# ---------------------------------------------------------------------------------------
def seed_readings(machine, values, *, metric="cpu_load_pct", start=None, step=60):
    start = start if start is not None else NOW - correlate.BASELINE_WINDOW_SECONDS + 600
    with correlate.get_conn(app.DB_PATH) as conn:
        for index, value in enumerate(values):
            ts = start + index * step
            conn.execute(
                f"INSERT OR IGNORE INTO readings (ts_text, ts_epoch, machine, temp, {metric}) "
                "VALUES (?,?,?,?,?)",
                (str(ts), ts, machine, 40.0 + (index % 3), value))


def test_a_baseline_refuses_to_answer_without_enough_history():
    seed_readings("BASE-THIN", [10.0] * 10)
    stats = correlate.baselines(app.DB_PATH, "BASE-THIN", now=NOW)
    check("ten readings produce no baseline", "cpu_load_pct" not in stats)
    check("and therefore no anomalies", correlate.anomalies(app.DB_PATH, "BASE-THIN", now=NOW) == [])


def test_the_baseline_is_robust_to_the_excursion_it_must_detect():
    """Median and MAD, not mean and standard deviation. A mean is dragged by exactly the
    spike this is meant to catch; the robust pair is not."""
    values = [20.0] * 200 + [100.0] * 40
    check("the median ignores a sixth of the window being pinned",
          correlate.baseline_from_values(values)[0] == 20.0)
    check("a flat history has a zero MAD", correlate.baseline_from_values([20.0] * 200)[1] == 0)
    check("too few samples is None, not a number",
          correlate.baseline_from_values([20.0] * 5) is None)


def test_a_flat_metric_never_becomes_an_anomaly():
    """A machine that idles at exactly 2% has a MAD of zero, which makes every deviation
    infinite. The honest reading of a perfectly flat history is that there is no spread to be
    unusual against -- not that 3% is a five-alarm event."""
    seed_readings("BASE-FLAT", [2.0] * 300)
    seed_readings("BASE-FLAT", [3.0], start=NOW - 120)
    check("a zero-MAD metric claims no anomaly",
          not [a for a in correlate.anomalies(app.DB_PATH, "BASE-FLAT", now=NOW)
               if a["metric"] == "cpu_load_pct"])


def test_a_real_excursion_is_reported_with_its_figures():
    # A normally-variable machine: enough spread for a non-zero MAD, then a current reading
    # far outside it.
    seed_readings("BASE-BUSY", [18.0, 20.0, 22.0, 19.0, 21.0] * 60)
    seed_readings("BASE-BUSY", [99.0], start=NOW - 120)
    found = [a for a in correlate.anomalies(app.DB_PATH, "BASE-BUSY", now=NOW)
             if a["metric"] == "cpu_load_pct"]
    check("a genuine excursion is reported", len(found) == 1)
    check("it carries the figures it was judged against",
          found and found[0]["score"] >= correlate.ANOMALY_SCORE
          and found[0]["samples"] >= correlate.MIN_BASELINE_SAMPLES)
    check("the current reading is excluded from its own baseline",
          found and found[0]["median"] < 30)


def test_nothing_in_correlate_raises_an_alert():
    """A baseline that started raising its own alerts would light up the whole fleet on the
    morning it shipped, from a threshold nobody chose. Asserted rather than trusted, because
    it is one INSERT away from being wrong and nothing else would notice."""
    before = alerts.count_open(app.DB_PATH)
    correlate.anomalies(app.DB_PATH, "BASE-BUSY", now=NOW)
    correlate.baselines(app.DB_PATH, "BASE-BUSY", now=NOW)
    check("reading anomalies raises no alerts", alerts.count_open(app.DB_PATH) == before)


# ---------------------------------------------------------------------------------------
# Event evidence (#16)
# ---------------------------------------------------------------------------------------
def record_event(machine, *, event_id, level, first_seen, last_seen, count=1,
                 log="Security", provider="Microsoft-Windows-Security-Auditing"):
    """One machine_events row, written straight to the table.

    Through the store rather than through `events.record_events` on purpose: that path
    normalises timestamps against *now* and rolls up by window, and these tests need rows
    sitting at chosen times relative to a fixed NOW.
    """
    with correlate.get_conn(app.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO machine_events (id, machine, log, provider, event_id, level, "
            "message, rollup_key, count, first_seen, last_seen, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{machine}-{event_id}-{first_seen}", machine, log, provider, event_id, level,
             f"event {event_id}", f"{log}|{event_id}", count, first_seen, last_seen,
             last_seen))


def test_event_rows_join_a_bundle_by_overlap_not_by_recency():
    """A rolled-up run that began before the window and was still going after it straddles
    both ends. `since=start` alone keeps it and a `last_seen <= end` test alone drops it, so
    the overlap has to be checked at both ends -- and a run of four hundred 4625s spanning a
    disk alert is exactly the PRD's example."""
    start, end = NOW - 3600, NOW - 1800
    record_event("EV-01", event_id=4625, level=events.LEVEL_WARNING,
                 first_seen=start - 600, last_seen=end + 600, count=400)
    record_event("EV-01", event_id=7034, level=events.LEVEL_ERROR,
                 first_seen=start + 60, last_seen=start + 120, count=1)
    record_event("EV-01", event_id=1000, level=events.LEVEL_ERROR,
                 first_seen=NOW - 10 * 86400, last_seen=NOW - 10 * 86400, count=99)
    record_event("EV-01", event_id=4624, level=events.LEVEL_INFORMATION,
                 first_seen=start + 60, last_seen=start + 120, count=5000)

    found = correlate.events_in_window(app.DB_PATH, "EV-01", start, end)
    ids = [row["event_id"] for row in found]
    check("a run straddling both ends of the window is kept", 4625 in ids)
    check("an event inside the window is kept", 7034 in ids)
    check("an event ten days earlier is not", 1000 not in ids)
    # Information and verbose are excluded: a bundle that buries its two error rows under
    # five thousand informational ones has reported nothing.
    check("informational noise is excluded", 4624 not in ids)
    check("the loudest row comes first, not the newest", ids and ids[0] == 4625)
    check("the count is the occurrences, not the row count",
          found and found[0]["count"] == 400)


def test_events_reach_the_facts_but_never_the_bundle_membership():
    """An event has no episode -- nothing cleared when it stopped arriving -- so it is
    evidence inside a bundle and never a member of one. A log line that opened an alert
    nobody asked to be alerted about is the fastest way to make the Alerts tab unreadable."""
    rows = [alert_row(41, "EV-02", NOW - 3600, rule_id=10),
            alert_row(42, "EV-02", NOW - 3000, rule_id=11)]
    record_event("EV-02", event_id=7034, level=events.LEVEL_ERROR,
                 first_seen=NOW - 3500, last_seen=NOW - 3400, count=3)
    bundle = correlate.bundle_alerts(
        rows, rules_by_id={10: fake_rule(10, "disk.max_used_pct"),
                           11: fake_rule(11, "proc.count")}, now=NOW)[0]
    check("the bundle's membership is alerts only", bundle["alert_ids"] == [41, 42])

    facts = correlate.bundle_facts(app.DB_PATH, bundle, rows, include_anomalies=False, now=NOW)
    check("but the events ride along in the facts",
          [e for e in facts["events"] if e["event_id"] == 7034])
    check("and can be switched off for a caller that does not want them",
          correlate.bundle_facts(app.DB_PATH, bundle, rows, include_anomalies=False,
                                 include_events=False, now=NOW)["events"] == [])

    check("the event facet is routed for when rules.py grows the family",
          correlate.facet_for_variable("event.security_4625") == correlate.FACET_EVENT)


# ---------------------------------------------------------------------------------------
# The wording layer
# ---------------------------------------------------------------------------------------
def test_a_model_answer_is_parsed_and_bounded():
    error, parsed = correlate.parse_recommendation(
        '```json\n{"explanation": "The drive filled.", "steps": ["Check C:"], '
        '"script": {"label": "Check disk", "shell": "powershell", "body": "Get-PSDrive"}}\n```')
    check("a fenced answer parses", error is None and parsed["explanation"] == "The drive filled.")
    check("its script survives", parsed["script"]["body"] == "Get-PSDrive")

    error, parsed = correlate.parse_recommendation("I am afraid I cannot do that")
    check("prose instead of JSON is refused", error is not None and parsed is None)

    error, parsed = correlate.parse_recommendation('{"steps": ["a"]}')
    check("an answer with no explanation is refused", error is not None)

    # Templating binds a value at RUN time that nobody reviewed at read time, which is the
    # whole safety argument for this feature reversed. The script is dropped; the explanation,
    # which was the useful part, is kept.
    error, parsed = correlate.parse_recommendation(
        '{"explanation": "x", "script": {"shell": "powershell", "body": "rm {{field.path}}"}}')
    check("a templated body is dropped, not escaped",
          error is None and parsed["script"] is None and parsed["explanation"] == "x")

    error, parsed = correlate.parse_recommendation(
        '{"explanation": "x", "script": {"shell": "bash", "body": "ls"}}')
    check("a shell this hub cannot run is dropped", error is None and parsed["script"] is None)

    error, parsed = correlate.parse_recommendation(
        '{"explanation": "x", "steps": ["s"], "script": null}')
    check("a null script is fine", error is None and parsed["script"] is None)


def test_the_provider_being_off_is_an_answer_not_a_crash():
    rows = [alert_row(1, "PC-01", NOW - 3600, rule_id=10),
            alert_row(2, "PC-01", NOW - 3000, rule_id=11)]
    bundle = correlate.bundle_alerts(
        rows, rules_by_id={10: fake_rule(10, "disk.max_used_pct"),
                           11: fake_rule(11, "proc.count")}, now=NOW)[0]
    facts = correlate.bundle_facts(app.DB_PATH, bundle, rows, include_anomalies=False, now=NOW)
    error, saved = correlate.recommend(app.DB_PATH, {"enabled": False}, bundle, facts, now=NOW)
    check("an off provider returns an error, never raises", error and saved is None)

    import ai
    recorded = [r for r in ai.list_requests(app.DB_PATH)
                if r["kind"] == correlate.KIND_RECOMMEND_FIX]
    check("and the refusal still lands in the ONE audit trail",
          recorded and recorded[0]["outcome"] == ai.OUTCOME_DISABLED)


def test_facts_carry_the_figures_the_model_is_not_asked_to_find():
    rows = [alert_row(1, "PC-01", NOW - 3600, rule_id=10),
            alert_row(2, "PC-01", NOW - 3000, rule_id=11)]
    rows[0]["detail"]["text"] = "C: is at 99%"
    bundle = correlate.bundle_alerts(
        rows, rules_by_id={10: fake_rule(10, "disk.max_used_pct"),
                           11: fake_rule(11, "proc.count")}, now=NOW)[0]
    facts = correlate.bundle_facts(app.DB_PATH, bundle, rows, include_anomalies=False, now=NOW)
    check("every member episode is in the facts", len(facts["episodes"]) == 2)
    check("the rule's own text travels with it",
          facts["episodes"][0]["text"] == "C: is at 99%")
    check("the deterministic cause is in the facts, not asked for",
          facts["cause"] and facts["cause"]["reason"] in correlate.CAUSE_REASONS)


def test_a_recommendation_goes_stale_when_the_bundle_changes():
    """A recommendation is prose about a specific set of episodes. Once a third alert joins
    them it is prose about something else, and rendering it anyway is the one way this
    feature could put a wrong sentence in front of an operator with no model involved."""
    rows = [alert_row(1, "PC-07", NOW - 3600, rule_id=10),
            alert_row(2, "PC-07", NOW - 3000, rule_id=11)]
    rules_by_id = {10: fake_rule(10, "disk.max_used_pct"),
                   11: fake_rule(11, "proc.count"),
                   12: fake_rule(12, "metric.cpu_load_pct")}
    bundle = correlate.bundle_alerts(rows, rules_by_id=rules_by_id, now=NOW)[0]
    correlate.save_recommendation(app.DB_PATH, bundle,
                                  {"explanation": "The drive filled.", "steps": [],
                                   "script": None, "provider": "t", "model": "m"}, now=NOW)
    check("it reads back for the bundle it was written about",
          (correlate.stored_recommendation(app.DB_PATH, bundle) or {}).get("explanation")
          == "The drive filled.")

    rows.append(alert_row(3, "PC-07", NOW - 2400, rule_id=12))
    widened = correlate.bundle_alerts(rows, rules_by_id=rules_by_id, now=NOW)[0]
    check("a third alert joining makes it stale",
          correlate.stored_recommendation(app.DB_PATH, widened) is None)


def test_a_suggested_script_lands_switched_off():
    """The PRD's mitigation, kept verbatim: drafted into the library for a human to read,
    never executed. `enabled=False` is not decoration -- scripts.validate_reference refuses a
    disabled script at rule-save time, so until somebody turns it on there is no path from
    this text to a machine."""
    rows = [alert_row(21, "PC-08", NOW - 3600, rule_id=10),
            alert_row(22, "PC-08", NOW - 3000, rule_id=11)]
    bundle = correlate.bundle_alerts(
        rows, rules_by_id={10: fake_rule(10, "disk.max_used_pct"),
                           11: fake_rule(11, "proc.count")}, now=NOW)[0]
    correlate.save_recommendation(
        app.DB_PATH, bundle,
        {"explanation": "Clear the temp folder.", "steps": [],
         "script": {"label": "Report free space", "shell": "powershell",
                    "body": "Get-PSDrive -PSProvider FileSystem"},
         "provider": "t", "model": "m"}, now=NOW)
    error, script = correlate.draft_script(app.DB_PATH, bundle, actor="tester@example.com")
    check("the script is drafted", error is None and script is not None)
    check("and it is switched OFF", script and script["enabled"] is False)
    check("so a rule cannot reference it yet",
          scripts.validate_reference(scripts.specs(app.DB_PATH), script["name"], {})[0])
    check("its name is inside the script grammar",
          script and rules.is_valid_name(script["name"]))
    long_name = correlate.suggested_script_name({
        "machine": "PC with a machine name far beyond the script-name limit",
        "alert_ids": [12345, 67890],
    })
    check("a long machine name does not truncate the bundle anchor",
          len(long_name) == 32 and long_name.endswith("_12345"))

    # Asking twice replaces the draft rather than littering the library.
    error, again = correlate.draft_script(app.DB_PATH, bundle, actor="tester@example.com")
    check("drafting twice reuses the name", error is None and again["name"] == script["name"])

    empty = correlate.bundle_alerts([alert_row(31, "PC-09", NOW - 60, rule_id=10)],
                                    rules_by_id={10: fake_rule(10, "disk.count")}, now=NOW)[0]
    error, drafted = correlate.draft_script(app.DB_PATH, empty)
    check("a bundle with no recommendation drafts nothing", error and drafted is None)


# ---------------------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------------------
def test_the_bundles_endpoint_answers():
    body = client.get("/api/alerts/bundles").get_json()
    check("the endpoint answers with bundles", isinstance(body.get("bundles"), list))
    check("it reports what the caller may do",
          "can_recommend" in body and "can_draft_script" in body)
    for bundle in body["bundles"]:
        check_ids = bundle.get("alert_ids") or []
        if not check_ids:
            check("no bundle is empty", False)
            break
    else:
        check("no bundle is empty", True)


def test_scope_is_applied_before_bundling():
    """A scoped operator's bundles must hold only their own machines.

    Bundling on top of an unfiltered list would put a hostname outside their reach into the
    membership, the causal claim and any recommendation written about it -- and it would look
    exactly like a working feature while doing so. Narrowed by hand, the same way
    test_alerts.py does, because the session user is a break-glass superuser whose
    machine_filter() is None.
    """
    alerts.upsert_rule(app.DB_PATH, "SCOPE-MINE", 901, "Disk nearly full", "C: is at 96%")
    alerts.upsert_rule(app.DB_PATH, "SCOPE-THEIRS", 902, "Disk nearly full", "C: is at 96%")

    body = client.get("/api/alerts/bundles").get_json()
    machines = {b.get("machine") for b in body["bundles"]}
    check("unscoped, both machines are bundled",
          "SCOPE-MINE" in machines and "SCOPE-THEIRS" in machines)

    original = app.access.machine_filter
    try:
        app.access.machine_filter = lambda: (lambda m: m != "SCOPE-THEIRS")
        body = client.get("/api/alerts/bundles").get_json()
        machines = {b.get("machine") for b in body["bundles"]}
        check("an out-of-scope machine has no bundle", "SCOPE-THEIRS" not in machines)
        check("...and the operator's own still does", "SCOPE-MINE" in machines)
        # The alert ids matter as much as the machine names: a bundle that grouped the two
        # would carry the withheld machine's alert id even with its hostname absent.
        ids = {i for b in body["bundles"] for i in b["alert_ids"]}
        withheld = [a["id"] for a in alerts.list_open(app.DB_PATH)
                    if a.get("machine") == "SCOPE-THEIRS"]
        check("no withheld alert id appears in any bundle",
              not (ids & set(withheld)))
    finally:
        app.access.machine_filter = original


def test_every_route_needs_a_session():
    fresh = app.app.test_client()
    for method, path in (("get", "/api/alerts/bundles"),
                         ("get", "/api/alerts/bundles/PC-01/1"),
                         ("get", "/api/machines/PC-01/anomalies"),
                         ("post", "/api/alerts/bundles/PC-01/1/recommend"),
                         ("post", "/api/alerts/bundles/PC-01/1/script")):
        resp = getattr(fresh, method)(path, json={})
        check(f"{method.upper()} {path} refuses an anonymous caller",
              resp.status_code in (302, 401, 403))


def test_a_machine_route_takes_both_gates():
    """`access.require_machine`, not `access.require`. A capability-only gate on a route that
    names a machine is the failure CLAUDE.md calls out by name, and it is invisible until a
    scoped operator reads somebody else's machine."""
    import correlate_web
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "hub", "correlate_web.py"), encoding="utf-8").read()
    machine_routes = source.count('bundles/<machine>/<int:anchor>') + source.count(
        'machines/<machine>/anomalies')
    check("every machine-named route is gated on require_machine",
          source.count("access.require_machine(") == machine_routes)
    check("the module exports its factory",
          hasattr(correlate_web, "create_correlate_blueprint"))


def test_a_suggested_body_is_not_readable_without_issue_commands():
    """scripts.py splits its reads on this line: listing a script is `view`, reading its BODY
    is `issue_commands`, because a body is SYSTEM-privileged code. A drafted body has not
    reached the library yet, but it is a body headed there, so a `manage_rules` operator who
    may ask for a recommendation still must not read the code it suggests."""
    import correlate_web
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "hub", "correlate_web.py"), encoding="utf-8").read()
    # Every route that can return a recommendation runs it through the redactor. Asserted on
    # the source because the alternative -- a second signed-in client with a narrower
    # permission group -- is a whole fixture for one boolean, and a route added later without
    # the call is exactly what this needs to catch.
    returns = source.count("correlate.stored_recommendation(db_path, bundle)")
    check("every stored recommendation goes through the redactor",
          returns and source.count("_readable(") == returns + 2)  # + its def and the POST
    check("the redactor drops the script rather than blanking it",
          'redacted.pop("script", None)' in source)
    check("and it is keyed on issue_commands",
          "access.can(permissions.ISSUE_COMMANDS)" in source)


def test_a_missing_bundle_is_a_404_not_a_guess():
    resp = client.post("/api/alerts/bundles/NO-SUCH-PC/999999/recommend", json={})
    # 403 is also correct here: an unknown machine is out of scope for anyone whose scope is
    # a list, and permissions_web refuses the two identically on purpose so hostnames cannot
    # be enumerated. What must NOT happen is a 200 with a recommendation about nothing.
    check("an unknown bundle never yields a recommendation",
          resp.status_code in (403, 404))

    resp = client.post("/api/alerts/bundles/NO-SUCH-PC/999999/recommend",
                       data="{}", content_type="text/plain")
    check("a non-JSON body is refused (CSRF)", resp.status_code in (403, 404, 415))


# ---------------------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------------------
def test_every_server_vocabulary_has_catalog_text():
    """Mirrors tests/test_i18n.py's server-supplied-text test, for this module's own two
    catalogs. It lives here rather than there so the check sits beside the tuples it covers:
    a facet added to correlate.FACETS without a string would reach the Alerts tab rendering
    its own key as a label, on the card whose whole job is to say what went wrong."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    catalogs = {}
    for lang in ("en", "de", "es"):
        with open(os.path.join(root, "hub", "locales", f"{lang}.json"), encoding="utf-8") as fh:
            catalogs[lang] = json.load(fh)["alerts"]["bundle"]

    for lang, catalog in catalogs.items():
        missing_facets = [f for f in correlate.FACETS if f not in catalog.get("facet", {})]
        missing_causes = [r for r in correlate.CAUSE_REASONS if r not in catalog.get("cause", {})]
        check(f"{lang}: every facet has a label ({missing_facets})", not missing_facets)
        check(f"{lang}: every causal reason has a sentence ({missing_causes})", not missing_causes)

    # The other direction. A string left behind after a facet was removed is dead text a
    # translator keeps maintaining, and nothing else would ever mention it.
    extra = [f for f in catalogs["en"]["facet"] if f not in correlate.FACETS]
    check(f"no orphaned facet strings ({extra})", not extra)

    # The console's lookup maps are the only place these keys are written literally, which is
    # what makes them visible to test_i18n's scan. A facet the JS does not know renders as its
    # own code -- true but ugly -- so they are checked to agree.
    js = open(os.path.join(root, "hub", "static", "js", "alerts.js"), encoding="utf-8").read()
    unmapped = [f for f in correlate.FACETS if f"alerts.bundle.facet.{f}" not in js]
    check(f"the console maps every facet ({unmapped})", not unmapped)
    unmapped = [r for r in correlate.CAUSE_REASONS if f"alerts.bundle.cause.{r}" not in js]
    check(f"the console maps every causal reason ({unmapped})", not unmapped)


def main():
    test_facets_come_from_the_condition_not_the_name()
    test_overlapping_episodes_on_one_machine_are_one_bundle()
    test_separate_machines_and_separate_times_stay_separate()
    test_an_active_episode_ends_at_now()
    test_duplicate_serial_is_never_bundled()
    test_bundling_is_deterministic()
    test_a_known_pair_in_the_right_order_names_a_cause()
    test_no_cause_is_claimed_without_one()
    test_the_causal_table_is_a_dag_of_defensible_pairs()
    test_a_baseline_refuses_to_answer_without_enough_history()
    test_the_baseline_is_robust_to_the_excursion_it_must_detect()
    test_a_flat_metric_never_becomes_an_anomaly()
    test_a_real_excursion_is_reported_with_its_figures()
    test_nothing_in_correlate_raises_an_alert()
    test_event_rows_join_a_bundle_by_overlap_not_by_recency()
    test_events_reach_the_facts_but_never_the_bundle_membership()
    test_a_model_answer_is_parsed_and_bounded()
    test_the_provider_being_off_is_an_answer_not_a_crash()
    test_facts_carry_the_figures_the_model_is_not_asked_to_find()
    test_a_recommendation_goes_stale_when_the_bundle_changes()
    test_a_suggested_script_lands_switched_off()
    test_the_bundles_endpoint_answers()
    test_scope_is_applied_before_bundling()
    test_every_route_needs_a_session()
    test_a_machine_route_takes_both_gates()
    test_a_suggested_body_is_not_readable_without_issue_commands()
    test_a_missing_bundle_is_a_404_not_a_guess()
    test_every_server_vocabulary_has_catalog_text()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
