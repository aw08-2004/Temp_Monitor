"""Alert correlation, anomaly baselining and recommended fixes -- roadmap #17, the PRD's
"Alert Brain".

A machine whose C: drive filled, whose backup service then failed to write, and whose CPU sat
pinned while it retried, raises three alerts. It had **one** problem. An operator who opens
the Alerts tab sees three cards, reads them in whatever order `updated_at` happens to give,
and fixes the symptom that sorted to the top. Grouping those three into one bundle, with the
disk named as the likely cause, is the whole feature.

**The silent failure this module exists to catch: a cause invented to fit a coincidence.**
It is very easy to write correlation that always has an answer -- pick the earliest alert,
call it the cause, and the console looks clever on every bundle. It would also be wrong
roughly as often as it is right, and an operator who rebuilds a print spooler because this
hub told them to, twice, will never read the third recommendation. So:

* Grouping is **structural** -- same machine, overlapping episode windows -- and claims
  nothing about why.
* A **cause** is claimed only when a pair of facets appears in `CAUSAL_PAIRS`, a short hand
  written table where every entry has a mechanism somebody can state out loud, AND the cause
  episode began before the effect episode. A consequence that started first is not a
  consequence. Without a match the bundle says "these happened together", which is true, and
  stops there.
* A **baseline** is a statistic over this machine's own history, never a fleet average and
  never a model, and it refuses to answer at all below `MIN_BASELINE_SAMPLES` -- a baseline
  over six readings is a number with no information in it, and "not enough history" is the
  honest output.
* Only the **wording** goes to a language model, through `ai.complete()` (#24's provider, one
  configuration and one off switch -- see ROADMAP.MD #17, which rejects growing a second one
  here). Every figure in the prompt is computed above; the model is asked to explain, not to
  measure.

WHY A BUNDLE IS A VIEW AND NOT A ROW
------------------------------------
ROADMAP.MD #17 leaves this open. It is a **view**, recomputed on read, and there is no
`alert_bundles` table.

The alternative -- a bundle row, with a `bundle_id` column on `alerts` -- was rejected on a
mechanism, not on taste. Alerts are written from three unrelated code paths (rules evaluation
on its own thread, AD sync, the serial dedup), and an id stamped at raise time is a grouping
computed before the rest of the bundle existed: the second alert of a causal pair would never
join the first one's bundle, because when the first was raised there was nothing to join. The
alternative to that is invalidating stored bundles on every episode refresh, which happens on
every rules tick, fleet-wide. A pure function over the open-alert list the Alerts tab already
loads is cheaper than either and cannot drift from the list it groups, because it *is* that
list, grouped.

The one thing that does get stored is a **recommendation** (`alert_recommendations`), because
that one cost a provider call. It is keyed on the bundle key and carries the exact member ids
it was written about; a bundle whose membership has since changed shows no recommendation
rather than a stale one. See `stored_recommendation`.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
**Anomalies do not raise alerts.** `anomalies()` is read by a bundle and by the machine page;
nothing here writes to `alerts`. A baseline that started raising its own alerts would light up
every machine in the fleet on the morning it shipped, from a threshold nobody chose, in a
product whose alerting is otherwise entirely operator-authored. If a per-machine baseline
should alert, it belongs in `rules.py` as a variable an operator can write a rule against --
which is a different change with a different blast radius.

**Events are EVIDENCE inside a bundle, not members of one.** #16's rows join a bundle's
window (see `events_in_window`) and ride along in `bundle_facts`, so the PRD's own example --
four hundred 4625s on the machine whose disk filled -- is in front of the operator and in the
facts the wording layer is handed. They are not bundled *as* alerts and they never open one:
an event has no episode, nothing cleared when it stopped arriving, and treating a log line as
an alert would put rows on the Alerts tab that no operator asked to be alerted about.

**A rule condition over event counters is still #16's other half, and is not here.** That
needs an `event.*` variable family in `rules.py` -- a change to the rules engine, with its own
resolver, catalog entries and blast radius. When it lands, `facet_for_variable` already routes
the family to a facet, so an event-driven alert will bundle with a metric one with no further
work here. Noted in ROADMAP.MD #17 rather than half-built.

Flask-free, like `alerts.py` and `rules.py` beside it: the caller passes the db path, the AI
configuration dict and the actor in, so every function here is exercisable against a temp
database with no Flask, no environment and no provider. `correlate_web.py` is the HTTP half.
"""
import json
import re
import sqlite3
import time

import ai
import alerts
import events
import rules
import scripts

# ---------------------------------------------------------------------------------------
# Facets -- what an alert is ABOUT
# ---------------------------------------------------------------------------------------

# A rule alert's text is whatever its author typed, so the alert itself says nothing a
# machine can reason over. What it DOES carry is a rule id, and a rule carries a condition
# over named variables -- and the variables are a fixed namespace this hub owns. So "is this
# a disk alert" is answered by "does its condition read a disk variable", which survives the
# rule being renamed, retranslated, or rewritten to a different threshold.
#
# Rejected: matching on the rule's NAME or its alert text with a keyword list. It works on the
# rules the author of the list happened to imagine, fails silently on "Laufwerk fast voll",
# and there is no test that can tell the difference.
FACET_DISK = "disk"
FACET_MEMORY = "memory"
FACET_CPU = "cpu"
FACET_THERMAL = "thermal"
FACET_NETWORK = "network"
FACET_PROCESS = "process"
FACET_LIVENESS = "liveness"
FACET_SESSION = "session"
FACET_DIRECTORY = "directory"
FACET_IDENTITY = "identity"
FACET_EVENT = "event"
FACET_OTHER = "other"

