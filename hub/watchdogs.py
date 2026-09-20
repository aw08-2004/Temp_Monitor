"""Self-healing watchdogs -- roadmap #20. A service stops, the agent puts it back inside two
minutes, and only the giving-up reaches a human.

**The agent is given a desired state, never a condition.** That is the whole design decision
in this file, and it is the one the roadmap left open, so it is written down here rather than
in a commit message.

#20's entry says a watchdog is "a small rule the agent holds and runs itself", and the
sequencing note calls it "the one item that needs the rules engine to grow a second
evaluation site (on the agent)". It does not. What travels to the machine is
`{service, grace, flap limits}` -- a declaration of what must be true -- and nothing that has
to be *evaluated* in the rules engine's sense. Three reasons, in order of how much they cost
when ignored:

  1. **A second evaluator of `rules.evaluate` would be a second implementation of the
     condition language, in another language.** UNKNOWN as a third truth value, per-source
     staleness, `matches` wildcards, derived variables, probes -- the agent would need all of
     it to read one expression the way the hub reads it, and the two would drift. They would
     drift SILENTLY: a watchdog whose condition reads UNKNOWN on the machine does nothing, and
     doing nothing is also what a healthy watchdog does. There is no observable difference
     between "the service is fine" and "the agent could not understand the rule", which makes
     it the worst possible thing to get wrong in a feature whose entire value is that nobody
     is watching it.
  2. **This codebase already decided this question once.** `policy.py`: "The device is never
     given a rule engine: it receives a flat list of packages to suspend and applies it.
     Everything about precedence, overlap and mode happens here, where it can be read, tested
     and previewed before anything reaches a phone." A watchdog is the same shape of problem
     and gets the same answer.
  3. **The alternative the roadmap floats -- an ordinary rule with a "evaluate locally" tick
     box -- changes what a rule's condition MEANS without changing a character of its text.**
     `sys.uptime_days > 7` hub-side reads a reported value with a staleness bound; the same
     words agent-side would read a live one with none. One rule, two readings, and the tick
     box is the only clue. Watchdogs are therefore authored separately, and that answers the
     second of #20's two open parameters.

What the rules engine DOES lend, because these parts have nothing to do with evaluation and
duplicating them would be its own kind of drift:

  * **Targets.** `rules.validate_target` / `resolve_targets` / `scoped_targets` verbatim, so
    `field.site == "asuncion"` aims a watchdog exactly as it aims a rule, and an author's
    scope is pinned at save time and re-intersected at delivery time for the same reason.
  * **The perimeter.** Writing a watchdog needs `manage_rules` AND `issue_commands` -- see
    `watchdogs_web.py`. A watchdog restarts a service unattended, which is `restart_process`
    by another name, and `rules.validate_actions` already refuses to let somebody who may
    raise an alert also issue a command. This is that same rule, applied at the only place a
    watchdog can be written.
  * **The escalation.** Giving up raises an ordinary per-machine alert episode
    (`alerts.KIND_WATCHDOG`), so it lands where an operator already looks.

**The flap limit is the first of #20's open parameters**, and it is answered with a count in a
window (`max_restarts` in `window_seconds`) rather than an exponential backoff. A backoff has
no terminal state, so a service that dies every ninety seconds would be restarted forever and
the escalation -- the part a human reads -- would never arrive. A count reaches a decision.

**There is no dead-man expiry on a watchdog document, and that is deliberately the opposite of
`policy.py`.** An app policy carries `max_age_seconds` because a phone whose hub is gone must
not stay a brick. A watchdog is the reverse: an unreachable hub is precisely when local
evaluation earns its keep, and a watchdog that switched itself off after a week offline would
switch off on the machines that needed it most. The agent holds the last document it was
given, indefinitely, and the only way to stop one is to reach the machine.

Flask-free and app-free, like rules.py, fleet.py and policy.py beside it.
"""
import hashlib
import json
import sqlite3
import time

import alerts
import rules

# ---------------------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------------------