FACETS = (FACET_DISK, FACET_MEMORY, FACET_CPU, FACET_THERMAL, FACET_NETWORK,
          FACET_PROCESS, FACET_LIVENESS, FACET_SESSION, FACET_DIRECTORY,
          FACET_IDENTITY, FACET_EVENT, FACET_OTHER)

# i18n: a facet crosses the wire as a CODE and the console renders it, the same way wake.py's
# diagnosis codes do -- a hub answering a German operator should not have to be told which
# language to build a sentence in. Every facet needs `alerts.bundle.facet.<name>` in all three
# catalogs, and tests/test_correlate.py fails on a missing one: a bundle headed with a facet
# nobody can read is a bundle nobody trusts. Deliberately no key CONSTANT here -- the console
# writes each key out literally, which is the only form test_i18n.py's scan can see.

# Exact variable names first, then dotted prefixes. `metric.*` splits across three facets --
# `metric.disk_load_pct` is disk pressure and `metric.cpu_temp` is heat, and lumping the whole
# group under one facet would bundle a thermal alert with a full drive and then offer to
# explain the connection.
_FACET_BY_VARIABLE = {
    "metric.cpu_temp": FACET_THERMAL,
    "metric.gpu_temp": FACET_THERMAL,
    "metric.fan_rpm": FACET_THERMAL,
    "metric.cpu_load_pct": FACET_CPU,
    "metric.cpu_power_w": FACET_CPU,
    "metric.gpu_load_pct": FACET_CPU,
    "metric.gpu_power_w": FACET_CPU,
    "metric.memory_load_pct": FACET_MEMORY,
    "metric.mem_used_gb": FACET_MEMORY,
    "metric.mem_total_gb": FACET_MEMORY,
    "metric.disk_load_pct": FACET_DISK,
    "metric.net_rx_bps": FACET_NETWORK,
    "metric.net_tx_bps": FACET_NETWORK,
}

_FACET_BY_PREFIX = (
    ("disk.", FACET_DISK),
    # #16's counters, for when rules.py grows the family. Listed now rather than when it
    # lands, because the cost of being early is one unreachable tuple entry and the cost of
    # being late is every event-driven alert silently bundling as `other`.
    ("event.", FACET_EVENT),
    ("proc.", FACET_PROCESS),
    ("process.", FACET_PROCESS),
    ("net.", FACET_NETWORK),
    ("session.", FACET_SESSION),
    ("ad.", FACET_DIRECTORY),
    ("bios.", FACET_IDENTITY),
    ("hw.", FACET_IDENTITY),
    ("sys.", FACET_LIVENESS),
)


def facet_for_variable(name):
    """Which facet a rules variable belongs to. Unknown names -- a custom field, a derived
    variable, an `event.*` name once #16 lands -- are FACET_OTHER rather than an error.

    Returning `other` instead of raising is the load-bearing half. This is called on every
    variable of every enabled rule; a hub that stopped correlating because an operator added
    a custom field would be a hub whose alert grouping silently switched itself off.
    """
    name = str(name or "").strip().lower()
    if not name:
        return FACET_OTHER
    if name in _FACET_BY_VARIABLE:
        return _FACET_BY_VARIABLE[name]
    for prefix, facet in _FACET_BY_PREFIX:
        if name.startswith(prefix):
            return facet
    return FACET_OTHER


def condition_variables(node, _depth=0):
    """Every variable name a validated condition AST reads, deduplicated and sorted.

    A small walker rather than something imported from rules.py: `rules.validate_condition`
    already bounds depth and node count at save time, so anything stored is walkable, and a
    second traversal there would be a public function with one caller. Depth is still bounded
    here, because this also runs over conditions read straight out of the database and a
    corrupted row must not take the Alerts tab down with a recursion error.
    """
    if _depth > rules.MAX_CONDITION_DEPTH + 1 or not isinstance(node, dict):
        return []
    if node.get("op") in ("and", "or", "not"):
        found = []
        for child in node.get("nodes") or []:
            found.extend(condition_variables(child, _depth + 1))
        return sorted(set(found))
    name = node.get("var")
    return [str(name)] if name else []


def facets_for_rule(rule):
    """The facets one rule's condition touches. Empty conditions give `{other}`, never an
    empty set -- an alert with no facet at all would silently drop out of every causal pair
    test rather than simply matching none of them."""
    if not rule:
        return frozenset({FACET_OTHER})
    found = {facet_for_variable(name)
             for name in condition_variables(rule.get("condition"))}
    return frozenset(found or {FACET_OTHER})


# The three non-rule alert kinds have a fixed subject, so their facet is fixed too rather
# than derived. `duplicate_serial` is absent on purpose: it is not bundled at all (see
# bundle_alerts), so a facet for it would be a value nothing reads.
_FACET_BY_KIND = {
    alerts.KIND_AD_UNMATCHED: FACET_DIRECTORY,
    alerts.KIND_HIGH_TEMP: FACET_THERMAL,
}


def facets_for_alert(alert, rules_by_id=None):
    """The facets of one alert row. `rules_by_id` is `{id: rule}` for the rule alerts, passed
    in by the caller so a page rendering forty alerts loads the rule table once."""
    kind = alert.get("kind")
    if kind == alerts.KIND_RULE:
        return facets_for_rule((rules_by_id or {}).get(alert.get("rule_id")))
    return frozenset({_FACET_BY_KIND.get(kind, FACET_OTHER)})


# ---------------------------------------------------------------------------------------
# Causal pairs
# ---------------------------------------------------------------------------------------