#: What the agent reports back per watchdog. A CLOSED set: an unrecognised status is stored
#: as-is for the console but never drives an alert, on the same reasoning
#: `capabilities.PLATFORMS` is closed -- this is an escalation input.
STATUS_OK = "ok"                 # the service is there and running; nothing was done
STATUS_RESTARTED = "restarted"   # it was stopped, we started it, it is running now
STATUS_FAILED = "failed"         # a restart was attempted and it did not come back
STATUS_GIVEN_UP = "given_up"     # flap limit reached; not trying again until the window clears
STATUS_MISSING = "missing"       # no such service on this machine
STATUSES = (STATUS_OK, STATUS_RESTARTED, STATUS_FAILED, STATUS_GIVEN_UP, STATUS_MISSING)

#: The two that mean a human has to do something. `failed` is in here and `missing` is not,
#: and the split is worth stating: a restart that did not work is a machine in trouble, while
#: a watchdog aimed at a service that machine has never had is an authoring mistake that is
#: obvious the moment the page is opened. A fleet-wide watchdog with one mistyped service name
#: would otherwise raise an alert per PC for one typo, which is how an alert list stops being
#: read.
ESCALATING_STATUSES = frozenset({STATUS_FAILED, STATUS_GIVEN_UP})

#: Text key prefix for the status words. en.json is the schema -- see hub/i18n.py. NOT
#: `watchdogs.status`, which is the state table's column heading: one key cannot be both a
#: sentence and a namespace, and the catalog would silently lose whichever was written second.
STATUS_TEXT_KEY = "watchdogs.status_name"

MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 500
#: Windows service names are capped at 256 by the SCM itself.
MAX_SERVICE_CHARS = 256
#: A watchdog per service per fleet is already a lot of standing instructions; this is a
#: runaway guard, not a design limit.
MAX_WATCHDOGS = 200
MAX_DETAIL_CHARS = 500

#: How long the service must be seen NOT running before anything is done about it. Zero is
#: allowed and means "act on the first look", which is right for a service that is never
#: legitimately down and wrong for one an installer stops for forty seconds.
MIN_GRACE_SECONDS = 0
MAX_GRACE_SECONDS = 3600
DEFAULT_GRACE_SECONDS = 60

#: Flap protection. One restart is not flapping and a limit of one would escalate on the first
#: ordinary crash, so the floor is 2.
MIN_RESTARTS = 2
MAX_RESTARTS = 20
DEFAULT_MAX_RESTARTS = 3

#: The window the restarts are counted in. The floor is five minutes because anything shorter
#: cannot distinguish a flap from two unrelated crashes.
MIN_WINDOW_SECONDS = 300
MAX_WINDOW_SECONDS = 7 * 86400
DEFAULT_WINDOW_SECONDS = 3600

#: Services this hub will never hand a watchdog, whatever an operator types.
#:
#: **The agent keeps its own copy and that copy is the authority**, exactly as
#: `ProcessGuard` does for `end_process`: this one exists so a refusal is immediate in the
#: console rather than discovered by a machine restarting itself. Extend both or neither.
#:
#: Each entry earns its place:
#:   rpcss, dcomlaunch  -- stopping either takes the machine down with a bugcheck or a
#:                         one-minute forced restart. Windows will not let you, but a
#:                         watchdog that tries every thirty seconds is still a machine
#:                         spending its life on a refused SCM call.
#:   plugplay, power, brokerinfrastructure, systemeventsbroker, lsm
#:                      -- the same class: critical, non-stoppable, and a dependency of
#:                         most of what else is running.
#:   winmgmt            -- stoppable, and the wrong answer every time. Restarting WMI takes
#:                         every dependent down with it, and this agent reads its own
#:                         sensors through it.
#:   gpsvc              -- Group Policy. Restarting it mid-apply is how a machine ends up
#:                         with half a policy and no record of which half.
#:   tempmonitoragent   -- ourselves. A watchdog on the agent cannot work: if the agent is
#:                         stopped, nothing is evaluating the watchdog. Refusing says that,
#:                         where accepting would look like protection and be nothing.
PROTECTED_SERVICES = frozenset({
    "rpcss", "dcomlaunch", "plugplay", "power", "brokerinfrastructure",
    "systemeventsbroker", "lsm", "winmgmt", "gpsvc", "tempmonitoragent",
})


def normalize_service(name):
    """Lowercased and trimmed -- the same normalization the agent applies, so a name cannot
    slip past one end by spelling. Windows service names are case-insensitive."""
    return str(name or "").strip().lower()


def is_protected(name):
    return normalize_service(name) in PROTECTED_SERVICES


# ---------------------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------------------


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_watchdogs_db(db_path):
    """Create the watchdog tables if absent. Idempotent -- called next to
    rules.init_rules_db() on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # The watchdogs themselves. `target_json` and `scope_json` are the rules engine's,
        # byte for byte, so a target saved here means what the same target means on a rule.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchdogs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL,
                description    TEXT NOT NULL DEFAULT '',
                enabled        INTEGER NOT NULL DEFAULT 1,
                target_json    TEXT NOT NULL,
                scope_json     TEXT,
                service        TEXT NOT NULL,
                grace_seconds  INTEGER NOT NULL DEFAULT 60,
                max_restarts   INTEGER NOT NULL DEFAULT 3,
                window_seconds INTEGER NOT NULL DEFAULT 3600,
                created_at     INTEGER NOT NULL,
                created_by     TEXT NOT NULL DEFAULT '',
                updated_at     INTEGER NOT NULL,
                updated_by     TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # What each machine last said about each watchdog it holds. One row per (machine,
        # watchdog), overwritten -- the history is the events table below.
        #
        # **This table is the answer to "is this actually working", which is the question a
        # self-healing feature has to be able to answer.** A watchdog with no rows here is one
        # no machine has ever reported on, and that is indistinguishable from a healthy fleet
        # unless the console can show the difference. It can, because of this.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_watchdog_state (
                machine         TEXT NOT NULL,
                watchdog_id     INTEGER NOT NULL,
                status          TEXT NOT NULL DEFAULT '',
                detail          TEXT NOT NULL DEFAULT '',
                restarts        INTEGER NOT NULL DEFAULT 0,
                last_restart_at INTEGER,
                reported_at     INTEGER NOT NULL,
                PRIMARY KEY (machine, watchdog_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_watchdog_state_wd "
                     "ON machine_watchdog_state(watchdog_id, status)")
        # One row per thing that happened, which is a table rather than a log line for the
        # same reason `rule_fires` is: "the spooler was restarted on that PC four times last
        # Tuesday" is a question asked three months later, in a change ticket.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchdog_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                watchdog_id INTEGER NOT NULL,
                machine     TEXT NOT NULL,
                at          INTEGER NOT NULL,
                status      TEXT NOT NULL,
                detail      TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watchdog_events_wd "
                     "ON watchdog_events(watchdog_id, at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watchdog_events_machine "
                     "ON watchdog_events(machine, at DESC)")


# ---------------------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------------------


def _clean(value, limit):
    return str(value or "").strip()[:limit]


def _bounded_int(raw, default, low, high, label):
    """An int inside its bounds, or an error naming the bound it broke.

    Clamping silently was the obvious alternative and is refused on purpose: somebody who
    types a 10-second flap window has a reason for the number, and a watchdog that quietly
    became a 300-second one is a watchdog behaving differently from what its page says.
    """
    if raw is None or raw == "":
        return None, default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return f"{label} must be a whole number of seconds", None
    if value < low or value > high:
        return f"{label} must be between {low} and {high}", None
    return None, value


def validate(payload, extra=None):
    """Check a whole watchdog payload. Returns (error, normalised).

    Error text goes back to the author verbatim (see watchdogs_web.py) -- it is written out of
    their own input and an editor whose errors all read "invalid" is one nobody can use.
    """
    if not isinstance(payload, dict):
        return "the watchdog must be an object", None

    name = _clean(payload.get("name"), MAX_NAME_CHARS)
    if not name:
        return "a watchdog needs a name", None

    service = _clean(payload.get("service"), MAX_SERVICE_CHARS)
    if not service:
        return "a watchdog needs the name of a service to watch", None
    if is_protected(service):
        return (f"'{service}' is one of the services this hub will not watch: restarting it "
                f"does not recover a machine, it takes it down. See PROTECTED_SERVICES."), None

    err, target = rules.validate_target(payload.get("target"), extra)
    if err:
        return err, None

    err, grace = _bounded_int(payload.get("grace_seconds"), DEFAULT_GRACE_SECONDS,
                              MIN_GRACE_SECONDS, MAX_GRACE_SECONDS, "the grace period")
    if err:
        return err, None
    err, max_restarts = _bounded_int(payload.get("max_restarts"), DEFAULT_MAX_RESTARTS,
                                     MIN_RESTARTS, MAX_RESTARTS, "the restart limit")
    if err:
        return err, None
    err, window = _bounded_int(payload.get("window_seconds"), DEFAULT_WINDOW_SECONDS,
                               MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS, "the flap window")
    if err:
        return err, None

    return None, {
        "name": name,
        "description": _clean(payload.get("description"), MAX_DESCRIPTION_CHARS),
        "enabled": bool(payload.get("enabled", True)),
        "target": target,
        "service": service,
        "grace_seconds": grace,
        "max_restarts": max_restarts,
        "window_seconds": window,
    }


def _decode(row):
    if row is None:
        return None
    out = dict(row)
    out["enabled"] = bool(out["enabled"])
    try:
        out["target"] = json.loads(out.pop("target_json") or "{}")
    except (TypeError, ValueError):
        out["target"] = {"include": [], "exclude": []}
    raw_scope = out.pop("scope_json", None)
    # NULL means the author was unrestricted, which is also what every row written before a
    # scoped operator ever touched this reads -- see rules.scoped_targets.
    try:
        out["author_scope"] = json.loads(raw_scope) if raw_scope else None
    except (TypeError, ValueError):
        out["author_scope"] = None
    return out


def list_watchdogs(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM watchdogs ORDER BY name, id").fetchall()
    return [_decode(r) for r in rows]


def get_watchdog(db_path, watchdog_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM watchdogs WHERE id = ?", (watchdog_id,)).fetchone()
    return _decode(row)


def save_watchdog(db_path, payload, *, watchdog_id=None, actor="", now=None,
                  author_scope=None, extra=None):
    """Create or replace one watchdog. Returns (error, id).

    `author_scope` is the caller's resolved machine set, or None for an unrestricted
    operator, and it is PERSISTED rather than merely checked -- see rules.scoped_targets for
    why the save-time check alone leaks as soon as a machine is enrolled.
    """
    err, clean = validate(payload, extra)
    if err:
        return err, None
    now = int(time.time() if now is None else now)
    scope_json = None if author_scope is None else json.dumps(sorted(author_scope))
    with get_conn(db_path) as conn:
        if watchdog_id is None:
            total = conn.execute("SELECT COUNT(*) AS n FROM watchdogs").fetchone()["n"]
            if total >= MAX_WATCHDOGS:
                return f"this hub already holds {MAX_WATCHDOGS} watchdogs", None
            cur = conn.execute(
                "INSERT INTO watchdogs(name, description, enabled, target_json, scope_json, "
                "service, grace_seconds, max_restarts, window_seconds, created_at, created_by, "
                "updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (clean["name"], clean["description"], int(clean["enabled"]),
                 json.dumps(clean["target"]), scope_json, clean["service"],
                 clean["grace_seconds"], clean["max_restarts"], clean["window_seconds"],
                 now, actor, now, actor))
            return None, cur.lastrowid
        exists = conn.execute("SELECT id FROM watchdogs WHERE id = ?", (watchdog_id,)).fetchone()
        if not exists:
            return "that watchdog no longer exists", None
        conn.execute(
            "UPDATE watchdogs SET name=?, description=?, enabled=?, target_json=?, "
            "scope_json=?, service=?, grace_seconds=?, max_restarts=?, window_seconds=?, "
            "updated_at=?, updated_by=? WHERE id=?",
            (clean["name"], clean["description"], int(clean["enabled"]),
             json.dumps(clean["target"]), scope_json, clean["service"],
             clean["grace_seconds"], clean["max_restarts"], clean["window_seconds"],
             now, actor, watchdog_id))
    return None, watchdog_id


def set_enabled(db_path, watchdog_id, enabled, actor="", now=None):
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        cur = conn.execute("UPDATE watchdogs SET enabled=?, updated_at=?, updated_by=? "
                           "WHERE id=?", (int(bool(enabled)), now, actor, watchdog_id))
        changed = cur.rowcount > 0
    if changed and not enabled:
        # A disabled watchdog's escalation cannot be acted on any more, so it must not stay
        # in the alert list -- the same reasoning as alerts.resolve_for_rule.
        alerts.resolve_for_watchdog(db_path, watchdog_id)
    return changed


def delete_watchdog(db_path, watchdog_id):
    """Delete a watchdog and everything hanging off it.

    The state and event rows go WITH it rather than being kept for the record. They are keyed
    on an id that is about to be reused by the next AUTOINCREMENT rollover-free insert only in
    theory, but the real reason is plainer: an event whose watchdog no longer exists cannot be
    explained to anybody reading it.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM watchdogs WHERE id = ?", (watchdog_id,))
        conn.execute("DELETE FROM machine_watchdog_state WHERE watchdog_id = ?", (watchdog_id,))
        conn.execute("DELETE FROM watchdog_events WHERE watchdog_id = ?", (watchdog_id,))
        deleted = cur.rowcount > 0
    if deleted:
        alerts.resolve_for_watchdog(db_path, watchdog_id)
    return deleted