# (cause facet, effect facet, reason key). Short on purpose. Every entry is a mechanism that
# can be stated in one sentence and that an operator would recognise; anything that needs a
# paragraph of hedging to justify is a correlation this hub should report as a coincidence
# and let a human interpret.
#
# What is NOT in here matters as much:
#   * cpu -> thermal. A hot CPU under load is real, but the arrow points BOTH ways (heat
#     throttles, throttling raises load) and a table that claims a direction it cannot
#     establish is the invented cause this module exists to avoid.
#   * network -> liveness. A machine that stopped reporting and a network alert on it look
#     causal and are usually the SAME observation twice, which is a merge, not a cause.
#   * anything -> other. `other` is "this hub does not know what this rule is about"; deriving
#     a cause from it would be deriving one from ignorance.
CAUSAL_PAIRS = (
    # The roadmap's own example: a volume with no free space, and the service that then
    # failed to write to it.
    (FACET_DISK, FACET_PROCESS, "disk_starved_process"),
    (FACET_DISK, FACET_LIVENESS, "disk_starved_agent"),
    # Paging. Once a machine is out of physical memory everything on it is slow, and the CPU
    # alert is the paging, not a second problem.
    (FACET_MEMORY, FACET_CPU, "memory_paging"),
    (FACET_MEMORY, FACET_PROCESS, "memory_starved_process"),
    # Thermal throttling presents as a machine that got slow for no visible reason.
    (FACET_THERMAL, FACET_CPU, "thermal_throttling"),
    # An agent that cannot reach the hub stops answering the directory sync too. The arrow is
    # safe here in a way network -> liveness is not: the AD check is a SEPARATE observation
    # made by the hub against a third system, not the same heartbeat read twice.
    (FACET_LIVENESS, FACET_DIRECTORY, "offline_unmatched"),
)

CAUSE_REASONS = tuple(reason for _, _, reason in CAUSAL_PAIRS)

_PAIR_REASON = {(cause, effect): reason for cause, effect, reason in CAUSAL_PAIRS}


# ---------------------------------------------------------------------------------------
# Bundling
# ---------------------------------------------------------------------------------------

# How far apart two episodes may sit and still be called overlapping. Five minutes, because
# the gap between a cause and its consequence is not zero: a disk fills, and the backup
# service notices on its next write. Zero slack would split exactly the pairs this feature
# exists to join. It is small enough that two unrelated problems an hour apart stay two
# bundles, which is the failure in the other direction.
JOIN_SLACK_SECONDS = 300

# A bundle nobody has touched in a fortnight is history, not a situation. The cap bounds what
# the Alerts tab groups; alerts older than this are still listed, just never joined to a
# younger one, so a months-old ad_unmatched row cannot swallow today's disk alert into its
# bundle purely by having been open the whole time.
MAX_BUNDLE_SPAN_SECONDS = 14 * 86400


def _window(alert, now):
    """(start, end) of an alert's episode, in epoch seconds.

    An ACTIVE episode ends at `now`, not at `updated_at`: the condition is still true, and
    treating the last evaluation as the end would stop a week-old ongoing problem overlapping
    anything raised today -- which is the exact case bundling is for.
    """
    start = int(alert.get("created_at") or 0)
    ended = alert.get("episode_ended_at")
    if ended:
        end = int(ended)
    else:
        end = int(now)
    return start, max(start, end)


def _overlaps(a_window, b_window, slack):
    a_start, a_end = a_window
    b_start, b_end = b_window
    return a_start <= b_end + slack and b_start <= a_end + slack


class _Components:
    """Union-find over alert ids. Plain dict parents with path halving -- the sets here are
    tens of elements, so the structure is for determinism and clarity, not for speed."""

    def __init__(self):
        self.parent = {}

    def add(self, key):
        self.parent.setdefault(key, key)

    def find(self, key):
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, left, right):
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        # Smaller id always wins, so the component's representative does not depend on the
        # order the pairs were considered in. That is what makes the bundle key stable
        # between two reads of the same alert list.
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root


def bundle_anchor(alert_ids):
    """A bundle's anchor: the lowest member alert id.

    Ids only ever increase, so a new alert joining an existing bundle does not move the
    anchor. It CAN move when a new alert bridges two previously separate components -- and
    that is correct: a bundle with different membership is a different situation, and any
    recommendation written about the old one is about something else now.
    """
    return min(alert_ids) if alert_ids else 0


def bundle_key(machine, alert_ids):
    """The stable identifier for one bundle, and the recommendations table's primary key.

    Never put in a URL. The web layer addresses a bundle as `<machine>/<anchor>` instead, so
    the machine sits in a route parameter where `access.require_machine` can read it -- a
    bundle addressed by an opaque key would need its scope check written by hand at each
    route, which is the shape CLAUDE.md warns produces a gate somebody forgets.
    """
    return "{}#{}".format(str(machine or ""), bundle_anchor(alert_ids))