# ---------------------------------------------------------------------------------------
# The document the machine holds
# ---------------------------------------------------------------------------------------


def document_version(entries):
    """A stable id for a resolved document, so the heartbeat can skip re-sending an unchanged
    one. A hash of the CONTENT rather than a counter, for the reasons policy.document_version
    gives: two hubs, a restore from backup, or a watchdog edited and edited back all produce
    the same document and should not cost every machine a re-application."""
    blob = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _machine_row(db_path, machine):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT machine, ad_ou, ad_dn FROM machine_info WHERE machine = ?",
                           (machine,)).fetchone()
    # A machine the hub has no row for still gets resolved, against the name alone: it is
    # mid-enrolment, and the `all` and `machines` selectors are both answerable without a row.
    return dict(row) if row else {"machine": machine, "ad_ou": "", "ad_dn": ""}


def watchdogs_for(db_path, machine):
    """Every ENABLED watchdog whose target -- narrowed to its author's scope -- covers this
    machine, in id order."""
    machine = str(machine or "").strip()
    if not machine:
        return []
    with get_conn(db_path) as conn:
        # The common case on every hub the day this ships, and the one worth one cheap query:
        # no watchdogs at all means the heartbeat does no target resolution whatsoever.
        rows = conn.execute("SELECT * FROM watchdogs WHERE enabled = 1 ORDER BY id").fetchall()
    if not rows:
        return []
    row_list = [_machine_row(db_path, machine)]
    out = []
    for row in rows:
        watchdog = _decode(row)
        try:
            covered = rules.scoped_targets(
                watchdog, rules.resolve_targets(db_path, watchdog["target"], row_list))
        except Exception as exc:                      # noqa: BLE001 -- one bad target must
            print(f"[watchdogs] target {watchdog['id']}: {exc}")   # not cost the others
            continue
        if covered:
            out.append(watchdog)
    return out