def _pick_cause(members):
    """The single causal claim for one bundle, or None.

    Deterministic by construction. Every ordered member pair whose facets appear in
    CAUSAL_PAIRS and whose cause STARTED FIRST is a candidate; the candidates are sorted on
    (cause start, cause id, effect start, effect id) and the first is taken. One claim, not a
    list: a bundle offering three competing causes is a bundle an operator has to adjudicate,
    which is the work this was supposed to save them.

    Strictly-before, not before-or-equal. Two episodes that opened on the same evaluation tick
    are simultaneous as far as anything here can tell, and picking one as the cause of the
    other would be picking on tie-break order.
    """
    candidates = []
    for cause in members:
        for effect in members:
            if cause["alert_id"] == effect["alert_id"]:
                continue
            if cause["started_at"] >= effect["started_at"]:
                continue
            for cause_facet in sorted(cause["facets"]):
                for effect_facet in sorted(effect["facets"]):
                    reason = _PAIR_REASON.get((cause_facet, effect_facet))
                    if reason:
                        candidates.append((
                            cause["started_at"], cause["alert_id"],
                            effect["started_at"], effect["alert_id"],
                            cause_facet, effect_facet, reason,
                        ))
    if not candidates:
        return None
    best = sorted(candidates)[0]
    return {
        "alert_id": best[1],
        "facet": best[4],
        "effect_alert_id": best[3],
        "effect_facet": best[5],
        "reason": best[6],
    }


def bundle_alerts(alert_rows, *, rules_by_id=None, now=None, slack=JOIN_SLACK_SECONDS):
    """Group open alerts into bundles. Pure -- no database, no clock beyond `now`.

    `alert_rows` is `alerts.list_open()`'s output (already scope-filtered by the caller; this
    function must never be the thing that decides who sees what). Returns a list of bundles
    ordered newest activity first, matching the order the Alerts tab already renders in.

    `duplicate_serial` alerts are never bundled. Their subject is a SET of machines rather
    than one machine, so there is no single machine whose story they are part of, and forcing
    them into one would mean picking a member hostname arbitrarily. They come back as
    single-alert bundles with `machine` None, which keeps the Alerts tab's render loop over
    one shape instead of two.
    """
    now = int(time.time() if now is None else now)
    rows = sorted(alert_rows or [], key=lambda a: int(a.get("id") or 0))

    members = {}
    joinable = []
    for row in rows:
        alert_id = int(row.get("id") or 0)
        machine = row.get("machine") if row.get("kind") in alerts.PER_MACHINE_KINDS else None
        start, end = _window(row, now)
        entry = {
            "alert_id": alert_id,
            "machine": machine,
            "kind": row.get("kind"),
            "facets": facets_for_alert(row, rules_by_id),
            "started_at": start,
            "ended_at": end,
            "updated_at": int(row.get("updated_at") or start),
            "active": not row.get("episode_ended_at"),
        }
        members[alert_id] = entry
        if machine and now - start <= MAX_BUNDLE_SPAN_SECONDS:
            joinable.append(entry)

    components = _Components()
    for entry in members.values():
        components.add(entry["alert_id"])
    for index, left in enumerate(joinable):
        for right in joinable[index + 1:]:
            if left["machine"] != right["machine"]:
                continue
            if _overlaps((left["started_at"], left["ended_at"]),
                         (right["started_at"], right["ended_at"]), slack):
                components.union(left["alert_id"], right["alert_id"])

    grouped = {}
    for alert_id in sorted(members):
        grouped.setdefault(components.find(alert_id), []).append(members[alert_id])

    bundles = []
    for group in grouped.values():
        ids = [m["alert_id"] for m in group]
        facets = sorted({f for m in group for f in m["facets"]})
        bundles.append({
            "key": bundle_key(group[0]["machine"], ids),
            "anchor": bundle_anchor(ids),
            "machine": group[0]["machine"],
            "alert_ids": sorted(ids),
            "facets": facets,
            "started_at": min(m["started_at"] for m in group),
            "last_activity_at": max(m["updated_at"] for m in group),
            "active": any(m["active"] for m in group),
            "cause": _pick_cause(group),
        })
    bundles.sort(key=lambda b: (-b["last_activity_at"], -max(b["alert_ids"])))
    return bundles


# ---------------------------------------------------------------------------------------
# Anomaly baselining (FR-02)
# ---------------------------------------------------------------------------------------

# Columns of `readings` a baseline is computed over. A deliberate subset of app.py's
# READING_METRIC_COLUMNS plus `temp`, and NOT an import of it: the throughput counters
# (`net_rx_bps`, `disk_read_bps` and friends) are bursty by nature, so a robust deviation over
# them reports "somebody opened a browser" all day. Held here so adding a metric column to
# ingest does not silently start baselining it.
BASELINE_METRICS = ("temp", "cpu_load_pct", "memory_load_pct", "disk_load_pct",
                    "gpu_temp", "fan_rpm")

# How far back a baseline looks. Two weeks covers a working fortnight including one weekend
# each side, which is what makes "this machine is normally idle on Sunday" part of normal
# rather than an anomaly every Sunday.
BASELINE_WINDOW_SECONDS = 14 * 86400

# The trailing slice judged AGAINST the baseline, and therefore excluded FROM it. Without
# this an hour of 100% CPU is in its own reference set, dragging the median toward itself and
# shrinking the deviation it should have produced.
BASELINE_EXCLUDE_SECONDS = 3600

# Below this many samples no baseline is claimed at all. A machine enrolled yesterday has a
# median; it does not have a normal.
MIN_BASELINE_SAMPLES = 60

# A ceiling on rows pulled per machine. At a 60 s heartbeat a fortnight is around 20 000
# readings, so this is "two weeks, and never more than that however fast a machine reports".
MAX_BASELINE_ROWS = 40000

# Consistency constant that makes the MAD comparable to a standard deviation on normal data.
MAD_SCALE = 1.4826

# How many scaled MADs out counts as an anomaly. 3.5 rather than 3: this number is read by a
# person deciding whether to walk to a desk, and the cost of a false one is that they stop
# reading the panel.
ANOMALY_SCORE = 3.5

# A floor in the metric's own units, applied on TOP of the score. A machine that idles at
# exactly 2% CPU has a MAD near zero, which makes 3% an infinite deviation and a lie. The
# score says "unusual for this machine" and this says "and big enough to matter".
MIN_ABSOLUTE_DELTA = {
    "temp": 8.0,
    "gpu_temp": 8.0,
    "cpu_load_pct": 15.0,
    "memory_load_pct": 10.0,
    "disk_load_pct": 15.0,
    "fan_rpm": 400.0,
}
DEFAULT_ABSOLUTE_DELTA = 10.0


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _median(values):
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def baseline_from_values(values):
    """(median, mad) over a list of floats, or None below MIN_BASELINE_SAMPLES.

    **Median and MAD, not mean and standard deviation.** A mean is dragged by exactly the
    excursions this is meant to detect -- one pinned hour poisons the baseline that should
    have flagged it -- and a standard deviation is dragged harder, because it squares the
    distance. The robust pair is the textbook answer and, more to the point here, it is a
    statistic rather than a model, which is what ROADMAP.MD #17 asks for.
    """
    if len(values) < MIN_BASELINE_SAMPLES:
        return None
    median = _median(values)
    mad = _median([abs(value - median) for value in values])
    return median, mad


def baselines(db_path, machine, *, now=None, window_seconds=BASELINE_WINDOW_SECONDS,
              exclude_seconds=BASELINE_EXCLUDE_SECONDS, metrics=BASELINE_METRICS):
    """`{metric: {"median", "mad", "samples"}}` for one machine over its trailing history.

    One query for every metric rather than one per metric: the row set is identical and the
    columns are a hardcoded tuple, so the alternative is six scans of the same index for no
    gain. Metrics with too few non-NULL samples are simply absent from the result, which is
    how a hub with `metrics.collect_gpu` switched off reports no GPU baseline instead of a
    baseline of nothing.
    """
    now = int(time.time() if now is None else now)
    columns = [m for m in metrics if m in BASELINE_METRICS]
    if not columns:
        return {}
    selected = ", ".join(columns)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT {selected} FROM readings "
            "WHERE machine = ? AND ts_epoch >= ? AND ts_epoch < ? "
            "ORDER BY ts_epoch DESC LIMIT ?",
            (str(machine or ""), now - window_seconds, now - exclude_seconds,
             MAX_BASELINE_ROWS),
        ).fetchall()

    out = {}
    for column in columns:
        values = [float(r[column]) for r in rows if r[column] is not None]
        stats = baseline_from_values(values)
        if stats is None:
            continue
        median, mad = stats
        out[column] = {"median": median, "mad": mad, "samples": len(values)}
    return out


def _latest_reading(db_path, machine, columns, now, exclude_seconds):
    """The most recent reading inside the excluded trailing slice -- i.e. the CURRENT one,
    the value being judged. None when the machine has not reported inside it."""
    selected = ", ".join(columns)
    with get_conn(db_path) as conn:
        return conn.execute(
            f"SELECT ts_epoch, {selected} FROM readings "
            "WHERE machine = ? AND ts_epoch >= ? ORDER BY ts_epoch DESC LIMIT 1",
            (str(machine or ""), now - exclude_seconds),
        ).fetchone()


def anomalies(db_path, machine, *, now=None, score_threshold=ANOMALY_SCORE,
              window_seconds=BASELINE_WINDOW_SECONDS,
              exclude_seconds=BASELINE_EXCLUDE_SECONDS):
    """Metrics whose current value sits far outside this machine's own recent normal.

    Returns a list of `{metric, value, median, mad, score, delta}`, worst first, and an empty
    list whenever the answer is not known -- no baseline, no recent reading, a flat metric
    whose MAD is zero. **Empty means "nothing to say", never "everything is fine"**, and the
    console renders it as absence rather than as an all-clear for exactly that reason.

    Nothing here writes an alert. See the module docstring on why a baseline that raised its
    own alerts would be a fleet-wide surprise nobody configured.
    """
    now = int(time.time() if now is None else now)
    stats = baselines(db_path, machine, now=now, exclude_seconds=exclude_seconds,
                      window_seconds=window_seconds)
    if not stats:
        return []
    latest = _latest_reading(db_path, machine, sorted(stats), now, exclude_seconds)
    if latest is None:
        return []

    found = []
    for metric in sorted(stats):
        value = latest[metric]
        if value is None:
            continue
        value = float(value)
        median = stats[metric]["median"]
        mad = stats[metric]["mad"]
        # A zero MAD means the metric did not move at all across the window. Every deviation
        # from it divides by zero, so no score is claimed -- the honest reading of a perfectly
        # flat history is that this machine has no spread to be unusual against.
        if mad <= 0:
            continue
        delta = abs(value - median)
        if delta < MIN_ABSOLUTE_DELTA.get(metric, DEFAULT_ABSOLUTE_DELTA):
            continue
        score = delta / (MAD_SCALE * mad)
        if score < score_threshold:
            continue
        found.append({"metric": metric, "value": value, "median": median, "mad": mad,
                      "score": score, "delta": value - median,
                      "samples": stats[metric]["samples"]})
    found.sort(key=lambda a: (-a["score"], a["metric"]))
    return found


# ---------------------------------------------------------------------------------------
# Event evidence (#16)
# ---------------------------------------------------------------------------------------

# Which of #16's levels are worth putting in front of somebody reading a bundle. Information
# and verbose are excluded: a subscription an operator wrote to count successful logons is a
# legitimate thing to collect and a terrible thing to volunteer beside a disk alert, and a
# bundle that buries its two error rows under four hundred informational ones has reported
# nothing.
EVIDENCE_LEVELS = (events.LEVEL_CRITICAL, events.LEVEL_ERROR, events.LEVEL_WARNING)