def resolve_for(db_path, machine):
    """The document this machine should hold. Returns `{"version", "watchdogs"}`.

    **An empty document is still a document**, and that is the release valve -- the same one
    policy.resolve_for documents. Removing a machine from a watchdog's target has to be able
    to STOP it watching, so "nothing applies" is `watchdogs: []` rather than an absent block,
    which the agent would read as "the hub had nothing to say" and go on doing what it was
    doing.

    Note what is NOT in here: no `max_age_seconds`. See the module docstring -- a watchdog has
    no dead-man expiry, deliberately and unlike an app policy.
    """
    entries = [{
        "id": w["id"],
        "service": w["service"],
        "grace_seconds": w["grace_seconds"],
        "max_restarts": w["max_restarts"],
        "window_seconds": w["window_seconds"],
    } for w in watchdogs_for(db_path, machine)]
    return {"version": document_version(entries), "watchdogs": entries}


# ---------------------------------------------------------------------------------------
# What the machine reports back
# ---------------------------------------------------------------------------------------


def _clean_state(raw):
    """One reported state, or None if it is not one.

    Everything here is bounded and coerced before it is stored, because it arrives from a
    machine. An unrecognised `status` is kept rather than dropped -- the console shows it,
    and `ESCALATING_STATUSES` is a membership test, so an unknown word can never raise an
    alert by accident.
    """
    if not isinstance(raw, dict):
        return None
    try:
        watchdog_id = int(raw.get("id"))
    except (TypeError, ValueError):
        return None
    status = _clean(raw.get("status"), 32)
    if not status:
        return None
    try:
        restarts = max(0, int(raw.get("restarts") or 0))
    except (TypeError, ValueError):
        restarts = 0
    last = raw.get("last_restart_at")
    try:
        last = int(last) if last not in (None, "") else None
    except (TypeError, ValueError):
        last = None
    return {"id": watchdog_id, "status": status, "restarts": restarts,
            "last_restart_at": last, "detail": _clean(raw.get("detail"), MAX_DETAIL_CHARS)}