# A hard cap on rows per bundle. These go into a prompt as well as onto a page, and an
# unbounded join would let one noisy machine decide how large every recommendation request is.
MAX_EVIDENCE_ROWS = 10

# How much of an event's message travels. Enough to identify it (an account name out of a
# 4625, a service name out of a 7034) and not enough for one stack-trace-shaped row to
# dominate a prompt.
MAX_EVIDENCE_MESSAGE_CHARS = 300

# The same slack the episode windows use, and for the same reason: the event a problem
# produced does not land in the same second the rule matched.
EVIDENCE_SLACK_SECONDS = JOIN_SLACK_SECONDS


def events_in_window(db_path, machine, start, end, *, levels=EVIDENCE_LEVELS,
                     limit=MAX_EVIDENCE_ROWS, slack=EVIDENCE_SLACK_SECONDS):
    """#16 rows whose own occurrence window overlaps [start, end]. Loudest first.

    **Overlap, not "since".** A rolled-up row carries `first_seen` and `last_seen`, so a run
    of four hundred 4625s that began before the disk alert and was still going after it is
    one row that straddles the window -- and `since=start` alone would keep it, while
    `last_seen <= end` alone would drop it. Both ends are checked here rather than pushed
    into `events.list_events`, which offers `since` and not a range; the row count after a
    `since` filter on one machine is small enough that the second half costs nothing.

    Ordered by occurrences rather than by time, because the cap is what decides what an
    operator sees: four hundred failed logons matter more than the one stray warning that
    happened to be newer.
    """
    rows = events.list_events(db_path, machine=str(machine or ""), levels=list(levels),
                              since=int(start) - int(slack), limit=200)
    end = int(end) + int(slack)
    inside = [row for row in rows if int(row.get("first_seen") or 0) <= end]
    inside.sort(key=lambda r: (-int(r.get("count") or 1), -int(r.get("last_seen") or 0)))
    return [{"log": row.get("log"), "provider": row.get("provider"),
             "event_id": row.get("event_id"), "level": row.get("level"),
             "count": int(row.get("count") or 1),
             "first_seen": row.get("first_seen"), "last_seen": row.get("last_seen"),
             "message": str(row.get("message") or "")[:MAX_EVIDENCE_MESSAGE_CHARS]}
            for row in inside[:limit]]


# ---------------------------------------------------------------------------------------
# The stored recommendation
# ---------------------------------------------------------------------------------------

MAX_EXPLANATION_CHARS = 1200
MAX_STEP_CHARS = 300
MAX_STEPS = 6
MAX_SCRIPT_CHARS = 4000
MAX_RECOMMENDATIONS = 500


def init_correlate_db(db_path):
    """Create the recommendations table if absent. Idempotent -- called next to
    alerts.init_alerts_db() on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_recommendations (
                bundle_key   TEXT PRIMARY KEY,
                machine      TEXT NOT NULL,
                alert_ids    TEXT NOT NULL,   -- JSON array, the exact membership written about
                created_at   INTEGER NOT NULL,
                actor        TEXT NOT NULL DEFAULT '',
                provider     TEXT NOT NULL DEFAULT '',
                model        TEXT NOT NULL DEFAULT '',
                explanation  TEXT NOT NULL DEFAULT '',
                steps_json   TEXT NOT NULL DEFAULT '[]',
                script_json  TEXT,            -- the DRAFTED script, before a human saves it
                script_name  TEXT             -- set once it lands in the script library
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_recommendations_machine "
                     "ON alert_recommendations(machine, created_at DESC)")


def _decode_recommendation(row):
    out = dict(row)
    for column, key, fallback in (("steps_json", "steps", []),
                                  ("script_json", "script", None),
                                  ("alert_ids", "alert_ids", [])):
        raw = out.pop(column, None) if column != "alert_ids" else out.get(column)
        try:
            decoded = json.loads(raw) if raw else fallback
        except (TypeError, ValueError):
            decoded = fallback
        out[key] = decoded
    return out


def stored_recommendation(db_path, bundle):
    """The saved recommendation for this bundle, or None.

    **Returns None when the bundle's membership has changed since it was written**, even
    though the row is still there under the same key. A recommendation is prose about a
    specific set of episodes; once a seventh alert joins them it is prose about something
    else, and showing it anyway is the one way this feature could put a wrong sentence in
    front of an operator without any model being involved.
    """
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM alert_recommendations WHERE bundle_key = ?",
                           (str(bundle.get("key") or ""),)).fetchone()
    if row is None:
        return None
    saved = _decode_recommendation(row)
    if sorted(saved.get("alert_ids") or []) != sorted(bundle.get("alert_ids") or []):
        return None
    return saved


def save_recommendation(db_path, bundle, recommendation, *, actor="", now=None):
    """Store (or replace) one bundle's recommendation and return it."""
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO alert_recommendations "
            "(bundle_key, machine, alert_ids, created_at, actor, provider, model, "
            " explanation, steps_json, script_json, script_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(bundle.get("key") or ""), str(bundle.get("machine") or ""),
             json.dumps(sorted(bundle.get("alert_ids") or [])), now, str(actor or ""),
             str(recommendation.get("provider") or ""), str(recommendation.get("model") or ""),
             str(recommendation.get("explanation") or ""),
             json.dumps(recommendation.get("steps") or []),
             json.dumps(recommendation["script"]) if recommendation.get("script") else None,
             recommendation.get("script_name")),
        )
        # A bounded table, pruned on write rather than on a timer. The row is a cache of one
        # provider call; keeping every bundle any operator ever asked about would grow without
        # a reader, and there is no retention setting to explain to anybody.
        conn.execute(
            "DELETE FROM alert_recommendations WHERE bundle_key NOT IN ("
            "  SELECT bundle_key FROM alert_recommendations "
            "  ORDER BY created_at DESC LIMIT ?)",
            (MAX_RECOMMENDATIONS,),
        )
    return stored_recommendation(db_path, bundle)


def clear_recommendation(db_path, bundle_key):
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM alert_recommendations WHERE bundle_key = ?",
                           (str(bundle_key or ""),))
        return cur.rowcount > 0


def list_recommendations(db_path, limit=100):
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM alert_recommendations ORDER BY created_at DESC LIMIT ?",
            (max(1, min(500, int(limit or 100))),)).fetchall()
    return [_decode_recommendation(r) for r in rows]


# ---------------------------------------------------------------------------------------
# The wording layer
# ---------------------------------------------------------------------------------------

# Deliberately not `ai.KIND_*`: this is a different request shape with a different prompt, and
# the audit trail should be able to answer "how much of last month's provider spend was the
# Alert Brain" without inferring it from the machine column.
KIND_RECOMMEND_FIX = "recommend_fix"


def bundle_facts(db_path, bundle, alert_rows, *, rules_by_id=None, include_anomalies=True,
                 include_events=True, now=None):
    """Everything known about one bundle, computed here and only here.

    This is the seam the whole design rests on: the figures in a recommendation come out of
    this function, deterministically, and the model is handed them as facts to word rather
    than asked to find them. A summary that invents a number is worse than no summary, because
    it gets quoted into a change ticket -- `ai.daily_summary`'s docstring draws the same line
    for the same reason.
    """
    by_id = {int(a.get("id") or 0): a for a in alert_rows or []}
    episodes = []
    for alert_id in bundle.get("alert_ids") or []:
        row = by_id.get(alert_id)
        if row is None:
            continue
        detail = row.get("detail") or {}
        episodes.append({
            "alert_id": alert_id,
            "kind": row.get("kind"),
            "rule_name": detail.get("rule_name") or "",
            "text": str(detail.get("text") or "")[:MAX_STEP_CHARS],
            "refreshes": int(detail.get("count") or 1),
            "facets": sorted(facets_for_alert(row, rules_by_id)),
            "started_at": int(row.get("created_at") or 0),
            "ended_at": row.get("episode_ended_at"),
        })
    facts = {
        "machine": bundle.get("machine") or "",
        "facets": list(bundle.get("facets") or []),
        "cause": bundle.get("cause"),
        "started_at": bundle.get("started_at"),
        "active": bool(bundle.get("active")),
        "episodes": episodes,
        "anomalies": [],
        "events": [],
    }
    if bundle.get("machine"):
        if include_anomalies:
            facts["anomalies"] = anomalies(db_path, bundle["machine"], now=now)
        if include_events:
            # The bundle's own span, not a fixed lookback: the question an operator has is
            # "what else was this machine saying while THIS was happening", and a 24-hour
            # window would answer a different one.
            facts["events"] = events_in_window(
                db_path, bundle["machine"], bundle.get("started_at") or 0,
                bundle.get("last_activity_at") or int(time.time() if now is None else now))
    return facts


_SYSTEM_PROMPT = """You are helping a Windows helpdesk technician read one group of related \
alerts from their fleet management console.

You are given FACTS that the console already computed. They are correct. Your job is wording, \
not measurement.

Rules you must follow:
- Never state a number, a drive letter, a service name or a hostname that is not in the facts.
- `events` are Windows event log records collected from this machine during the same period. `count` is how many times that record repeated, not how serious it is.
- If the facts name a likely cause, explain that cause. If they do not, say what the alerts \
have in common and do not guess at a cause.
- The steps are for a technician who can reach the machine remotely and run PowerShell as \
SYSTEM. Be concrete and ordinary; prefer checking before changing.
- The optional script must be safe to READ and to review. It may inspect and report. It must \
not delete user data, disable security software, or reboot the machine.
- Write British English, plain and short. No marketing, no apologies, no headings.

Answer with one JSON object and nothing else:
{"explanation": "two or three sentences", "steps": ["...", "..."], \
"script": {"label": "short title", "shell": "powershell", "body": "..."}}

`script` is optional -- use null when no script would help. Never include `{{` in the body."""


def _facts_prompt(facts):
    """The user half of the prompt. JSON, not prose: the facts are already structured, and
    rendering them into English here would mean writing the sentence twice -- once for the
    model and once for the console -- with nothing keeping the two in step."""
    return json.dumps(facts, sort_keys=True, default=str)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

# `ai._parse_envelope` is not reused. It is shaped around the rule drafter's envelope (rules,
# refusal, repair attempts) and widening it to serve two callers would make one function that
# is wrong for both. The fence-stripping is the only part worth copying, and it is four lines.
UNREADABLE = "the AI provider did not answer with a recommendation this hub could read"