def record_report(db_path, machine, payload, now=None):
    """Store what a machine just said about its watchdogs, and escalate what needs it.

    Returns a summary dict for the caller's log. Called from the heartbeat, so it must not
    raise for anything a machine can send; the caller wraps it anyway, on the same
    never-fatal rule every other heartbeat block follows.

    **An event row is written on a CHANGE, not on every report.** A machine reporting `ok`
    every thirty seconds for a year is one steady state, not a million rows -- but a restart
    is always an event, which is why `last_restart_at` advancing counts as a change even when
    the status word did not move.
    """
    machine = str(machine or "").strip()
    now = int(time.time() if now is None else now)
    states = (payload or {}).get("states") if isinstance(payload, dict) else None
    summary = {"stored": 0, "events": 0, "escalated": 0, "cleared": 0}
    if not machine or not isinstance(states, list):
        return summary

    known = {w["id"]: w for w in list_watchdogs(db_path)}
    escalations = []
    clears = []
    with get_conn(db_path) as conn:
        for raw in states[:MAX_WATCHDOGS]:
            state = _clean_state(raw)
            if state is None or state["id"] not in known:
                # A watchdog the hub does not hold any more. Dropped rather than stored: the
                # agent is one heartbeat away from being told to forget it, and a row nothing
                # can explain is worse than no row.
                continue
            previous = conn.execute(
                "SELECT status, last_restart_at FROM machine_watchdog_state "
                "WHERE machine=? AND watchdog_id=?", (machine, state["id"])).fetchone()
            conn.execute(
                "INSERT INTO machine_watchdog_state(machine, watchdog_id, status, detail, "
                "restarts, last_restart_at, reported_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(machine, watchdog_id) DO UPDATE SET status=excluded.status, "
                "detail=excluded.detail, restarts=excluded.restarts, "
                "last_restart_at=excluded.last_restart_at, reported_at=excluded.reported_at",
                (machine, state["id"], state["status"], state["detail"], state["restarts"],
                 state["last_restart_at"], now))
            summary["stored"] += 1

            changed = (previous is None
                       or previous["status"] != state["status"]
                       or (state["last_restart_at"] or 0) != (previous["last_restart_at"] or 0))
            if changed:
                conn.execute(
                    "INSERT INTO watchdog_events(watchdog_id, machine, at, status, detail) "
                    "VALUES (?,?,?,?,?)",
                    (state["id"], machine, now, state["status"], state["detail"]))
                summary["events"] += 1

            if state["status"] in ESCALATING_STATUSES:
                escalations.append((state, known[state["id"]]))
            elif previous is not None and previous["status"] in ESCALATING_STATUSES:
                # Recovered. Ending the episode leaves the alert open and visible and lets the
                # NEXT failure raise a fresh one beside it -- alerts.end_watchdog_episode.
                clears.append(state["id"])

    # Outside the connection, like rules.evaluate_once dispatches outside its own: alerts.py
    # opens its own, and two connections contending for one write lock is the deadlock
    # packages.py warns about.
    for state, watchdog in escalations:
        try:
            alerts.upsert_watchdog(db_path, machine, watchdog["id"], watchdog["name"],
                                   watchdog["service"], state["status"], state["detail"],
                                   now=now)
            summary["escalated"] += 1
        except Exception as exc:                      # noqa: BLE001
            print(f"[watchdogs] Could not raise an alert for {machine}/{watchdog['id']}: {exc}")
    for watchdog_id in clears:
        try:
            if alerts.end_watchdog_episode(db_path, machine, watchdog_id, now=now):
                summary["cleared"] += 1
        except Exception as exc:                      # noqa: BLE001
            print(f"[watchdogs] Could not clear the alert for {machine}/{watchdog_id}: {exc}")
    return summary


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------