def parse_recommendation(text):
    """Model text -> (error, recommendation). Nothing here trusts the payload's shape.

    The script is NOT validated against `scripts.validate_script` here, because that needs the
    hub's variable namespace and this module has no database handle at this point. It is
    validated at DRAFT time instead -- see `draft_script`, which is the only path by which a
    model-written body can reach the script library, and therefore the only place the check
    has to be.
    """
    body = str(text or "").strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        payload = json.loads(body)
    except ValueError:
        return UNREADABLE, None
    if not isinstance(payload, dict):
        return UNREADABLE, None

    explanation = str(payload.get("explanation") or "").strip()[:MAX_EXPLANATION_CHARS]
    if not explanation:
        return UNREADABLE, None

    steps = []
    for step in payload.get("steps") or []:
        cleaned = str(step or "").strip()[:MAX_STEP_CHARS]
        if cleaned:
            steps.append(cleaned)
        if len(steps) >= MAX_STEPS:
            break

    script = None
    raw_script = payload.get("script")
    if isinstance(raw_script, dict):
        script_body = str(raw_script.get("body") or "").strip()
        shell = str(raw_script.get("shell") or scripts.DEFAULT_SHELL).strip().lower()
        # A model-authored body containing `{{...}}` is refused outright rather than escaped.
        # Templating binds a value at RUN time that nobody reviewed at read time, and the
        # whole safety argument for this feature is that a human reads the script before it
        # runs. Dropping the script keeps the explanation, which is the part that was useful.
        if script_body and "{{" not in script_body and shell in scripts.SHELLS:
            script = {
                "label": str(raw_script.get("label") or "").strip()[:scripts.MAX_LABEL_CHARS],
                "shell": shell,
                "body": script_body[:MAX_SCRIPT_CHARS],
            }
    return None, {"explanation": explanation, "steps": steps, "script": script}


def recommend(db_path, config, bundle, facts, *, api_key="", actor="", now=None):
    """Ask the configured provider to word a fix for one bundle. Returns (error, saved).

    Off by default and never implicit: `ai.is_enabled` gates it, and this is only ever
    reached from an operator pressing a button on one bundle. Nothing in the hub calls it on a
    timer -- a feature that quietly bills a provider per alert episode is one an operator
    finds out about from an invoice.

    Every outcome lands in `ai_requests` through `ai.record_request`, the same audit trail the
    rule drafter writes to, because ROADMAP.MD #17 asks for ONE audit trail across every place
    a language model is asked anything, not a second one grown here.
    """
    stamp = {"provider": str((config or {}).get("provider") or ""),
             "model": str((config or {}).get("model") or "")}
    prompt = _facts_prompt(facts)

    def _record(outcome, error=""):
        ai.record_request(db_path, actor=actor, kind=KIND_RECOMMEND_FIX,
                          provider=stamp["provider"], model=stamp["model"],
                          machine=str(bundle.get("machine") or ""),
                          prompt_chars=len(prompt), outcome=outcome, error=error,
                          now=now)

    if not ai.is_enabled(config):
        message = "AI assistance is switched off"
        _record(ai.OUTCOME_DISABLED, message)
        return message, None

    error, text = ai.complete(config, [{"role": "system", "content": _SYSTEM_PROMPT},
                                       {"role": "user", "content": prompt}], api_key=api_key)
    if error:
        _record(ai.OUTCOME_PROVIDER_ERROR, error)
        return error, None

    error, recommendation = parse_recommendation(text)
    if error:
        _record(ai.OUTCOME_REFUSED, error)
        return error, None

    recommendation.update(stamp)
    _record(ai.OUTCOME_OK)
    return None, save_recommendation(db_path, bundle, recommendation, actor=actor, now=now)


def suggested_script_name(bundle):
    """A script library name for one bundle's suggestion, inside `scripts._NAME_RE`'s grammar.

    Derived from the bundle key rather than random, so asking twice about the same bundle
    replaces the draft instead of littering the library with `fix_1`, `fix_2`, `fix_3`.
    """
    machine = re.sub(r"[^a-z0-9]+", "_", str(bundle.get("machine") or "").lower()).strip("_")
    prefix = "suggested_"
    suffix = f"_{min(bundle.get('alert_ids') or [0])}"
    machine_chars = 32 - len(prefix) - len(suffix)
    return f"{prefix}{machine[:machine_chars]}{suffix}"


def draft_script(db_path, bundle, *, known_variable=None, actor="", now=None):
    """Put a bundle's suggested script into the script library, SWITCHED OFF. Returns
    (error, script).

    **Drafted, never executed.** This is the PRD's own mitigation and ROADMAP.MD #17 keeps it
    verbatim: a suggested script becomes a row in the library a human opens, reads and enables,
    on the same page as every hand-written one. It is saved with `enabled=False`, which is not
    decoration -- `scripts.validate_reference` refuses a disabled script at rule-save time and
    `rules.py` refuses it again at fire time, so until somebody turns it on there is no path
    from this text to a machine.

    It also goes through `scripts.validate_script` unchanged, the same call the hand-written
    path makes. A model-authored body earns no shortcut through the check that decides what
    may run as SYSTEM.
    """
    saved = stored_recommendation(db_path, bundle)
    if saved is None:
        return "there is no current recommendation for this bundle", None
    script = saved.get("script")
    if not script:
        return "that recommendation did not include a script", None

    name = suggested_script_name(bundle)
    label = script.get("label") or f"Suggested fix for {bundle.get('machine') or 'a machine'}"
    description = str(saved.get("explanation") or "")[:scripts.MAX_DESCRIPTION_CHARS]
    error, stored = scripts.save_script(
        db_path, name, label, description, script.get("shell"), script.get("body"),
        [], scripts.DEFAULT_TIMEOUT_SECONDS,
        # Switched off. A human enables it after reading it, which is the entire mitigation.
        enabled=False,
        known_variable=known_variable, actor=actor, now=now)
    if error:
        return error, None

    with get_conn(db_path) as conn:
        conn.execute("UPDATE alert_recommendations SET script_name=? WHERE bundle_key=?",
                     (stored["name"], str(bundle.get("key") or "")))
    return None, stored