def states_for(db_path, watchdog_id):
    """Every machine's last word on one watchdog, worst first.

    Ordered by status rather than by name on purpose: the page exists to answer "is anything
    wrong", and a hundred healthy PCs above the one that gave up is the same as not showing
    it.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM machine_watchdog_state WHERE watchdog_id = ?",
            (watchdog_id,)).fetchall()
    order = {STATUS_GIVEN_UP: 0, STATUS_FAILED: 1, STATUS_MISSING: 2,
             STATUS_RESTARTED: 3, STATUS_OK: 4}
    return sorted((dict(r) for r in rows),
                  key=lambda r: (order.get(r["status"], 2.5), r["machine"]))


def machine_states(db_path, machine):
    """Every watchdog this machine holds, for the machine page."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT s.*, w.name, w.service FROM machine_watchdog_state s "
            "JOIN watchdogs w ON w.id = s.watchdog_id WHERE s.machine = ? "
            "ORDER BY w.name, w.id", (machine,)).fetchall()
    return [dict(r) for r in rows]


def list_events(db_path, watchdog_id=None, machine=None, limit=100):
    clauses = []
    params = []
    if watchdog_id is not None:
        clauses.append("e.watchdog_id = ?")
        params.append(watchdog_id)
    if machine:
        clauses.append("e.machine = ?")
        params.append(machine)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 100), 500)))
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT e.*, w.name, w.service FROM watchdog_events e "
            f"LEFT JOIN watchdogs w ON w.id = e.watchdog_id {where} "
            f"ORDER BY e.at DESC, e.id DESC LIMIT ?", params).fetchall()
    return [dict(r) for r in rows]


def prune_events(db_path, retention_days, now=None):
    """Drop events older than the retention window. Same shape as rules.prune_fires, and
    called from the same maintenance pass."""
    days = int(retention_days or 0)
    if days <= 0:
        return 0
    cutoff = int(time.time() if now is None else now) - days * 86400
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM watchdog_events WHERE at < ?", (cutoff,))
        return cur.rowcount


def forget_machine(db_path, machine):
    """Drop everything this machine reported. Called when a machine is deleted."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_watchdog_state WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM watchdog_events WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move one machine's rows onto the surviving name (the dedup path in app.py).

    The state rows are MOVED with an OR REPLACE rather than merged: both names describe one
    PC, and the row being dropped is the older report of the same physical service.
    """
    with get_conn(db_path) as conn:
        conn.execute("UPDATE OR REPLACE machine_watchdog_state SET machine = ? WHERE machine = ?",
                     (new_machine, old_machine))
        conn.execute("UPDATE watchdog_events SET machine = ? WHERE machine = ?",
                     (new_machine, old_machine))
