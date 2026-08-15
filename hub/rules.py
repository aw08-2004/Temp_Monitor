"""Rules engine -- machine data as named variables, conditions over them, and actions.

This module is the foundation: it turns everything the hub knows about a machine into a
flat namespace of named, typed, AGE-STAMPED values that a condition can be written against.

The data it unifies is scattered by design -- the agent's two uplinks land temperature and
sensors in `readings`, identity in `machine_info`, logon sessions in `remote_inventory`,
NICs in `machine_nics`, firmware in `machine_bios`, processes in `machine_processes`, and
AD facts in `machine_info`'s ad_* columns. Each of those tables has its own cadence and its
own idea of "recent". A rule author should not have to know any of that; they should be able
to write `sys.uptime_days > 7` and have it mean the obvious thing.

Three properties this module exists to guarantee:

1. UNKNOWN is a third truth value, not false. Nearly every variable here can be absent (a
   machine that has never reported a BIOS block) or stale (an offline PC's last temperature,
   a process list nobody has asked for in an hour). Both resolve to UNKNOWN, comparisons
   involving UNKNOWN are UNKNOWN, and -- in phase 2's evaluator -- a rule fires only on TRUE.
   Without this, `disk.c.free_gb < 10` would fire on every machine that has never reported a
   disk, which is the single fastest way to make an alerting feature untrustworthy.

2. Every value carries its age. Staleness is per-SOURCE, not global: a BIOS inventory is
   rescanned six-hourly and a 3-hour-old one is perfectly good, while a 3-hour-old CPU load
   is meaningless. `max_age` on each variable encodes that, and it is the only thing standing
   between "this rule reads live state" and "this rule reads a fossil".

3. It is Flask-free and app-free, so it can be unit-tested in isolation -- the same house
   rule as fleet.py, alerts.py and packages.py. In particular it does NOT import
   app.extract_diagnostics (that would be a circular import and would drag Flask in);
   the caller passes the already-computed diagnostics dict in. That is the right seam
   anyway: this module's job is mapping data onto a namespace, not parsing sensor blobs.

Phase 1 (this file, for now): the variable catalog, the resolver, operator-defined custom
fields, and message templating. Conditions, rules and actions build on top in later phases.
"""
import json
import re
import sqlite3
import time
from collections import namedtuple
from datetime import datetime

import alerts
import fleet

# ---------------------------------------------------------------------------------------
# UNKNOWN
# ---------------------------------------------------------------------------------------


class _Unknown:
    """The 'we do not know' value. A singleton so callers can use `is UNKNOWN`.

    Deliberately NOT None: None is a legitimate resolved value in a few places (a BIOS that
    genuinely reports "password state is unreadable"), and more importantly a bare None
    compares as falsey, which is exactly the bug this whole sentinel exists to prevent.
    Falsey here too -- but nothing in the evaluator is allowed to test it for truthiness,
    and `__bool__` raising would make defensive `if value:` in UI code explode instead of
    degrade, so it stays quietly false and the evaluator checks identity.
    """

    __slots__ = ()

    def __repr__(self):
        return "UNKNOWN"

    def __bool__(self):
        return False


UNKNOWN = _Unknown()

# Value kinds. These drive the operator list the condition builder offers, how a literal is
# parsed in the text expression, and how a value renders into a message.
KIND_TEXT = "text"
KIND_NUMBER = "number"
KIND_BOOL = "bool"
KIND_CHOICE = "choice"          # custom fields only: text drawn from a fixed list
VALUE_KINDS = (KIND_TEXT, KIND_NUMBER, KIND_BOOL, KIND_CHOICE)


class Value(namedtuple("Value", "value kind age_seconds")):
    """One resolved variable: what it is, what type it is, and how old the underlying
    report is. `age_seconds` is None for values that have no meaningful age (identity,
    operator-set fields) -- distinct from 0, which means "reported just now"."""

    __slots__ = ()

    @property
    def known(self):
        return self.value is not UNKNOWN


def unknown(kind):
    return Value(UNKNOWN, kind, None)


# ---------------------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------------------

# `unit` is optional and carries no weight in comparisons -- with one exception that earns
# it: a duration literal (`7d`) written against a variable measured in DAYS must mean 7, and
# against one measured in SECONDS must mean 604800. Without the unit recorded here, the
# obvious `sys.uptime_days > 7d` would compare days against 604800 and never fire, which is
# precisely the sort of silently-wrong rule this module exists to make impossible.
Var = namedtuple("Var", "name kind group max_age unit")
Var.__new__.__defaults__ = (None,)

UNIT_SECONDS = "seconds"
UNIT_DAYS = "days"
DURATION_UNITS = (UNIT_SECONDS, UNIT_DAYS)

# max_age is the oldest a source report may be before the variable reads UNKNOWN, in
# seconds; None means the value never goes stale. The numbers below are each ~3x the
# cadence the data actually arrives at, so a couple of missed reports degrade nothing:
#
#   live     900   temp/sensors arrive every 5-10s; machine_info.updated_at is refreshed at
#                  most every ~30s by persist_live_status. 15 minutes tolerates a reboot.
#   session  900   RemoteInventoryReporter rescans every 60s and sends only on change, so a
#                  machine whose sessions are stable sends nothing -- but the heartbeat that
#                  carries it runs every 10s, and record_inventory stamps reported_at on
#                  every write, so absence for 15 minutes really does mean out of contact.
#   network 10800  NetworkInventoryReporter rescans every 15 minutes, change-only.
#   bios     None  BiosInventoryReporter rescans six-hourly; firmware simply does not change
#                  on its own, so an old inventory is not a wrong one. Age is still reported
#                  so the UI can show it.
#   proc      300  Demand-only -- the process list rides the heartbeat solely while somebody
#                  has the Processes card open. So proc.* is UNKNOWN on virtually every
#                  machine virtually all the time, which is correct and intended: a rule
#                  must not conclude "0 processes" from "nobody was looking".
#   ad     172800  directory.sync_once runs on its own schedule; 2 days survives a weekend
#                  with the sync disabled.
#
# Identity (hw.*, sys.machine) never expires: a serial number is not a measurement.
AGE_LIVE = 900
AGE_SESSION = 900
AGE_NETWORK = 10800
AGE_BIOS = None
AGE_PROC = 300
AGE_AD = 172800

GROUP_SYS = "sys"
GROUP_HW = "hw"
GROUP_METRIC = "metric"
GROUP_DISK = "disk"
GROUP_SESSION = "session"
GROUP_NET = "net"
GROUP_BIOS = "bios"
GROUP_AD = "ad"
GROUP_PROC = "proc"
GROUP_FIELD = "field"

STATIC_VARIABLES = (
    # -- sys: liveness and the agent itself -------------------------------------------
    Var("sys.machine", KIND_TEXT, GROUP_SYS, None),
    Var("sys.uptime_seconds", KIND_NUMBER, GROUP_SYS, AGE_LIVE, UNIT_SECONDS),
    Var("sys.uptime_days", KIND_NUMBER, GROUP_SYS, AGE_LIVE, UNIT_DAYS),
    Var("sys.status", KIND_TEXT, GROUP_SYS, None),
    Var("sys.online", KIND_BOOL, GROUP_SYS, None),
    Var("sys.enrolled", KIND_BOOL, GROUP_SYS, None),
    Var("sys.last_seen_seconds_ago", KIND_NUMBER, GROUP_SYS, None, UNIT_SECONDS),
    Var("sys.agent_version", KIND_TEXT, GROUP_SYS, None),
    # -- hw: identity, read once at agent startup and never refreshed -------------------
    Var("hw.model", KIND_TEXT, GROUP_HW, None),
    Var("hw.manufacturer", KIND_TEXT, GROUP_HW, None),
    Var("hw.serial_number", KIND_TEXT, GROUP_HW, None),
    Var("hw.service_tag", KIND_TEXT, GROUP_HW, None),
    Var("hw.asset_tag", KIND_TEXT, GROUP_HW, None),
    # -- metric: the chartable scalars, out of extract_diagnostics ----------------------
    Var("metric.cpu_temp", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.cpu_load_pct", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.cpu_power_w", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.gpu_temp", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.gpu_load_pct", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.gpu_power_w", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.memory_load_pct", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.mem_used_gb", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.mem_total_gb", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.disk_load_pct", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.fan_rpm", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.net_rx_bps", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    Var("metric.net_tx_bps", KIND_NUMBER, GROUP_METRIC, AGE_LIVE),
    # -- disk: aggregates. Per-volume disk.<letter>.* are added dynamically -------------
    # The aggregates matter more than they look. "any drive is nearly full" is the rule
    # people actually want, and writing it per drive letter means a rule that silently
    # misses the machine with an extra data disk.
    Var("disk.count", KIND_NUMBER, GROUP_DISK, AGE_LIVE),
    Var("disk.max_used_pct", KIND_NUMBER, GROUP_DISK, AGE_LIVE),
    Var("disk.min_free_gb", KIND_NUMBER, GROUP_DISK, AGE_LIVE),
    # -- session: who is logged in ------------------------------------------------------
    Var("session.count", KIND_NUMBER, GROUP_SESSION, AGE_SESSION),
    Var("session.user", KIND_TEXT, GROUP_SESSION, AGE_SESSION),
    Var("session.account", KIND_TEXT, GROUP_SESSION, AGE_SESSION),
    Var("session.domain", KIND_TEXT, GROUP_SESSION, AGE_SESSION),
    Var("session.console_active", KIND_BOOL, GROUP_SESSION, AGE_SESSION),
    Var("session.at_logon_screen", KIND_BOOL, GROUP_SESSION, AGE_SESSION),
    # -- net ----------------------------------------------------------------------------
    Var("net.ipv4", KIND_TEXT, GROUP_NET, AGE_NETWORK),
    Var("net.mac", KIND_TEXT, GROUP_NET, AGE_NETWORK),
    Var("net.link_up", KIND_BOOL, GROUP_NET, AGE_NETWORK),
    Var("net.wake_enabled", KIND_BOOL, GROUP_NET, AGE_NETWORK),
    Var("net.fast_startup", KIND_BOOL, GROUP_NET, AGE_NETWORK),
    # -- bios ---------------------------------------------------------------------------
    Var("bios.version", KIND_TEXT, GROUP_BIOS, AGE_BIOS),
    Var("bios.vendor", KIND_TEXT, GROUP_BIOS, AGE_BIOS),
    Var("bios.support", KIND_TEXT, GROUP_BIOS, AGE_BIOS),
    Var("bios.password_set", KIND_BOOL, GROUP_BIOS, AGE_BIOS),
    # -- ad -----------------------------------------------------------------------------
    Var("ad.ou", KIND_TEXT, GROUP_AD, AGE_AD),
    Var("ad.dn", KIND_TEXT, GROUP_AD, AGE_AD),
    Var("ad.owner", KIND_TEXT, GROUP_AD, AGE_AD),
    Var("ad.os", KIND_TEXT, GROUP_AD, AGE_AD),
    Var("ad.disabled", KIND_BOOL, GROUP_AD, AGE_AD),
    # -- proc: present only while an operator has the Processes card open ---------------
    Var("proc.count", KIND_NUMBER, GROUP_PROC, AGE_PROC),
    Var("proc.cpu_cores", KIND_NUMBER, GROUP_PROC, AGE_PROC),
    Var("proc.mem_total_mb", KIND_NUMBER, GROUP_PROC, AGE_PROC),
)

STATIC_BY_NAME = {v.name: v for v in STATIC_VARIABLES}

# Per-volume variables, expanded per drive letter a machine actually reports.
DISK_VOLUME_SUFFIXES = (
    ("used_pct", KIND_NUMBER),
    ("free_gb", KIND_NUMBER),
    ("used_gb", KIND_NUMBER),
    ("total_gb", KIND_NUMBER),
)

# i18n: every catalog entry needs `rules.variable.<name>.label` and `.description` in
# hub/i18n/en.json, the same contract packages.step.<kind>.* has. tests/test_i18n.py fails
# on a missing one, which is the point -- a variable nobody can read the name of is a
# variable nobody will use correctly.
VARIABLE_TEXT_KEY = "rules.variable"
FIELD_KIND_TEXT_KEY = "rules.field_kind"

# A custom field / derived variable / probe name. Same shape as packages.py's variable names
# (lowercase, underscore, bounded) so the two namespaces cannot drift apart, and so a name is
# always safe to embed in a dotted variable and in a {{...}} template placeholder.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_FIELD_VALUE_CHARS = 512


def is_valid_name(name):
    return bool(_NAME_RE.match(str(name or "")))


# ---------------------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------------------


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_rules_db(db_path):
    """Create the rules-engine tables if absent. Idempotent -- called next to
    app.init_db()/fleet.init_fleet_db()/alerts.init_alerts_db() on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # Operator-defined per-machine fields. The DEFINITION is fleet-wide (one row per
        # field), the VALUES are per machine. Splitting them is what makes "add a
        # `location` field" a single act rather than something you retro-fit onto every
        # machine row, and it is what lets the condition builder offer `field.location`
        # as a first-class variable before any machine has a value for it.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_fields (
                name          TEXT PRIMARY KEY,
                label         TEXT NOT NULL,
                kind          TEXT NOT NULL,
                choices_json  TEXT NOT NULL DEFAULT '[]',
                default_value TEXT,
                description   TEXT NOT NULL DEFAULT '',
                created_at    INTEGER NOT NULL,
                created_by    TEXT NOT NULL DEFAULT '',
                updated_at    INTEGER NOT NULL,
                updated_by    TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Values are stored as TEXT whatever the kind, and coerced on read. SQLite would
        # happily hold mixed types in one column, but a rule comparing `field.headcount > 5`
        # must not depend on whether the operator who typed "12" hit a numeric keypad --
        # one storage type plus one coercion path means one answer.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_field_values (
                machine    TEXT NOT NULL,
                name       TEXT NOT NULL,
                value      TEXT,
                updated_at INTEGER NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (machine, name)
            )
            """
        )
        # Read pattern is "every field value for one machine" (the resolver, once per
        # machine per evaluation tick) and "every machine with a value for one field" (the
        # `field` target selector in phase 3). The PK covers the first; this covers the
        # second.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_machine_field_values_name "
            "ON machine_field_values(name, value)"
        )
        # The rules themselves. `target_json` is who, `condition_json` is when, `actions_json`
        # is what -- three independent halves (thirds) that the editor presents separately and
        # that validate separately. `condition_text` is the same condition rendered back to
        # the expression language, stored so the text editor opens on what the author typed
        # rather than on a re-derivation of it; it is never the source of truth.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rules (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL,
                description          TEXT NOT NULL DEFAULT '',
                enabled              INTEGER NOT NULL DEFAULT 1,
                target_json          TEXT NOT NULL,
                condition_json       TEXT NOT NULL,
                condition_text       TEXT NOT NULL DEFAULT '',
                for_seconds          INTEGER NOT NULL DEFAULT 0,
                cooldown_seconds     INTEGER NOT NULL DEFAULT 3600,
                max_targets_per_tick INTEGER NOT NULL DEFAULT 25,
                actions_json         TEXT NOT NULL,
                created_at           INTEGER NOT NULL,
                created_by           TEXT NOT NULL DEFAULT '',
                updated_at           INTEGER NOT NULL,
                updated_by           TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Per (rule, machine) evaluation state. This is what turns a momentary condition into
        # a debounced, cooled-down, snoozeable one:
        #
        #   matched_since  when the condition first became TRUE and has been TRUE since.
        #                  NULL the moment it stops being TRUE -- including when it goes
        #                  UNKNOWN, because "we lost contact" must not accumulate toward
        #                  "has been true for 10 minutes".
        #   last_fired_at  cooldown anchor.
        #   snoozed_until  set by a snooze action (a user pressing "Later"). Independent of
        #                  cooldown on purpose: one is the rule's global pacing, the other is
        #                  one person deferring one prompt on one PC.
        #   firing         whether this machine is currently inside a matched episode, so a
        #                  rule can be reported as "12 machines matching" without re-running.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_state (
                rule_id       INTEGER NOT NULL,
                machine       TEXT NOT NULL,
                matched_since INTEGER,
                last_fired_at INTEGER,
                snoozed_until INTEGER,
                firing        INTEGER NOT NULL DEFAULT 0,
                updated_at    INTEGER NOT NULL,
                PRIMARY KEY (rule_id, machine)
            )
            """
        )
        # Fire history: one row per (rule, machine, firing). Answers "who declined the
        # restart, and when" -- which is the question that gets asked three months later, so
        # it is a table rather than a log line. `outcome` is filled in when a show_message
        # comes back; `actions_json` records what was actually dispatched, which is not always
        # what the rule says (the kill switch may have suppressed a command).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_fires (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id     INTEGER NOT NULL,
                machine     TEXT NOT NULL,
                fired_at    INTEGER NOT NULL,
                actions_json TEXT NOT NULL DEFAULT '[]',
                command_id  TEXT,
                outcome     TEXT,
                outcome_at  INTEGER,
                detail      TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rule_fires_rule "
                     "ON rule_fires(rule_id, fired_at DESC)")
        # The routing lookup: a show_message result arrives knowing only its command id.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rule_fires_command "
                     "ON rule_fires(command_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rule_fires_machine "
                     "ON rule_fires(machine, fired_at DESC)")
        # Derived variables: a named arithmetic expression over other variables, so
        # "free space as a percentage" is defined once rather than re-typed into six rules.
        # Stored as text and re-parsed on read: the expression is short, the parse is cheap,
        # and keeping ONE representation means the editor and the evaluator cannot disagree
        # about what it says.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS derived_vars (
                name        TEXT PRIMARY KEY,
                expression  TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                unit        TEXT NOT NULL DEFAULT '',
                created_at  INTEGER NOT NULL,
                created_by  TEXT NOT NULL DEFAULT '',
                updated_at  INTEGER NOT NULL,
                updated_by  TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Probes: values the agent has to go and fetch, because nothing in the regular
        # telemetry carries them (is this app installed? what version? does this file exist?).
        # `interval_seconds` is per probe rather than fleet-wide: a registry read is cheap
        # enough to do hourly, and a script is not.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS probes (
                name             TEXT PRIMARY KEY,
                kind             TEXT NOT NULL,
                spec_json        TEXT NOT NULL DEFAULT '{}',
                value_kind       TEXT NOT NULL DEFAULT 'text',
                interval_seconds INTEGER NOT NULL DEFAULT 3600,
                timeout_seconds  INTEGER NOT NULL DEFAULT 30,
                enabled          INTEGER NOT NULL DEFAULT 1,
                description      TEXT NOT NULL DEFAULT '',
                created_at       INTEGER NOT NULL,
                created_by       TEXT NOT NULL DEFAULT '',
                updated_at       INTEGER NOT NULL,
                updated_by       TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # One row per (machine, probe). `error` and `value` are independent: a probe that
        # failed keeps its last good value AND records why the refresh failed, so an operator
        # can tell "this machine has never answered" from "it answered last week and has been
        # erroring since". Staleness then does the rest -- the resolver ages the value out.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_probe_values (
                machine      TEXT NOT NULL,
                name         TEXT NOT NULL,
                value        TEXT,
                collected_at INTEGER,
                requested_at INTEGER,
                error        TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (machine, name)
            )
            """
        )


# ---- custom field definitions ---------------------------------------------------------


def _decode_field(row):
    field = dict(row)
    try:
        field["choices"] = json.loads(field.pop("choices_json", None) or "[]")
    except (TypeError, ValueError):
        field["choices"] = []
    return field


def list_fields(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM custom_fields ORDER BY name").fetchall()
    return [_decode_field(r) for r in rows]


def get_field(db_path, name):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM custom_fields WHERE name = ?", (name,)).fetchone()
    return _decode_field(row) if row else None


def validate_field(name, label, kind, choices=None, default_value=None):
    """Returns (error_or_None, normalised_dict). Pure -- no DB, so it is testable and so the
    same checks can run on a bulk import later."""
    if not is_valid_name(name):
        return ("name must be lowercase letters, digits and underscores, "
                "starting with a letter, at most 32 characters"), None
    label = str(label or "").strip()
    if not label:
        return "label is required", None
    if len(label) > 100:
        return "label is too long", None
    if kind not in VALUE_KINDS:
        return f"kind must be one of {', '.join(VALUE_KINDS)}", None

    normalised_choices = []
    if kind == KIND_CHOICE:
        for choice in (choices or []):
            text = str(choice).strip()
            if not text:
                continue
            if len(text) > MAX_FIELD_VALUE_CHARS:
                return "choice is too long", None
            if text not in normalised_choices:
                normalised_choices.append(text)
        if not normalised_choices:
            return "a choice field needs at least one choice", None
        if len(normalised_choices) > 100:
            return "too many choices", None
    elif choices:
        return "only a choice field can have choices", None

    if default_value is not None and str(default_value).strip() != "":
        err, coerced = coerce_field_value(kind, default_value, normalised_choices)
        if err:
            return f"default value: {err}", None
        default_value = str(coerced)
    else:
        default_value = None

    return None, {"name": name, "label": label, "kind": kind,
                  "choices": normalised_choices, "default_value": default_value}


def coerce_field_value(kind, raw, choices=()):
    """Text off the wire (or out of the DB) -> a typed Python value. Returns (error, value).

    An empty string is not an error and not a zero: it is "no value set", which resolves to
    UNKNOWN. That distinction is the whole reason this returns a tuple rather than raising --
    clearing a field is a legitimate edit, and it must not become `0` or `False`.
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if text == "":
        return None, None
    if len(text) > MAX_FIELD_VALUE_CHARS:
        return "value is too long", None
    if kind == KIND_NUMBER:
        try:
            number = float(text)
        except ValueError:
            return "value must be a number", None
        if number != number or number in (float("inf"), float("-inf")):
            return "value must be a finite number", None
        return None, int(number) if number.is_integer() else number
    if kind == KIND_BOOL:
        lowered = text.lower()
        if lowered in ("1", "true", "yes", "on"):
            return None, True
        if lowered in ("0", "false", "no", "off"):
            return None, False
        return "value must be true or false", None
    if kind == KIND_CHOICE:
        if choices and text not in choices:
            return "value is not one of the allowed choices", None
        return None, text
    return None, text


def save_field(db_path, name, label, kind, choices=None, default_value=None,
               description="", actor="", now=None):
    """Create or update a field definition. Returns (error, field).

    The KIND of an existing field is deliberately immutable. Changing `headcount` from
    number to text would leave every stored value valid but every rule comparing it
    silently rewritten from arithmetic to string ordering -- "9" > "10" is true for text.
    Delete and recreate is the honest path, and it makes the operator confront the values
    they are throwing away.
    """
    err, normalised = validate_field(name, label, kind, choices, default_value)
    if err:
        return err, None
    now = int(now if now is not None else time.time())
    existing = get_field(db_path, normalised["name"])
    if existing and existing["kind"] != normalised["kind"]:
        return ("a field's kind cannot be changed after it is created; "
                "delete the field and create it again"), None
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO custom_fields (name, label, kind, choices_json, default_value,
                                       description, created_at, created_by,
                                       updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                label         = excluded.label,
                choices_json  = excluded.choices_json,
                default_value = excluded.default_value,
                description   = excluded.description,
                updated_at    = excluded.updated_at,
                updated_by    = excluded.updated_by
            """,
            (normalised["name"], normalised["label"], normalised["kind"],
             json.dumps(normalised["choices"]), normalised["default_value"],
             str(description or "")[:500], now, str(actor or ""), now, str(actor or "")),
        )
    return None, get_field(db_path, normalised["name"])


def delete_field(db_path, name):
    """Drop a field definition and every machine's value for it.

    The values go too, deliberately. A definition-less value is unreachable -- nothing can
    read it, nothing can edit it, and recreating the field later would resurrect stale data
    an operator had every reason to believe was gone.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_field_values WHERE name = ?", (name,))
        cur = conn.execute("DELETE FROM custom_fields WHERE name = ?", (name,))
        return cur.rowcount > 0


# ---- custom field values --------------------------------------------------------------


def get_machine_fields(db_path, machine):
    """Raw stored text per field name for one machine. Coercion happens in the resolver."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT name, value, updated_at FROM machine_field_values WHERE machine = ?",
            (machine,),
        ).fetchall()
    return {r["name"]: r["value"] for r in rows}


def set_machine_field(db_path, machine, name, value, actor="", now=None):
    """Set (or clear) one field on one machine. Returns (error, None).

    Clearing is `value=None` or an empty string, and it DELETES the row rather than storing
    an empty one -- so `field.x` on a machine that was cleared reads UNKNOWN, identically to
    a machine that was never set. Two ways to express "no value" would be two behaviours to
    keep in step forever.
    """
    field = get_field(db_path, name)
    if not field:
        return f"unknown field: {name}", None
    err, coerced = coerce_field_value(field["kind"], value, field["choices"])
    if err:
        return err, None
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        if coerced is None:
            conn.execute("DELETE FROM machine_field_values WHERE machine = ? AND name = ?",
                         (machine, name))
        else:
            conn.execute(
                """
                INSERT INTO machine_field_values (machine, name, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(machine, name) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (machine, name, str(coerced), now, str(actor or "")),
            )
    return None, None


def set_machine_field_bulk(db_path, machines, name, value, actor="", now=None):
    """Set one field across many machines -- the primary editing path, since fields exist to
    be applied to a selection ("these forty PCs are Branch 2"), not typed one at a time.

    Validates ONCE and then writes, so a bad value fails before touching anything rather
    than half-applying across the selection. Returns (error, count)."""
    field = get_field(db_path, name)
    if not field:
        return f"unknown field: {name}", 0
    err, coerced = coerce_field_value(field["kind"], value, field["choices"])
    if err:
        return err, 0
    now = int(now if now is not None else time.time())
    names = [str(m).strip() for m in machines if str(m or "").strip()]
    if not names:
        return "no machines selected", 0
    with get_conn(db_path) as conn:
        if coerced is None:
            conn.executemany(
                "DELETE FROM machine_field_values WHERE machine = ? AND name = ?",
                [(m, name) for m in names],
            )
        else:
            conn.executemany(
                """
                INSERT INTO machine_field_values (machine, name, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(machine, name) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                [(m, name, str(coerced), now, str(actor or "")) for m in names],
            )
    return None, len(names)


# ---------------------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------------------


def _num(raw):
    """A number, or UNKNOWN. Rejects bool (True would otherwise arrive as 1) and non-finite
    floats (a NaN silently makes every comparison false, which is a lie -- UNKNOWN is the
    truth)."""
    if raw is None or isinstance(raw, bool):
        return UNKNOWN
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            return UNKNOWN
        return int(raw) if isinstance(raw, int) else value
    return UNKNOWN


def _text(raw):
    if raw is None:
        return UNKNOWN
    text = str(raw).strip()
    return text if text else UNKNOWN


def _bool(raw):
    """SQLite has no boolean; nullable flags arrive as 0/1/None. None means "unknowable",
    which several of these genuinely are -- bios.password_set is documented nullable
    precisely because some vendors will not say."""
    if raw is None:
        return UNKNOWN
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    lowered = str(raw).strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    return UNKNOWN


def _age(now, reported_at):
    if reported_at is None:
        return None
    try:
        return max(0, int(now) - int(reported_at))
    except (TypeError, ValueError):
        return None


def parse_timestamp(value):
    """machine_info.updated_at -> epoch seconds.

    That column holds a naive LOCAL-time "%Y-%m-%d %H:%M:%S" string (app.to_timestamp_str),
    not an epoch and not UTC. Parsed here rather than taking app.parse_request_datetime
    because this module stays app-free; the format is app's to change, so a caller that has
    already parsed it can pass the epoch in as `updated_at_epoch` and skip this entirely.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    cleaned = str(value).strip().replace("T", " ")
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            parsed = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    try:
        return int(parsed.timestamp())
    except (OSError, OverflowError, ValueError):
        return None


def _put(out, var, raw, age=None):
    """Store one resolved variable, applying the staleness rule.

    This is the single chokepoint where age turns into UNKNOWN. Every source funnels through
    it so that no future variable can accidentally be added without a staleness answer.

    Note the second branch: a variable that HAS a freshness requirement but whose age could
    not be determined reads UNKNOWN, not "fine". An unparseable or absent timestamp means we
    cannot show the value is current, and for a module whose entire premise is "never fire on
    data you cannot vouch for", cannot-show is the same as is-not.
    """
    if raw is UNKNOWN:
        out[var.name] = Value(UNKNOWN, var.kind, age)
        return
    if var.max_age is not None and (age is None or age > var.max_age):
        out[var.name] = Value(UNKNOWN, var.kind, age)
        return
    out[var.name] = Value(raw, var.kind, age)


def _disk_letter(name):
    """"C: (Windows)" -> "c". The agent builds this string in VolumeReader as
    "<letter>: (<label>)", so the letter is the first character followed by a colon.
    Anything else (the pre-3.10.0 per-device fallback, which has no letter at all) returns
    None and is represented only in the aggregates."""
    text = str(name or "").strip()
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return text[0].lower()
    return None


def disk_variables(disks):
    """The dynamic per-volume half of the catalog for one machine's disk list."""
    out = []
    for disk in disks or []:
        letter = _disk_letter(disk.get("name"))
        if not letter:
            continue
        for suffix, kind in DISK_VOLUME_SUFFIXES:
            out.append(Var(f"disk.{letter}.{suffix}", kind, GROUP_DISK, AGE_LIVE))
    return out


def _resolve_disks(out, diagnostics, age):
    disks = (diagnostics or {}).get("disks") or []
    used_pcts, free_gbs = [], []
    for disk in disks:
        used_gb = disk.get("used_gb")
        total_gb = disk.get("total_gb")
        used_pct = disk.get("used_pct")
        free_gb = None
        if isinstance(used_gb, (int, float)) and isinstance(total_gb, (int, float)):
            free_gb = round(float(total_gb) - float(used_gb), 1)
        if isinstance(used_pct, (int, float)):
            used_pcts.append(float(used_pct))
        if free_gb is not None:
            free_gbs.append(free_gb)

        letter = _disk_letter(disk.get("name"))
        if not letter:
            # The pre-3.10.0 fallback shape: a percentage with no drive letter and no size.
            # It still counts toward max_used_pct -- dropping it would make "any disk nearly
            # full" quietly blind on older agents, which is the opposite of degrading well.
            continue
        prefix = f"disk.{letter}"
        _put(out, Var(f"{prefix}.used_pct", KIND_NUMBER, GROUP_DISK, AGE_LIVE),
             _num(used_pct), age)
        _put(out, Var(f"{prefix}.free_gb", KIND_NUMBER, GROUP_DISK, AGE_LIVE),
             _num(free_gb), age)
        _put(out, Var(f"{prefix}.used_gb", KIND_NUMBER, GROUP_DISK, AGE_LIVE),
             _num(used_gb), age)
        _put(out, Var(f"{prefix}.total_gb", KIND_NUMBER, GROUP_DISK, AGE_LIVE),
             _num(total_gb), age)

    # `disk.count` is 0, not UNKNOWN, when we have sensors and they contain no volumes --
    # that is a real answer. It is UNKNOWN only when there is no sensor block at all.
    has_sensors = bool((diagnostics or {}).get("has_sensors"))
    _put(out, STATIC_BY_NAME["disk.count"],
         _num(len(disks)) if has_sensors else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["disk.max_used_pct"],
         _num(round(max(used_pcts), 1)) if used_pcts else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["disk.min_free_gb"],
         _num(min(free_gbs)) if free_gbs else UNKNOWN, age)


def _pick_session(sessions):
    """The one session `session.user` and friends describe.

    The console session wins, then any other signed-in one; the logon screen never does. A
    machine sitting at the lock screen has a WinSta session with no human behind it, and a
    rule that pops a dialog "for the logged-in user" must not target it -- that message goes
    to a desktop nobody will look at until the next person signs in and sees a stale prompt.
    """
    real = [s for s in (sessions or [])
            if isinstance(s, dict) and not s.get("is_logon_screen") and str(s.get("user") or "").strip()]
    if not real:
        return None
    for session in real:
        if session.get("is_console"):
            return session
    return real[0]


def resolve_machine_vars(db_path, machine, *, now=None, diagnostics=None, live=None,
                         online_window=None, enrolled=None, fields=None, row=None):
    """Every variable for one machine: {name: Value}.

    Arguments the caller supplies rather than this module computing them, and why:

    * `diagnostics` -- the output of app.extract_diagnostics() for this machine's most recent
      sensor block. Passed in because importing app here would be circular and would pull
      Flask into a module that is deliberately free of it. Pass None for a machine with no
      sensors; every metric.* and disk.* then reads UNKNOWN, which is correct.
    * `live` -- {"temp", "uptime_seconds", "boot_epoch"} from app's in-memory caches, which
      are fresher than the ~30s-throttled DB mirror. Optional; the DB row is the fallback.
    * `enrolled` -- fleet.is_enrolled(), which lives in fleet.py. Passing it avoids importing
      a second module for one boolean and lets the phase-3 evaluator hoist it out of the
      per-machine loop.
    * `row`/`fields` -- pre-fetched machine_info row and field values, so the evaluator can
      batch its reads across the fleet instead of doing two queries per machine per tick.

    Everything is optional and everything degrades to UNKNOWN, so a caller that knows only
    the machine name still gets a usable (if sparse) namespace.
    """
    now = int(now if now is not None else time.time())
    out = {}

    if row is None:
        with get_conn(db_path) as conn:
            row = conn.execute("SELECT * FROM machine_info WHERE machine = ?",
                               (machine,)).fetchone()
    info = dict(row) if row else {}
    live = live or {}

    # -- liveness -----------------------------------------------------------------------
    # updated_at is stored as a text timestamp; the caller passes the epoch it already
    # parsed for the status column rather than making this module re-parse a format it does
    # not own.
    reported_at = live.get("reported_at")
    if reported_at is None:
        reported_at = info.get("updated_at_epoch")
    if reported_at is None:
        reported_at = parse_timestamp(info.get("updated_at"))
    live_age = _age(now, reported_at)

    _put(out, STATIC_BY_NAME["sys.machine"], _text(machine))
    _put(out, STATIC_BY_NAME["sys.agent_version"], _text(info.get("companion_version")))

    last_seen_ago = live_age
    _put(out, STATIC_BY_NAME["sys.last_seen_seconds_ago"],
         _num(last_seen_ago) if last_seen_ago is not None else UNKNOWN)
    window = online_window if online_window is not None else 120
    is_online = (last_seen_ago is not None and last_seen_ago <= window)
    _put(out, STATIC_BY_NAME["sys.online"],
         is_online if last_seen_ago is not None else UNKNOWN)
    _put(out, STATIC_BY_NAME["sys.status"],
         ("online" if is_online else "offline") if last_seen_ago is not None else UNKNOWN)
    _put(out, STATIC_BY_NAME["sys.enrolled"], _bool(enrolled) if enrolled is not None else UNKNOWN)

    # -- uptime -------------------------------------------------------------------------
    # Preferred source is boot_epoch, so uptime advances in real time between the agent's
    # 600-second uptime reports instead of sitting up to ten minutes stale. The raw
    # uptime_seconds is the fallback for a machine that has not reported since boot_epoch
    # was introduced.
    boot_epoch = live.get("boot_epoch")
    if boot_epoch is None:
        boot_epoch = info.get("boot_epoch")
    uptime_seconds = UNKNOWN
    if isinstance(boot_epoch, (int, float)) and not isinstance(boot_epoch, bool) and boot_epoch > 0:
        uptime_seconds = max(0, now - int(boot_epoch))
    else:
        raw_uptime = live.get("uptime_seconds")
        if raw_uptime is None:
            raw_uptime = info.get("last_uptime_seconds")
        uptime_seconds = _num(raw_uptime)
    _put(out, STATIC_BY_NAME["sys.uptime_seconds"], uptime_seconds, live_age)
    _put(out, STATIC_BY_NAME["sys.uptime_days"],
         round(uptime_seconds / 86400.0, 2) if uptime_seconds is not UNKNOWN else UNKNOWN,
         live_age)

    # -- identity: never stale, so no age ------------------------------------------------
    for name, column in (("hw.model", "model"), ("hw.manufacturer", "manufacturer"),
                         ("hw.serial_number", "serial_number"),
                         ("hw.service_tag", "service_tag"), ("hw.asset_tag", "asset_tag")):
        _put(out, STATIC_BY_NAME[name], _text(info.get(column)))

    # -- metrics -------------------------------------------------------------------------
    diag = diagnostics or {}
    temp = live.get("temp")
    if temp is None:
        temp = info.get("last_temp")
    _put(out, STATIC_BY_NAME["metric.cpu_temp"], _num(temp), live_age)
    for name in ("cpu_load_pct", "cpu_power_w", "gpu_temp", "gpu_load_pct", "gpu_power_w",
                 "memory_load_pct", "mem_used_gb", "mem_total_gb", "disk_load_pct",
                 "fan_rpm", "net_rx_bps", "net_tx_bps"):
        _put(out, STATIC_BY_NAME[f"metric.{name}"], _num(diag.get(name)), live_age)

    _resolve_disks(out, diag, live_age)

    # -- sub-payloads, each with its own reported_at ------------------------------------
    _resolve_remote(db_path, machine, out, now)
    _resolve_network(db_path, machine, out, now)
    _resolve_bios(db_path, machine, out, now)
    _resolve_processes(db_path, machine, out, now)

    # -- AD ------------------------------------------------------------------------------
    ad_age = _age(now, info.get("ad_synced_at"))
    for name, column in (("ad.ou", "ad_ou"), ("ad.dn", "ad_dn"),
                         ("ad.owner", "ad_owner"), ("ad.os", "ad_os")):
        _put(out, STATIC_BY_NAME[name], _text(info.get(column)), ad_age)
    _put(out, STATIC_BY_NAME["ad.disabled"], _bool(info.get("ad_disabled")), ad_age)

    # -- custom fields, probes, then derived --------------------------------------------
    # Order is load-bearing: a derived variable may be a formula over a custom field or a
    # probe value, so both must already be in `out` before the formulas run.
    _resolve_fields(db_path, machine, out, fields)
    _resolve_probes(db_path, machine, out, now)
    _resolve_derived(db_path, out, {**field_variables(db_path), **probe_variables(db_path),
                                    **derived_variables(db_path)})
    return out


def _resolve_remote(db_path, machine, out, now):
    try:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT sessions_json, reported_at FROM remote_inventory WHERE machine = ?",
                (machine,),
            ).fetchone()
    except sqlite3.Error:
        row = None
    sessions, age = [], None
    if row:
        age = _age(now, row["reported_at"])
        try:
            sessions = json.loads(row["sessions_json"]) or []
        except (TypeError, ValueError):
            sessions = []
    if not isinstance(sessions, list):
        sessions = []

    picked = _pick_session(sessions)
    signed_in = [s for s in sessions
                 if isinstance(s, dict) and not s.get("is_logon_screen")
                 and str(s.get("user") or "").strip()]
    have = row is not None
    _put(out, STATIC_BY_NAME["session.count"],
         _num(len(signed_in)) if have else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["session.user"],
         _text((picked or {}).get("user")) if have else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["session.account"],
         _text((picked or {}).get("account")) if have else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["session.domain"],
         _text((picked or {}).get("domain")) if have else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["session.console_active"],
         (bool(picked and picked.get("is_console")) if have else UNKNOWN), age)
    _put(out, STATIC_BY_NAME["session.at_logon_screen"],
         (bool(sessions and not signed_in) if have else UNKNOWN), age)


def _resolve_network(db_path, machine, out, now):
    try:
        with get_conn(db_path) as conn:
            nic = conn.execute(
                # The machine's primary NIC: a link-up wired adapter with an address, else
                # any adapter with an address. Same "busiest/most real one wins" instinct as
                # _network_throughput -- a rule about "the machine's IP" means the one it is
                # actually reachable on, not whichever virtual adapter sorts first.
                "SELECT mac, ipv4, kind, link_up, wake_enabled, reported_at "
                "FROM machine_nics WHERE machine = ? "
                "ORDER BY (ipv4 IS NOT NULL AND ipv4 != '') DESC, link_up DESC, "
                "         (kind = 'ethernet') DESC, mac ASC LIMIT 1",
                (machine,),
            ).fetchone()
            net = conn.execute(
                "SELECT fast_startup, reported_at FROM machine_network WHERE machine = ?",
                (machine,),
            ).fetchone()
    except sqlite3.Error:
        nic, net = None, None

    nic_age = _age(now, nic["reported_at"]) if nic else None
    _put(out, STATIC_BY_NAME["net.ipv4"], _text(nic["ipv4"]) if nic else UNKNOWN, nic_age)
    _put(out, STATIC_BY_NAME["net.mac"], _text(nic["mac"]) if nic else UNKNOWN, nic_age)
    _put(out, STATIC_BY_NAME["net.link_up"], _bool(nic["link_up"]) if nic else UNKNOWN, nic_age)
    _put(out, STATIC_BY_NAME["net.wake_enabled"],
         _bool(nic["wake_enabled"]) if nic else UNKNOWN, nic_age)
    net_age = _age(now, net["reported_at"]) if net else None
    _put(out, STATIC_BY_NAME["net.fast_startup"],
         _bool(net["fast_startup"]) if net else UNKNOWN, net_age)


def _resolve_bios(db_path, machine, out, now):
    try:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT support, vendor, bios_version, password_set, reported_at "
                "FROM machine_bios WHERE machine = ?", (machine,),
            ).fetchone()
    except sqlite3.Error:
        row = None
    age = _age(now, row["reported_at"]) if row else None
    _put(out, STATIC_BY_NAME["bios.version"],
         _text(row["bios_version"]) if row else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["bios.vendor"], _text(row["vendor"]) if row else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["bios.support"], _text(row["support"]) if row else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["bios.password_set"],
         _bool(row["password_set"]) if row else UNKNOWN, age)


def _resolve_processes(db_path, machine, out, now):
    try:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT payload_json, captured_at, reported_at "
                "FROM machine_processes WHERE machine = ?", (machine,),
            ).fetchone()
    except sqlite3.Error:
        row = None
    payload, age = {}, None
    if row:
        age = _age(now, row["captured_at"] or row["reported_at"])
        try:
            payload = json.loads(row["payload_json"]) or {}
        except (TypeError, ValueError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    processes = payload.get("processes")
    _put(out, STATIC_BY_NAME["proc.count"],
         _num(len(processes)) if isinstance(processes, list) else UNKNOWN, age)
    _put(out, STATIC_BY_NAME["proc.cpu_cores"], _num(payload.get("cpu_cores")), age)
    _put(out, STATIC_BY_NAME["proc.mem_total_mb"], _num(payload.get("mem_total_mb")), age)


def _resolve_fields(db_path, machine, out, fields=None):
    """field.* -- operator-set, so never stale (age None).

    A field with a DEFAULT resolves to that default on a machine with no value, rather than
    UNKNOWN. That is what makes a default worth having: "every PC is Branch 1 unless I say
    otherwise" should be one field with a default, not forty writes.
    """
    definitions = list_fields(db_path)
    if fields is None:
        fields = get_machine_fields(db_path, machine) if definitions else {}
    for field in definitions:
        var = Var(f"field.{field['name']}", field["kind"], GROUP_FIELD, None)
        raw = fields.get(field["name"])
        if raw is None or str(raw).strip() == "":
            raw = field.get("default_value")
        _, coerced = coerce_field_value(field["kind"], raw, field["choices"])
        _put(out, var, UNKNOWN if coerced is None else coerced)


def catalog(db_path, disks=None):
    """Every variable name a rule may reference, for the condition builder's picker.

    `disks` (a machine's diagnostics["disks"]) expands the per-volume entries. Without it
    the catalog carries only the disk aggregates -- correct for a fleet-wide picker, since
    which drive letters exist is a per-machine fact and offering `disk.q.used_pct` because
    one machine has a Q: would be noise on the other four hundred.
    """
    entries = list(STATIC_VARIABLES) + list(disk_variables(disks))
    for field in list_fields(db_path):
        entries.append(Var(f"field.{field['name']}", field["kind"], GROUP_FIELD, None))
    entries.extend(probe_variables(db_path).values())
    entries.extend(derived_variables(db_path).values())
    return entries


def all_extra_variables(db_path):
    """Every dynamically-defined variable, as lookup_variable's `extra`. One function so no
    caller can accidentally validate a condition against a namespace missing a third of
    itself."""
    return {**field_variables(db_path), **probe_variables(db_path),
            **derived_variables(db_path)}


# ---------------------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------------------

# {{sys.machine}}. Double braces because packages.py already owns single-brace {name} for
# step substitution and its regex rejects dots -- two substitution syntaxes in one product is
# already one too many, so at least make them impossible to confuse.
_TEMPLATE_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_.]{0,63})\s*\}\}")
UNKNOWN_PLACEHOLDER = "unknown"


def format_value(value):
    """One resolved value as it should appear in a message to a human.

    Numbers lose their trailing .0 (nobody wants "up for 8.0 days") and are capped at one
    decimal; booleans read yes/no rather than True/False, because this text is going onto an
    end user's screen, not into a log.
    """
    if value is UNKNOWN or value is None:
        return UNKNOWN_PLACEHOLDER
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.1f}"
    return str(value)


def render_template(text, variables):
    """Substitute {{name}} placeholders from a resolved variable map.

    An unknown or unresolvable name renders as "unknown" rather than being left as literal
    braces. A message that reaches a user reading "Your PC has been up for {{sys.uptime_days}}
    days" is worse than one reading "unknown days" -- the first looks broken, the second
    looks like missing data, and only one of those is true.
    """
    if not text:
        return ""

    def replace(match):
        name = match.group(1)
        entry = (variables or {}).get(name)
        if entry is None:
            return UNKNOWN_PLACEHOLDER
        value = entry.value if isinstance(entry, Value) else entry
        return format_value(value)

    return _TEMPLATE_RE.sub(replace, str(text))


def template_names(text):
    """Every variable a template references -- so the editor can warn about a typo'd name
    before the message goes to four hundred desktops."""
    return sorted({m.group(1) for m in _TEMPLATE_RE.finditer(str(text or ""))})


# ---------------------------------------------------------------------------------------
# Variable lookup
# ---------------------------------------------------------------------------------------

_DISK_VAR_RE = re.compile(r"^disk\.([a-z])\.(used_pct|free_gb|used_gb|total_gb)$")
_DISK_SUFFIX_KINDS = dict(DISK_VOLUME_SUFFIXES)
_DISK_SUFFIX_UNITS = {"used_pct": "percent", "free_gb": "gb", "used_gb": "gb", "total_gb": "gb"}


def lookup_variable(name, extra=None):
    """The Var for a name, or None if no such variable can exist.

    Handles the two dynamic families structurally rather than by enumeration:
    `disk.<letter>.<suffix>` is valid for ANY drive letter, because which letters a machine
    has is a per-machine fact and a rule written against `disk.d.free_gb` must be storable
    before the machine with a D: drive has reported in. `field.<name>` comes from `extra`,
    which the caller builds once per request from the field definitions.
    """
    name = str(name or "")
    static = STATIC_BY_NAME.get(name)
    if static:
        return static
    if extra and name in extra:
        return extra[name]
    match = _DISK_VAR_RE.match(name)
    if match:
        suffix = match.group(2)
        return Var(name, _DISK_SUFFIX_KINDS[suffix], GROUP_DISK, AGE_LIVE,
                   _DISK_SUFFIX_UNITS[suffix])
    return None


def field_variables(db_path):
    """{name: Var} for every custom field, the `extra` argument lookup_variable wants."""
    return {f"field.{f['name']}": Var(f"field.{f['name']}", f["kind"], GROUP_FIELD, None)
            for f in list_fields(db_path)}


# ---------------------------------------------------------------------------------------
# Conditions: comparison operators
# ---------------------------------------------------------------------------------------

CMP_GT = ">"
CMP_GTE = ">="
CMP_LT = "<"
CMP_LTE = "<="
CMP_EQ = "=="
CMP_NE = "!="
CMP_CONTAINS = "contains"
CMP_NOT_CONTAINS = "not_contains"
CMP_STARTS_WITH = "starts_with"
CMP_ENDS_WITH = "ends_with"
CMP_MATCHES = "matches"
CMP_IN = "in"
CMP_NOT_IN = "not_in"
CMP_IS_KNOWN = "is_known"
CMP_IS_UNKNOWN = "is_unknown"

# Which operators each value kind offers. This is what the builder's operator dropdown reads,
# and what validation enforces -- `>` on a bool is not a typo worth guessing at, it is a
# question with no meaning, and offering it would invite rules whose author expected
# something Python's True > False happens to answer.
_ORDERED = (CMP_GT, CMP_GTE, CMP_LT, CMP_LTE)
_EQUALITY = (CMP_EQ, CMP_NE)
_TEXTUAL = (CMP_CONTAINS, CMP_NOT_CONTAINS, CMP_STARTS_WITH, CMP_ENDS_WITH, CMP_MATCHES)
_MEMBERSHIP = (CMP_IN, CMP_NOT_IN)
_PRESENCE = (CMP_IS_KNOWN, CMP_IS_UNKNOWN)

OPERATORS_BY_KIND = {
    KIND_NUMBER: _ORDERED + _EQUALITY + _MEMBERSHIP + _PRESENCE,
    KIND_TEXT: _EQUALITY + _TEXTUAL + _MEMBERSHIP + _PRESENCE,
    KIND_CHOICE: _EQUALITY + _MEMBERSHIP + _PRESENCE,
    KIND_BOOL: _EQUALITY + _PRESENCE,
}
ALL_OPERATORS = tuple(sorted({op for ops in OPERATORS_BY_KIND.values() for op in ops}))
# Operators that take no right-hand side at all.
NULLARY_OPERATORS = frozenset(_PRESENCE)
# ...and the ones whose right-hand side is a list rather than a scalar.
LIST_OPERATORS = frozenset(_MEMBERSHIP)

# i18n key root for operator labels: rules.operator.<op>.label
OPERATOR_TEXT_KEY = "rules.operator"

MAX_REGEX_CHARS = 200
MAX_CONDITION_NODES = 60
MAX_CONDITION_DEPTH = 8
MAX_LIST_ITEMS = 100

# A crude but effective guard against catastrophic backtracking: a quantified group whose
# body is itself quantified, i.e. the (a+)+ family. Not a proof of safety -- nothing short of
# a different regex engine is -- but it rejects the shapes that actually blow up, and the
# inputs are bounded (text variables are short, field values capped at 512 chars), which
# bounds the damage of whatever slips past.
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[*+}][^()]*\)\s*[*+{]")


def validate_regex(pattern):
    if len(pattern) > MAX_REGEX_CHARS:
        return f"pattern must be at most {MAX_REGEX_CHARS} characters"
    if _NESTED_QUANTIFIER_RE.search(pattern):
        return "pattern has nested repetition, which can hang the evaluator"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"invalid pattern: {exc}"
    return None


# ---------------------------------------------------------------------------------------
# Conditions: evaluation
# ---------------------------------------------------------------------------------------


def _compare(cmp_op, left, right):
    """One leaf comparison against a RESOLVED value. Returns True/False/UNKNOWN.

    The presence operators are handled by the caller, because they are the only two that
    must see an UNKNOWN left-hand side rather than short-circuiting on it.
    """
    if cmp_op in _ORDERED:
        if not isinstance(left, (int, float)) or isinstance(left, bool):
            return UNKNOWN
        if not isinstance(right, (int, float)) or isinstance(right, bool):
            return UNKNOWN
        if cmp_op == CMP_GT:
            return left > right
        if cmp_op == CMP_GTE:
            return left >= right
        if cmp_op == CMP_LT:
            return left < right
        return left <= right

    if cmp_op in _EQUALITY:
        equal = _loose_equal(left, right)
        if equal is UNKNOWN:
            return UNKNOWN
        return equal if cmp_op == CMP_EQ else not equal

    if cmp_op in _MEMBERSHIP:
        if not isinstance(right, (list, tuple)):
            return UNKNOWN
        hit = False
        for item in right:
            equal = _loose_equal(left, item)
            if equal is UNKNOWN:
                continue
            if equal:
                hit = True
                break
        return hit if cmp_op == CMP_IN else not hit

    # Textual operators. A non-text left-hand side is stringified rather than refused: a rule
    # reading `hw.serial_number starts_with "5CG"` should still work on a serial that happens
    # to be all digits and arrived as a number.
    haystack = "" if left is None else str(left)
    needle = "" if right is None else str(right)
    if cmp_op == CMP_CONTAINS:
        return needle.lower() in haystack.lower()
    if cmp_op == CMP_NOT_CONTAINS:
        return needle.lower() not in haystack.lower()
    if cmp_op == CMP_STARTS_WITH:
        return haystack.lower().startswith(needle.lower())
    if cmp_op == CMP_ENDS_WITH:
        return haystack.lower().endswith(needle.lower())
    if cmp_op == CMP_MATCHES:
        try:
            return re.search(needle, haystack, re.IGNORECASE) is not None
        except re.error:
            # Validation rejects a bad pattern at save time; a stored rule that somehow has
            # one reads UNKNOWN rather than raising, so one malformed rule cannot take the
            # whole evaluation tick down with it.
            return UNKNOWN
    return UNKNOWN


def _loose_equal(left, right):
    """Equality that does the obvious thing across the JSON/SQLite type boundary.

    Text comparison is case-insensitive because every string variable here is a Windows
    identifier -- hostnames, domains, usernames, OU names -- and Windows does not consider
    case meaningful in any of them. A rule reading `ad.ou == "OU=Sales"` must not miss the
    machine whose DN came back capitalised differently by the directory.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        left_bool = _bool(left)
        right_bool = _bool(right)
        if left_bool is UNKNOWN or right_bool is UNKNOWN:
            return UNKNOWN
        return left_bool == right_bool
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        # One side numeric, the other text: try the text as a number before giving up, so a
        # choice field storing "12" compares sanely against 12.
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return UNKNOWN
    return str(left).strip().lower() == str(right).strip().lower()


def evaluate(node, variables):
    """Three-valued evaluation of a condition AST. Returns True, False, or UNKNOWN.

    `and`/`or` are Kleene, which is the part worth being careful about:

      * `and` is FALSE as soon as any branch is FALSE, even if another is UNKNOWN -- a
        definitely-false conjunct settles it, so `cpu_load > 90 and bios.vendor == "Dell"`
        on a quiet machine with no BIOS block is FALSE, not UNKNOWN.
      * `or` is TRUE as soon as any branch is TRUE, for the mirror-image reason.
      * Otherwise a single UNKNOWN branch makes the whole thing UNKNOWN, and a rule fires
        only on TRUE.

    This is what stops "disk nearly full OR temp high" from firing on a machine that has
    reported neither.
    """
    if not isinstance(node, dict):
        return UNKNOWN

    op = node.get("op")
    if op in ("and", "or"):
        nodes = node.get("nodes") or []
        if not nodes:
            # An empty group is vacuously true for `and` and vacuously false for `or`, but a
            # rule whose condition is an empty group is almost certainly half-written -- so
            # it reads UNKNOWN and does not fire. Validation rejects it at save time anyway;
            # this is the belt to that braces.
            return UNKNOWN
        results = [evaluate(child, variables) for child in nodes]
        if op == "and":
            if any(r is False for r in results):
                return False
            return UNKNOWN if any(r is UNKNOWN for r in results) else True
        if any(r is True for r in results):
            return True
        return UNKNOWN if any(r is UNKNOWN for r in results) else False

    if op == "not":
        inner = node.get("nodes") or []
        if len(inner) != 1:
            return UNKNOWN
        result = evaluate(inner[0], variables)
        if result is UNKNOWN:
            return UNKNOWN
        return not result

    name = node.get("var")
    cmp_op = node.get("cmp")
    if not name or cmp_op not in ALL_OPERATORS:
        return UNKNOWN

    entry = (variables or {}).get(name)
    left = UNKNOWN
    if entry is not None:
        left = entry.value if isinstance(entry, Value) else entry

    # The presence operators are the only ones that answer definitely about an absent value.
    # They are the author's escape hatch: "fire when we have not heard a temperature" is a
    # legitimate rule, and without these it would be inexpressible.
    if cmp_op == CMP_IS_KNOWN:
        return left is not UNKNOWN
    if cmp_op == CMP_IS_UNKNOWN:
        return left is UNKNOWN

    if left is UNKNOWN:
        return UNKNOWN
    return _compare(cmp_op, left, node.get("value"))


def explain(node, variables):
    """The AST annotated with each leaf's resolved value and result -- what the preview
    endpoint returns so an operator can see WHY a machine did or did not match, rather than
    being handed a bare false and left to guess which clause was responsible."""
    if not isinstance(node, dict):
        return {"result": "unknown"}
    op = node.get("op")
    if op in ("and", "or", "not"):
        return {"op": op,
                "nodes": [explain(child, variables) for child in (node.get("nodes") or [])],
                "result": _result_name(evaluate(node, variables))}
    name = node.get("var")
    entry = (variables or {}).get(name)
    value = UNKNOWN
    age = None
    if entry is not None:
        value = entry.value if isinstance(entry, Value) else entry
        age = entry.age_seconds if isinstance(entry, Value) else None
    return {"var": name, "cmp": node.get("cmp"), "value": node.get("value"),
            "actual": None if value is UNKNOWN else value,
            "known": value is not UNKNOWN, "age_seconds": age,
            "result": _result_name(evaluate(node, variables))}


def _result_name(result):
    if result is UNKNOWN:
        return "unknown"
    return "true" if result else "false"


# ---------------------------------------------------------------------------------------
# Conditions: validation
# ---------------------------------------------------------------------------------------


def validate_condition(node, extra=None, _depth=0, _counter=None):
    """Check an AST is well-formed, references real variables, and uses operators those
    variables support. Returns (error, normalised_node).

    Normalisation matters as much as validation: literals are coerced to the variable's kind
    HERE, once, at save time. That is what lets the evaluator stay free of type juggling and
    what makes `field.headcount > "5"` from a JSON body behave identically to `> 5`.
    """
    if _counter is None:
        _counter = [0]
    _counter[0] += 1
    if _counter[0] > MAX_CONDITION_NODES:
        return f"condition has too many clauses (limit {MAX_CONDITION_NODES})", None
    if _depth > MAX_CONDITION_DEPTH:
        return f"condition is nested too deeply (limit {MAX_CONDITION_DEPTH})", None
    if not isinstance(node, dict):
        return "condition must be an object", None

    op = node.get("op")
    if op in ("and", "or", "not"):
        nodes = node.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            return f"'{op}' needs at least one clause", None
        if op == "not" and len(nodes) != 1:
            return "'not' takes exactly one clause", None
        normalised = []
        for child in nodes:
            err, child_node = validate_condition(child, extra, _depth + 1, _counter)
            if err:
                return err, None
            normalised.append(child_node)
        return None, {"op": op, "nodes": normalised}

    name = str(node.get("var") or "")
    var = lookup_variable(name, extra)
    if var is None:
        return f"unknown variable: {name or '(missing)'}", None
    cmp_op = node.get("cmp")
    allowed = OPERATORS_BY_KIND.get(var.kind, ())
    if cmp_op not in allowed:
        return (f"operator '{cmp_op}' cannot be used with {name} "
                f"({var.kind}); allowed: {', '.join(allowed)}"), None

    if cmp_op in NULLARY_OPERATORS:
        return None, {"var": name, "cmp": cmp_op}

    raw = node.get("value")
    if cmp_op in LIST_OPERATORS:
        if not isinstance(raw, (list, tuple)):
            return f"operator '{cmp_op}' needs a list of values", None
        if not raw:
            return f"operator '{cmp_op}' needs at least one value", None
        if len(raw) > MAX_LIST_ITEMS:
            return f"too many values (limit {MAX_LIST_ITEMS})", None
        items = []
        for item in raw:
            err, coerced = _coerce_literal(var, item)
            if err:
                return err, None
            items.append(coerced)
        return None, {"var": name, "cmp": cmp_op, "value": items}

    if cmp_op == CMP_MATCHES:
        pattern = str(raw or "")
        err = validate_regex(pattern)
        if err:
            return err, None
        return None, {"var": name, "cmp": cmp_op, "value": pattern}

    err, coerced = _coerce_literal(var, raw)
    if err:
        return err, None
    return None, {"var": name, "cmp": cmp_op, "value": coerced}


def _coerce_literal(var, raw):
    """A right-hand-side literal, coerced to the variable's kind. Returns (error, value)."""
    if raw is None:
        return "a value is required", None
    if var.kind == KIND_NUMBER:
        if isinstance(raw, bool):
            return "value must be a number", None
        if isinstance(raw, (int, float)):
            number = float(raw)
        else:
            text = str(raw).strip()
            seconds = parse_duration(text)
            if seconds is not None:
                number = _duration_in_unit(seconds, var.unit)
                if number is None:
                    return (f"{var.name} is not a duration, so '{text}' cannot be "
                            "compared against it"), None
                return None, _tidy_number(number)
            try:
                number = float(text)
            except ValueError:
                return f"value must be a number: {raw!r}", None
        if number != number or number in (float("inf"), float("-inf")):
            return "value must be a finite number", None
        return None, _tidy_number(number)
    if var.kind == KIND_BOOL:
        coerced = _bool(raw)
        if coerced is UNKNOWN:
            return "value must be true or false", None
        return None, coerced
    text = str(raw).strip()
    if len(text) > MAX_FIELD_VALUE_CHARS:
        return "value is too long", None
    return None, text


def _tidy_number(number):
    return int(number) if float(number).is_integer() else float(number)


def _duration_in_unit(seconds, unit):
    if unit == UNIT_SECONDS:
        return seconds
    if unit == UNIT_DAYS:
        return seconds / 86400.0
    return None


# ---------------------------------------------------------------------------------------
# Conditions: the text expression language
# ---------------------------------------------------------------------------------------
#
# Builder rows and typed text are two views of ONE AST -- parse_expression turns text into
# it, format_expression turns it back, and the evaluator never knows which the operator used.
#
# The parser is a hand-written tokeniser plus recursive descent. It is deliberately NOT
# eval(), ast.literal_eval() with a transform, or anything else that touches the Python
# compiler: this text arrives over HTTP from a console session, and the hub process it runs
# in is the one that can command every PC in the fleet. A hundred lines of tokeniser is a
# small price for a language that can only ever describe a comparison.

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|m|h|d|w)$", re.IGNORECASE)
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

MAX_EXPRESSION_CHARS = 4000


def parse_duration(text):
    """'7d' -> 604800. Returns None if it is not a duration literal.

    Exists because the motivating rule is "up for more than 7 days" and making an operator
    type 604800 is the kind of friction that gets a feature quietly abandoned.
    """
    match = _DURATION_RE.match(str(text or "").strip())
    if not match:
        return None
    return float(match.group(1)) * _DURATION_SECONDS[match.group(2).lower()]


_TOKEN_RE = re.compile(r"""
      (?P<ws>\s+)
    | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<duration>\d+(?:\.\d+)?\s*(?:s|m|h|d|w)\b)
    | (?P<number>-?\d+(?:\.\d+)?)
    | (?P<name>[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)
    | (?P<op>>=|<=|!=|==|=|>|<)
    | (?P<punct>[()\[\],])
""", re.VERBOSE | re.IGNORECASE)

_KEYWORDS = {"and", "or", "not", "true", "false", "is", "known", "unknown",
             "contains", "starts_with", "ends_with", "matches", "in", "with", "starts", "ends"}

Token = namedtuple("Token", "kind value pos")


class ExpressionError(ValueError):
    """A parse error with the character offset, so the editor can point at it."""

    def __init__(self, message, pos=None):
        super().__init__(message)
        self.pos = pos


def _tokenize(text):
    tokens, pos = [], 0
    length = len(text)
    while pos < length:
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise ExpressionError(f"unexpected character {text[pos]!r}", pos)
        kind = match.lastgroup
        value = match.group()
        pos = match.end()
        if kind == "ws":
            continue
        if kind == "name" and value.lower() in _KEYWORDS:
            kind = "keyword"
            value = value.lower()
        tokens.append(Token(kind, value, match.start()))
    return tokens


class _Parser:
    def __init__(self, tokens, extra):
        self.tokens = tokens
        self.pos = 0
        self.extra = extra

    def peek(self, offset=0):
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def next(self):
        token = self.peek()
        if token is None:
            raise ExpressionError("unexpected end of expression")
        self.pos += 1
        return token

    def accept_keyword(self, *words):
        token = self.peek()
        if token and token.kind == "keyword" and token.value in words:
            self.pos += 1
            return token.value
        return None

    def expect_punct(self, char):
        token = self.peek()
        if not token or token.kind != "punct" or token.value != char:
            raise ExpressionError(f"expected {char!r}", token.pos if token else None)
        self.pos += 1

    # -- grammar ------------------------------------------------------------------------
    # expression := or_expr
    # or_expr    := and_expr ('or' and_expr)*
    # and_expr   := not_expr ('and' not_expr)*
    # not_expr   := 'not' not_expr | primary
    # primary    := '(' expression ')' | comparison
    # comparison := NAME operator operand

    def parse(self):
        node = self.parse_or()
        leftover = self.peek()
        if leftover is not None:
            raise ExpressionError(f"unexpected {leftover.value!r}", leftover.pos)
        return node

    def parse_or(self):
        nodes = [self.parse_and()]
        while self.accept_keyword("or"):
            nodes.append(self.parse_and())
        return nodes[0] if len(nodes) == 1 else {"op": "or", "nodes": nodes}

    def parse_and(self):
        nodes = [self.parse_not()]
        while self.accept_keyword("and"):
            nodes.append(self.parse_not())
        return nodes[0] if len(nodes) == 1 else {"op": "and", "nodes": nodes}

    def parse_not(self):
        # `not` here is the prefix connective. `not contains` / `not in` are handled inside
        # parse_comparison, which is why this checks what FOLLOWS before claiming the token.
        token = self.peek()
        if token and token.kind == "keyword" and token.value == "not":
            following = self.peek(1)
            if not (following and following.kind == "keyword"
                    and following.value in ("contains", "in")):
                self.pos += 1
                return {"op": "not", "nodes": [self.parse_not()]}
        return self.parse_primary()

    def parse_primary(self):
        token = self.peek()
        if token and token.kind == "punct" and token.value == "(":
            self.pos += 1
            node = self.parse_or()
            self.expect_punct(")")
            return node
        return self.parse_comparison()

    def parse_comparison(self):
        token = self.next()
        if token.kind != "name":
            raise ExpressionError(f"expected a variable name, got {token.value!r}", token.pos)
        name = token.value
        var = lookup_variable(name, self.extra)
        if var is None:
            raise ExpressionError(f"unknown variable: {name}", token.pos)

        cmp_op, negate_membership = self._parse_operator()

        if cmp_op in NULLARY_OPERATORS:
            return {"var": name, "cmp": cmp_op}
        if cmp_op in LIST_OPERATORS:
            values = self._parse_list()
            return {"var": name, "cmp": CMP_NOT_IN if negate_membership else CMP_IN,
                    "value": values}
        return {"var": name, "cmp": cmp_op, "value": self._parse_operand()}

    def _parse_operator(self):
        """Returns (operator, negate_membership). Accepts both the underscored spellings
        (`starts_with`) and the natural two-word ones (`starts with`), because operators
        typed by hand should read like the sentence the author has in their head."""
        token = self.peek()
        if token is None:
            raise ExpressionError("expected an operator")

        if token.kind == "op":
            self.pos += 1
            return (CMP_EQ if token.value == "=" else token.value), False

        if token.kind == "keyword":
            word = token.value
            if word == "is":
                self.pos += 1
                negated = self.accept_keyword("not") is not None
                which = self.accept_keyword("known", "unknown")
                if which is None:
                    raise ExpressionError("expected 'known' or 'unknown' after 'is'", token.pos)
                known = (which == "known")
                if negated:
                    known = not known
                return (CMP_IS_KNOWN if known else CMP_IS_UNKNOWN), False
            if word == "not":
                self.pos += 1
                nxt = self.accept_keyword("contains", "in")
                if nxt == "contains":
                    return CMP_NOT_CONTAINS, False
                if nxt == "in":
                    return CMP_IN, True
                raise ExpressionError("expected 'contains' or 'in' after 'not'", token.pos)
            if word in ("contains", "matches", "in"):
                self.pos += 1
                return {"contains": CMP_CONTAINS, "matches": CMP_MATCHES,
                        "in": CMP_IN}[word], False
            if word in ("starts_with", "ends_with"):
                self.pos += 1
                return word, False
            if word in ("starts", "ends"):
                self.pos += 1
                if self.accept_keyword("with") is None:
                    raise ExpressionError(f"expected 'with' after '{word}'", token.pos)
                return (CMP_STARTS_WITH if word == "starts" else CMP_ENDS_WITH), False

        raise ExpressionError(f"expected an operator, got {token.value!r}", token.pos)

    def _parse_list(self):
        self.expect_punct("[")
        values = []
        while True:
            token = self.peek()
            if token and token.kind == "punct" and token.value == "]":
                self.pos += 1
                break
            values.append(self._parse_operand())
            token = self.peek()
            if token and token.kind == "punct" and token.value == ",":
                self.pos += 1
                continue
            self.expect_punct("]")
            break
        if not values:
            raise ExpressionError("list needs at least one value")
        return values

    def _parse_operand(self):
        token = self.next()
        if token.kind == "string":
            return _unquote(token.value)
        if token.kind == "duration":
            # Kept as the literal text; _coerce_literal converts it against the variable's
            # unit during validation, which is the only place that knows what unit means.
            return token.value.replace(" ", "")
        if token.kind == "number":
            number = float(token.value)
            return int(number) if number.is_integer() else number
        if token.kind == "keyword" and token.value in ("true", "false"):
            return token.value == "true"
        if token.kind == "name":
            # A bare word as a value -- `sys.status == online`. Convenient, and unambiguous
            # because the right-hand side is never a variable reference in this language.
            return token.value
        raise ExpressionError(f"expected a value, got {token.value!r}", token.pos)


def _unquote(text):
    body = text[1:-1]
    return re.sub(r"\\(.)", r"\1", body)


def parse_expression(text, extra=None):
    """Text -> validated AST. Returns (error, ast).

    Parsing and validation are deliberately one call: a parse that succeeded but produced a
    condition with an operator the variable does not support is not a success, and making
    callers remember to run both would eventually mean one that forgot.
    """
    raw = str(text or "").strip()
    if not raw:
        return "expression is empty", None
    if len(raw) > MAX_EXPRESSION_CHARS:
        return f"expression must be at most {MAX_EXPRESSION_CHARS} characters", None
    try:
        tokens = _tokenize(raw)
        if not tokens:
            return "expression is empty", None
        node = _Parser(tokens, extra).parse()
    except ExpressionError as exc:
        if exc.pos is not None:
            return f"{exc} (at character {exc.pos + 1})", None
        return str(exc), None
    return validate_condition(node, extra)


_BARE_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")


def _format_operand(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value)
    text = str(value)
    if _BARE_WORD_RE.match(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_number(value):
    return str(int(value)) if float(value).is_integer() else str(value)


def format_expression(node, _depth=0):
    """AST -> text, round-tripping with parse_expression.

    Round-trip is semantic, not literal: `7d` written against a days-valued variable comes
    back as `7`, because validation already converted it to the variable's unit. Both parse
    to the same AST, which is the property that actually matters -- the two editors must
    never disagree about what a rule means.
    """
    if not isinstance(node, dict):
        return ""
    op = node.get("op")
    if op in ("and", "or"):
        parts = [format_expression(child, _depth + 1) for child in (node.get("nodes") or [])]
        parts = [p for p in parts if p]
        if not parts:
            return ""
        joined = f" {op} ".join(parts)
        return f"({joined})" if _depth and len(parts) > 1 else joined
    if op == "not":
        inner = node.get("nodes") or []
        return f"not ({format_expression(inner[0], _depth + 1)})" if inner else ""

    name = node.get("var")
    cmp_op = node.get("cmp")
    if cmp_op == CMP_IS_KNOWN:
        return f"{name} is known"
    if cmp_op == CMP_IS_UNKNOWN:
        return f"{name} is unknown"
    value = node.get("value")
    if cmp_op in LIST_OPERATORS:
        items = ", ".join(_format_operand(v) for v in (value or []))
        return f"{name} {'not in' if cmp_op == CMP_NOT_IN else 'in'} [{items}]"
    spelling = {CMP_NOT_CONTAINS: "not contains", CMP_STARTS_WITH: "starts with",
                CMP_ENDS_WITH: "ends with"}.get(cmp_op, cmp_op)
    return f"{name} {spelling} {_format_operand(value)}"


def condition_variables(node, _seen=None):
    """Every variable a condition references -- used to warn about a rule whose variables
    are all UNKNOWN on every targeted machine, which is a rule that will never fire."""
    if _seen is None:
        _seen = set()
    if not isinstance(node, dict):
        return sorted(_seen)
    if node.get("op") in ("and", "or", "not"):
        for child in node.get("nodes") or []:
            condition_variables(child, _seen)
    elif node.get("var"):
        _seen.add(node["var"])
    return sorted(_seen)


# ---------------------------------------------------------------------------------------
# Derived variables -- var.<name>
# ---------------------------------------------------------------------------------------
#
# A named arithmetic expression over other variables. The motivating case is the one the
# built-in catalog cannot express: "free space as a percentage of the disk" is
# (total - used) / total * 100, which is obvious to write once and miserable to re-type into
# every rule that wants it.
#
# The arithmetic parser is separate from the condition parser above and produces a NUMBER
# rather than a truth value. Sharing one grammar was tempting, but a condition and a formula
# have genuinely different shapes, and merging them would mean `a > b + 1` had to decide
# whether `>` binds looser than `+` in a language where both sides can be either.

GROUP_DERIVED = "var"
MAX_DERIVED_DEPTH = 8
MAX_ARITHMETIC_CHARS = 500

_ARITH_TOKEN_RE = re.compile(r"""
      (?P<ws>\s+)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<name>[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)
    | (?P<op>[-+*/()])
""", re.VERBOSE | re.IGNORECASE)


def parse_arithmetic(text, extra=None):
    """Parse a derived variable's formula. Returns (error, ast).

    Grammar: term (('+'|'-') term)*, term := factor (('*'|'/') factor)*, factor := number |
    variable | '(' expr ')' | '-' factor. No functions, no comparisons, no eval -- the same
    reasoning as the condition parser, and the same refusal to let operator text reach the
    Python compiler.
    """
    raw = str(text or "").strip()
    if not raw:
        return "expression is empty", None
    if len(raw) > MAX_ARITHMETIC_CHARS:
        return f"expression must be at most {MAX_ARITHMETIC_CHARS} characters", None

    tokens, pos = [], 0
    while pos < len(raw):
        match = _ARITH_TOKEN_RE.match(raw, pos)
        if not match:
            return f"unexpected character {raw[pos]!r} (at character {pos + 1})", None
        pos = match.end()
        if match.lastgroup == "ws":
            continue
        tokens.append((match.lastgroup, match.group(), match.start()))

    state = {"i": 0}

    def peek():
        return tokens[state["i"]] if state["i"] < len(tokens) else None

    def take():
        token = peek()
        if token is None:
            raise ExpressionError("unexpected end of expression")
        state["i"] += 1
        return token

    def parse_expr():
        node = parse_term()
        while (token := peek()) and token[0] == "op" and token[1] in "+-":
            take()
            node = {"op": token[1], "left": node, "right": parse_term()}
        return node

    def parse_term():
        node = parse_factor()
        while (token := peek()) and token[0] == "op" and token[1] in "*/":
            take()
            node = {"op": token[1], "left": node, "right": parse_factor()}
        return node

    def parse_factor():
        kind, value, offset = take()
        if kind == "op" and value == "(":
            node = parse_expr()
            token = peek()
            if not token or token[1] != ")":
                raise ExpressionError("expected ')'", offset)
            take()
            return node
        if kind == "op" and value == "-":
            return {"op": "-", "left": {"const": 0}, "right": parse_factor()}
        if kind == "number":
            number = float(value)
            return {"const": int(number) if number.is_integer() else number}
        if kind == "name":
            var = lookup_variable(value, extra)
            if var is None:
                raise ExpressionError(f"unknown variable: {value}", offset)
            if var.kind != KIND_NUMBER:
                raise ExpressionError(f"{value} is not a number, so it cannot be used in a "
                                      "calculation", offset)
            return {"var": value}
        raise ExpressionError(f"unexpected {value!r}", offset)

    try:
        node = parse_expr()
    except ExpressionError as exc:
        return (f"{exc} (at character {exc.pos + 1})" if exc.pos is not None else str(exc)), None
    if state["i"] != len(tokens):
        return f"unexpected {tokens[state['i']][1]!r}", None
    return None, node


def evaluate_arithmetic(node, variables):
    """Compute a derived value, or UNKNOWN.

    UNKNOWN propagates through every operator: a formula over a value we do not have has no
    answer, and returning 0 would be a lie that fires rules. Division by zero is UNKNOWN too,
    not an error -- a machine reporting a zero-byte disk is a machine we know nothing useful
    about, not a reason to fail the whole evaluation pass.
    """
    if not isinstance(node, dict):
        return UNKNOWN
    if "const" in node:
        return node["const"]
    if "var" in node:
        entry = (variables or {}).get(node["var"])
        if entry is None:
            return UNKNOWN
        value = entry.value if isinstance(entry, Value) else entry
        return _num(value) if value is not UNKNOWN else UNKNOWN

    left = evaluate_arithmetic(node.get("left"), variables)
    right = evaluate_arithmetic(node.get("right"), variables)
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    op = node.get("op")
    try:
        if op == "+":
            return _tidy_number(left + right)
        if op == "-":
            return _tidy_number(left - right)
        if op == "*":
            return _tidy_number(left * right)
        if op == "/":
            if not right:
                return UNKNOWN
            return _tidy_number(left / right)
    except (TypeError, ValueError, OverflowError):
        return UNKNOWN
    return UNKNOWN


def arithmetic_variables(node, _seen=None):
    if _seen is None:
        _seen = set()
    if not isinstance(node, dict):
        return _seen
    if "var" in node:
        _seen.add(node["var"])
    for side in ("left", "right"):
        if node.get(side) is not None:
            arithmetic_variables(node[side], _seen)
    return _seen


def list_derived(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM derived_vars ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def derived_variables(db_path):
    """{name: Var} for every derived variable, for lookup_variable's `extra`."""
    return {f"var.{d['name']}": Var(f"var.{d['name']}", KIND_NUMBER, GROUP_DERIVED, None,
                                    d.get("unit") or None)
            for d in list_derived(db_path)}


def save_derived(db_path, name, expression, description="", unit="", actor="", now=None):
    """Create or update a derived variable. Returns (error, row).

    Cycle detection runs at SAVE time, not at evaluation time. A cycle caught during a rule
    pass would be a per-machine, per-tick failure buried in a log; caught here it is an error
    message next to the field the operator just typed.
    """
    if not is_valid_name(name):
        return ("name must be lowercase letters, digits and underscores, "
                "starting with a letter, at most 32 characters"), None
    extra = {**field_variables(db_path), **derived_variables(db_path), **probe_variables(db_path)}
    # Its own name must resolve while parsing (a formula may not reference itself, but the
    # name has to exist for the dependency walk below to be able to say so).
    extra[f"var.{name}"] = Var(f"var.{name}", KIND_NUMBER, GROUP_DERIVED, None)
    err, ast = parse_arithmetic(expression, extra)
    if err:
        return err, None

    err = _check_derived_cycles(db_path, name, ast, extra)
    if err:
        return err, None

    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO derived_vars (name, expression, description, unit, created_at,
                                      created_by, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                expression = excluded.expression, description = excluded.description,
                unit = excluded.unit, updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (name, str(expression).strip(), str(description or "")[:500],
             str(unit or "")[:32], now, str(actor or ""), now, str(actor or "")),
        )
    return None, next((d for d in list_derived(db_path) if d["name"] == name), None)


def _check_derived_cycles(db_path, name, ast, extra):
    """Would adding `name` create a reference loop, or a chain too deep to be sensible?"""
    formulas = {d["name"]: d["expression"] for d in list_derived(db_path)}
    formulas[name] = None            # placeholder; its AST is the one being checked

    def deps_of(target, node=None):
        if target == name and node is not None:
            source = node
        else:
            expression = formulas.get(target)
            if expression is None:
                return set()
            _, parsed = parse_arithmetic(expression, extra)
            source = parsed
        if source is None:
            return set()
        return {v[4:] for v in arithmetic_variables(source) if v.startswith("var.")}

    def walk(target, seen, depth, root_ast=None):
        if depth > MAX_DERIVED_DEPTH:
            return f"'{name}' depends on other calculations more than {MAX_DERIVED_DEPTH} deep"
        for dep in deps_of(target, root_ast if target == name else None):
            if dep == name:
                return (f"'{name}' would end up depending on itself"
                        if target != name else f"'{name}' cannot reference itself")
            if dep in seen:
                continue
            if dep not in formulas:
                return f"unknown calculation: var.{dep}"
            error = walk(dep, seen | {dep}, depth + 1)
            if error:
                return error
        return None

    return walk(name, {name}, 0, ast)


def delete_derived(db_path, name):
    """Drop a derived variable, refusing if another one still depends on it.

    Refusing rather than cascading: the dependent formula would otherwise silently start
    reading UNKNOWN, and every rule using it would quietly stop firing -- which is the
    hardest possible failure to notice, because nothing errors.
    """
    extra = {**field_variables(db_path), **derived_variables(db_path), **probe_variables(db_path)}
    for other in list_derived(db_path):
        if other["name"] == name:
            continue
        _, ast = parse_arithmetic(other["expression"], extra)
        if ast is not None and f"var.{name}" in arithmetic_variables(ast):
            return f"var.{other['name']} still uses this calculation", False
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM derived_vars WHERE name = ?", (name,))
        return None, cur.rowcount > 0


def _resolve_derived(db_path, out, extra):
    """Add every var.* to a resolved variable map, in dependency order.

    Iterative rather than recursive: each pass computes whatever it now can, and repeats
    while anything changed. With cycles already refused at save time this converges in at
    most MAX_DERIVED_DEPTH passes, and anything still unresolved after that reads UNKNOWN --
    which is the right answer for a formula whose inputs the machine has not reported.
    """
    definitions = list_derived(db_path)
    if not definitions:
        return
    pending = {d["name"]: d for d in definitions}
    for name, definition in pending.items():
        out[f"var.{name}"] = Value(UNKNOWN, KIND_NUMBER, None)

    for _ in range(MAX_DERIVED_DEPTH + 1):
        changed = False
        for name, definition in pending.items():
            if out[f"var.{name}"].known:
                continue
            _, ast = parse_arithmetic(definition["expression"], extra)
            if ast is None:
                continue
            value = evaluate_arithmetic(ast, out)
            if value is not UNKNOWN:
                # Age is the OLDEST age among the inputs: a formula is exactly as fresh as
                # its stalest term, and reporting the newest would make a calculation over a
                # week-old disk reading look current.
                ages = [out[v].age_seconds for v in arithmetic_variables(ast)
                        if v in out and out[v].age_seconds is not None]
                out[f"var.{name}"] = Value(value, KIND_NUMBER, max(ages) if ages else None)
                changed = True
        if not changed:
            break


# ---------------------------------------------------------------------------------------
# Probes -- probe.<name>
# ---------------------------------------------------------------------------------------

PROBE_REGISTRY = "registry"
PROBE_FILE_EXISTS = "file_exists"
PROBE_FILE_VERSION = "file_version"
PROBE_WMI = "wmi"
PROBE_SCRIPT = "script"
PROBE_KINDS = (PROBE_REGISTRY, PROBE_FILE_EXISTS, PROBE_FILE_VERSION, PROBE_WMI, PROBE_SCRIPT)
PROBE_TEXT_KEY = "rules.probe"

# The four read-only kinds. `script` is excluded because it is not a read -- it is arbitrary
# code, on a schedule, on every PC in the fleet, and it is gated behind its own setting.
PROBE_SAFE_KINDS = (PROBE_REGISTRY, PROBE_FILE_EXISTS, PROBE_FILE_VERSION, PROBE_WMI)

MIN_PROBE_INTERVAL = 300
MAX_PROBE_INTERVAL = 7 * 86400
# How much older than its own interval a probe value may get before it reads UNKNOWN. Three
# intervals tolerates a machine being off for a couple of collection windows without a rule
# suddenly going blind, while still ageing out a machine that has genuinely stopped answering.
PROBE_STALENESS_FACTOR = 3


def list_probes(db_path, enabled_only=False):
    query = "SELECT * FROM probes"
    if enabled_only:
        query += " WHERE enabled = 1"
    with get_conn(db_path) as conn:
        rows = conn.execute(query + " ORDER BY name").fetchall()
    out = []
    for row in rows:
        probe = dict(row)
        try:
            probe["spec"] = json.loads(probe.pop("spec_json") or "{}")
        except (TypeError, ValueError):
            probe["spec"] = {}
        probe["enabled"] = bool(probe["enabled"])
        out.append(probe)
    return out


def probe_variables(db_path):
    return {f"probe.{p['name']}": Var(f"probe.{p['name']}", p["value_kind"], "probe",
                                      int(p["interval_seconds"]) * PROBE_STALENESS_FACTOR)
            for p in list_probes(db_path)}


def validate_probe(name, kind, spec, value_kind, interval_seconds, timeout_seconds,
                   allow_script=False):
    if not is_valid_name(name):
        return "name must be lowercase letters, digits and underscores", None
    if kind not in PROBE_KINDS:
        return f"kind must be one of {', '.join(PROBE_KINDS)}", None
    if kind == PROBE_SCRIPT and not allow_script:
        return ("script probes are switched off (rules.probes_allow_script). A script probe "
                "runs arbitrary code on every targeted PC on a schedule"), None
    if value_kind not in (KIND_TEXT, KIND_NUMBER, KIND_BOOL):
        return "value kind must be text, number or bool", None
    spec = spec or {}
    if not isinstance(spec, dict):
        return "probe spec must be an object", None

    clean = {}
    if kind == PROBE_REGISTRY:
        root = str(spec.get("root") or "").upper()
        if root not in ("HKLM", "HKCU", "HKCR", "HKU"):
            return "registry root must be HKLM, HKCU, HKCR or HKU", None
        path = str(spec.get("path") or "").strip()
        if not path or len(path) > 512:
            return "registry path is required", None
        clean = {"root": root, "path": path,
                 "value": str(spec.get("value") or "").strip()[:256]}
    elif kind in (PROBE_FILE_EXISTS, PROBE_FILE_VERSION):
        path = str(spec.get("path") or "").strip()
        if not path or len(path) > 512:
            return "file path is required", None
        clean = {"path": path}
    elif kind == PROBE_WMI:
        query = str(spec.get("query") or "").strip()
        if not query or len(query) > 512:
            return "a WMI query is required", None
        if not query.lower().startswith("select "):
            # Only SELECT. WMI can also INVOKE methods, and a probe is a read.
            return "a WMI probe must be a SELECT query", None
        clean = {"query": query, "property": str(spec.get("property") or "").strip()[:128]}
    else:
        script = str(spec.get("script") or "").strip()
        if not script:
            return "a script is required", None
        if len(script) > 4000:
            return "script is too long", None
        clean = {"script": script}

    try:
        interval = max(MIN_PROBE_INTERVAL, min(MAX_PROBE_INTERVAL, int(interval_seconds)))
        timeout = max(5, min(600, int(timeout_seconds)))
    except (TypeError, ValueError):
        return "interval and timeout must be numbers", None

    return None, {"name": name, "kind": kind, "spec": clean, "value_kind": value_kind,
                  "interval_seconds": interval, "timeout_seconds": timeout}


def save_probe(db_path, name, kind, spec, value_kind=KIND_TEXT, interval_seconds=3600,
               timeout_seconds=30, description="", enabled=True, allow_script=False,
               actor="", now=None):
    err, probe = validate_probe(name, kind, spec, value_kind, interval_seconds,
                                timeout_seconds, allow_script)
    if err:
        return err, None
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO probes (name, kind, spec_json, value_kind, interval_seconds,
                                timeout_seconds, enabled, description, created_at,
                                created_by, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                kind = excluded.kind, spec_json = excluded.spec_json,
                value_kind = excluded.value_kind,
                interval_seconds = excluded.interval_seconds,
                timeout_seconds = excluded.timeout_seconds, enabled = excluded.enabled,
                description = excluded.description, updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (probe["name"], probe["kind"], json.dumps(probe["spec"]), probe["value_kind"],
             probe["interval_seconds"], probe["timeout_seconds"], 1 if enabled else 0,
             str(description or "")[:500], now, str(actor or ""), now, str(actor or "")),
        )
    return None, next((p for p in list_probes(db_path) if p["name"] == name), None)


def delete_probe(db_path, name):
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_probe_values WHERE name = ?", (name,))
        cur = conn.execute("DELETE FROM probes WHERE name = ?", (name,))
        return cur.rowcount > 0


def record_probe_value(db_path, machine, name, value, error="", now=None):
    """Store one probe answer.

    A failed collection keeps the previous VALUE and records the error alongside it, rather
    than blanking it. The value ages out on its own through the staleness rule, so an
    intermittent failure degrades gradually instead of making every rule over that probe go
    unknown on the first hiccup.
    """
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        if error:
            conn.execute(
                """
                INSERT INTO machine_probe_values (machine, name, error, requested_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(machine, name) DO UPDATE SET
                    error = excluded.error, requested_at = excluded.requested_at
                """,
                (machine, name, str(error)[:500], now),
            )
        else:
            conn.execute(
                """
                INSERT INTO machine_probe_values (machine, name, value, collected_at,
                                                  requested_at, error)
                VALUES (?, ?, ?, ?, ?, '')
                ON CONFLICT(machine, name) DO UPDATE SET
                    value = excluded.value, collected_at = excluded.collected_at,
                    requested_at = excluded.requested_at, error = ''
                """,
                (machine, name, None if value is None else str(value)[:1000], now, now),
            )


def probes_due(db_path, machines, now=None):
    """[(machine, probe)] for every collection that is due.

    Due-ness is measured from `requested_at`, not `collected_at`, so a machine whose probe
    keeps failing is retried on the same cadence as one that succeeds rather than being
    hammered every tick.
    """
    now = int(now if now is not None else time.time())
    probes = list_probes(db_path, enabled_only=True)
    if not probes or not machines:
        return []
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT machine, name, requested_at FROM machine_probe_values").fetchall()
    last = {(r["machine"], r["name"]): r["requested_at"] for r in rows}
    due = []
    for machine in machines:
        for probe in probes:
            when = last.get((machine, probe["name"]))
            if when is None or now - int(when) >= int(probe["interval_seconds"]):
                due.append((machine, probe))
    return due


def _resolve_probes(db_path, machine, out, now):
    definitions = list_probes(db_path)
    if not definitions:
        return
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT name, value, collected_at FROM machine_probe_values WHERE machine = ?",
            (machine,),
        ).fetchall()
    stored = {r["name"]: r for r in rows}
    for probe in definitions:
        var = Var(f"probe.{probe['name']}", probe["value_kind"], "probe",
                  int(probe["interval_seconds"]) * PROBE_STALENESS_FACTOR)
        row = stored.get(probe["name"])
        if not row or row["collected_at"] is None:
            _put(out, var, UNKNOWN)
            continue
        _, coerced = coerce_field_value(probe["value_kind"], row["value"])
        _put(out, var, UNKNOWN if coerced is None else coerced,
             _age(now, row["collected_at"]))


# ---------------------------------------------------------------------------------------
# Targeting -- who a rule applies to
# ---------------------------------------------------------------------------------------
#
# A rule is authored ONCE against a set of machines: the whole fleet, some AD OUs, an
# explicit list, or any of those minus exclusions. There is no per-machine rule; the machine
# pages only ever show which rules currently apply to them.
#
# The selector shapes deliberately mirror permissions.py's access scopes (SCOPE_ALL,
# SCOPE_LIST, SCOPE_AD_OU) so targeting and permission scoping speak one vocabulary. That
# matters for more than tidiness: enforce_scope below has to intersect a rule's targets with
# its author's scope, and an intersection between two different vocabularies is where the
# bugs would live.

TARGET_ALL = "all"
TARGET_MACHINES = "machines"
TARGET_AD_OU = "ad_ou"
TARGET_FIELD = "field"
TARGET_KINDS = (TARGET_ALL, TARGET_MACHINES, TARGET_AD_OU, TARGET_FIELD)
TARGET_TEXT_KEY = "rules.target"

MAX_TARGET_SELECTORS = 25
MAX_TARGET_MACHINES = 2000


def validate_target(target, extra=None):
    """Check a target spec. Returns (error, normalised).

    A target with no `include` at all is refused rather than defaulted to "all". Defaulting
    would mean a mis-typed include key silently addressed every PC in the company, and the
    cost of being wrong in that direction is not symmetric with the cost of an error message.
    """
    if not isinstance(target, dict):
        return "target must be an object", None
    normalised = {"include": [], "exclude": []}
    for side in ("include", "exclude"):
        selectors = target.get(side) or []
        if not isinstance(selectors, list):
            return f"target {side} must be a list", None
        if len(selectors) > MAX_TARGET_SELECTORS:
            return f"too many {side} selectors (limit {MAX_TARGET_SELECTORS})", None
        for selector in selectors:
            err, clean = _validate_selector(selector, extra)
            if err:
                return err, None
            normalised[side].append(clean)
    if not normalised["include"]:
        return "a rule needs at least one include selector", None
    return None, normalised


def _validate_selector(selector, extra=None):
    if not isinstance(selector, dict):
        return "selector must be an object", None
    kind = selector.get("kind")
    if kind not in TARGET_KINDS:
        return f"selector kind must be one of {', '.join(TARGET_KINDS)}", None
    if kind == TARGET_ALL:
        return None, {"kind": TARGET_ALL}
    if kind == TARGET_MACHINES:
        machines = selector.get("machines") or []
        if not isinstance(machines, list) or not machines:
            return "a machines selector needs at least one machine", None
        if len(machines) > MAX_TARGET_MACHINES:
            return f"too many machines (limit {MAX_TARGET_MACHINES})", None
        names = []
        for machine in machines:
            name = str(machine or "").strip()
            if name and name not in names:
                names.append(name)
        if not names:
            return "a machines selector needs at least one machine", None
        return None, {"kind": TARGET_MACHINES, "machines": names}
    if kind == TARGET_AD_OU:
        ou = str(selector.get("ou") or "").strip()
        if not ou:
            return "an OU selector needs an OU", None
        if len(ou) > 512:
            return "OU is too long", None
        return None, {"kind": TARGET_AD_OU, "ou": ou,
                      "include_children": bool(selector.get("include_children", True))}
    # TARGET_FIELD
    name = str(selector.get("field") or "").strip()
    var = lookup_variable(f"field.{name}", extra)
    if var is None:
        return f"unknown field: {name or '(missing)'}", None
    err, value = coerce_field_value(var.kind, selector.get("value"))
    if err:
        return f"field {name}: {err}", None
    if value is None:
        return f"field {name}: a value is required", None
    return None, {"kind": TARGET_FIELD, "field": name, "value": value}


def _all_machines(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT machine, ad_ou, ad_dn FROM machine_info").fetchall()
    return [dict(r) for r in rows]


def _selector_matches(selector, machine_row, field_values):
    kind = selector.get("kind")
    if kind == TARGET_ALL:
        return True
    name = machine_row.get("machine")
    if kind == TARGET_MACHINES:
        wanted = {m.lower() for m in selector.get("machines", [])}
        return str(name or "").lower() in wanted
    if kind == TARGET_AD_OU:
        want = str(selector.get("ou") or "").strip().lower()
        # Match against the OU when we have one, and fall back to the DN -- a machine's DN
        # ENDS WITH its OU, which is also what makes the sub-OU case a suffix test rather
        # than a second query.
        have_ou = str(machine_row.get("ad_ou") or "").strip().lower()
        have_dn = str(machine_row.get("ad_dn") or "").strip().lower()
        if selector.get("include_children", True):
            return bool((have_ou and have_ou.endswith(want))
                        or (have_dn and have_dn.endswith(want)))
        return have_ou == want
    if kind == TARGET_FIELD:
        have = (field_values.get(name) or {}).get(selector.get("field"))
        if have is None:
            return False
        return _loose_equal(have, selector.get("value")) is True
    return False


def resolve_targets(db_path, target, machines=None):
    """The machine names a target spec currently addresses, sorted.

    Exclusions win over inclusions, which is what makes "this OU except that one PC" and
    "everything except these two" each a single rule rather than a rewrite of the include
    list every time somebody leaves.
    """
    rows = machines if machines is not None else _all_machines(db_path)
    include = (target or {}).get("include") or []
    exclude = (target or {}).get("exclude") or []

    needs_fields = any(s.get("kind") == TARGET_FIELD for s in include + exclude)
    field_values = _all_field_values(db_path) if needs_fields else {}

    selected = []
    for row in rows:
        name = row.get("machine")
        if not name:
            continue
        if not any(_selector_matches(s, row, field_values) for s in include):
            continue
        if any(_selector_matches(s, row, field_values) for s in exclude):
            continue
        selected.append(name)
    return sorted(selected)


def _all_field_values(db_path):
    """{machine: {field: coerced_value}} for every machine that has any field set.

    One query for the whole fleet rather than one per machine: the target resolver runs on
    every evaluation tick for every rule, and a per-machine read there would be the first
    thing to fall over on a fleet of any size.
    """
    definitions = {f["name"]: f for f in list_fields(db_path)}
    if not definitions:
        return {}
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT machine, name, value FROM machine_field_values").fetchall()
    out = {}
    for row in rows:
        field = definitions.get(row["name"])
        if not field:
            continue
        _, coerced = coerce_field_value(field["kind"], row["value"], field["choices"])
        if coerced is not None:
            out.setdefault(row["machine"], {})[row["name"]] = coerced
    # Defaults apply to every machine that has no explicit value -- same rule the resolver
    # uses, and it has to be the same or the target picker and the condition would disagree
    # about what `field.site` is on a machine nobody has touched.
    defaults = {name: f["default_value"] for name, f in definitions.items()
                if f.get("default_value") not in (None, "")}
    if defaults:
        for row in _all_machines(db_path):
            machine = row.get("machine")
            for name, raw in defaults.items():
                bucket = out.setdefault(machine, {})
                if name not in bucket:
                    _, coerced = coerce_field_value(definitions[name]["kind"], raw,
                                                    definitions[name]["choices"])
                    if coerced is not None:
                        bucket[name] = coerced
    return out


# ---------------------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------------------

ACTION_ALERT = "alert"
ACTION_COMMAND = "command"
ACTION_SHOW_MESSAGE = "show_message"
ACTION_SNOOZE = "snooze"
ACTION_WEBHOOK = "webhook"
ACTION_EMAIL = "email"
ACTION_TYPES = (ACTION_ALERT, ACTION_COMMAND, ACTION_SHOW_MESSAGE, ACTION_SNOOZE,
                ACTION_WEBHOOK, ACTION_EMAIL)
ACTION_TEXT_KEY = "rules.action"

# Actions that change a machine rather than telling somebody about it. These are the ones
# behind the command kill switch and the cooldown floor.
MUTATING_ACTIONS = frozenset({ACTION_COMMAND})

MAX_ACTIONS = 10
MAX_MESSAGE_CHARS = 2000
MAX_SNOOZE_SECONDS = 30 * 86400

# Command types a rule may NEVER issue, whatever the operator's permissions.
#
# Everything here carries params that only make sense for one live, one-shot context -- a
# session id, a change id, a relay's peer MAC, a snapshot of a deployment. A rule fires with
# params frozen when it was SAVED, so any of these would be replaying a pointer to something
# that has since finished or moved. This is the same reasoning that makes them
# non-favoritable in fleet.py, and the same list plus the process commands, whose PIDs
# Windows recycles within minutes.
RULE_FORBIDDEN_COMMANDS = (fleet.SESSION_CONTROL_COMMANDS | fleet.SCHEDULED_COMMANDS
                           | fleet.REMOTE_CONTROL_COMMANDS | fleet.UNSAVEABLE_FIRMWARE_COMMANDS
                           | fleet.UNSAVEABLE_WAKE_COMMANDS | fleet.PROCESS_COMMANDS
                           | fleet.PROBE_COMMANDS
                           | frozenset({"install_virtual_display", "rename"}))
RULE_ALLOWED_COMMANDS = frozenset(fleet.ALL_COMMANDS) - RULE_FORBIDDEN_COMMANDS


def validate_actions(actions, extra=None, allow_command=True, _nested=False):
    """Check an action list. Returns (error, normalised).

    `allow_command` is the author's ISSUE_COMMANDS capability, checked here rather than at
    the endpoint so that the on_response follow-ups of a show_message get the same treatment
    as a top-level action -- a user clicking "Yes" is still a rule issuing a reboot, and a
    check that only guarded the top level would be trivially sidestepped by nesting.
    """
    if not isinstance(actions, list):
        return "actions must be a list", None
    if not actions:
        return "a rule needs at least one action", None
    if len(actions) > MAX_ACTIONS:
        return f"too many actions (limit {MAX_ACTIONS})", None
    normalised = []
    for action in actions:
        err, clean = _validate_action(action, extra, allow_command, _nested)
        if err:
            return err, None
        normalised.append(clean)
    return None, normalised


def _validate_action(action, extra, allow_command, nested):
    if not isinstance(action, dict):
        return "action must be an object", None
    kind = action.get("type")
    if kind not in ACTION_TYPES:
        return f"action type must be one of {', '.join(ACTION_TYPES)}", None
    params = action.get("params") or {}
    if not isinstance(params, dict):
        return "action params must be an object", None

    if kind == ACTION_ALERT:
        text = str(params.get("text") or "").strip()
        if not text:
            return "an alert action needs text", None
        if len(text) > MAX_MESSAGE_CHARS:
            return "alert text is too long", None
        return None, {"type": kind, "params": {"text": text}}

    if kind == ACTION_SNOOZE:
        try:
            seconds = int(params.get("seconds"))
        except (TypeError, ValueError):
            return "a snooze action needs a number of seconds", None
        if not 60 <= seconds <= MAX_SNOOZE_SECONDS:
            return f"snooze must be between 60 and {MAX_SNOOZE_SECONDS} seconds", None
        return None, {"type": kind, "params": {"seconds": seconds}}

    if kind == ACTION_COMMAND:
        if not allow_command:
            return ("you do not have permission to create a rule that issues commands"), None
        command_type = str(params.get("command_type") or "")
        if command_type not in fleet.ALL_COMMANDS:
            return f"unknown command: {command_type or '(missing)'}", None
        if command_type not in RULE_ALLOWED_COMMANDS:
            return (f"'{command_type}' cannot be issued by a rule: its parameters name a "
                    "specific live session, change or deployment, which a saved rule "
                    "cannot know about"), None
        command_params = params.get("params") or {}
        if not isinstance(command_params, dict):
            return "command params must be an object", None
        try:
            encoded = json.dumps(command_params)
        except (TypeError, ValueError):
            return "command params must be JSON-serialisable", None
        if len(encoded) > 4000:
            return "command params are too large", None
        return None, {"type": kind, "params": {"command_type": command_type,
                                               "params": command_params}}

    if kind == ACTION_SHOW_MESSAGE:
        return _validate_show_message(action, extra, allow_command, nested)

    if kind == ACTION_WEBHOOK:
        return _validate_webhook(params)

    return _validate_email(params)


# ---- show_message: the interactive one ------------------------------------------------
#
# The dialog is a ROUND TRIP, not a notification: the agent reports which button the user
# pressed and the hub maps that outcome to follow-up actions. Note what the agent is NOT
# told -- what any button MEANS. It reports an outcome id and nothing else, so "what Later
# does" stays a hub policy decision that can be changed without shipping a new agent.

BUTTON_OK = "ok"
BUTTON_CANCEL = "cancel"
BUTTON_YES = "yes"
BUTTON_NO = "no"
BUTTON_LATER = "later"
BUTTON_ACCEPT = "accept"
BUTTON_DECLINE = "decline"
BUTTON_IDS = (BUTTON_OK, BUTTON_CANCEL, BUTTON_YES, BUTTON_NO, BUTTON_LATER,
              BUTTON_ACCEPT, BUTTON_DECLINE)

# Presets keep the common case one click, and keep the i18n catalog finite: a button set an
# operator can invent freely is a button label nobody has translated.
BUTTON_PRESETS = {
    "ok": (BUTTON_OK,),
    "ok_cancel": (BUTTON_OK, BUTTON_CANCEL),
    "yes_no": (BUTTON_YES, BUTTON_NO),
    "yes_no_later": (BUTTON_YES, BUTTON_NO, BUTTON_LATER),
    "accept_decline": (BUTTON_ACCEPT, BUTTON_DECLINE),
    "acknowledge_only": (BUTTON_OK,),
}
BUTTON_TEXT_KEY = "rules.button"
BUTTON_PRESET_TEXT_KEY = "rules.buttons"

STYLE_DIALOG = "dialog"
STYLE_TOAST = "toast"
MESSAGE_STYLES = (STYLE_DIALOG, STYLE_TOAST)

# Outcomes that are not a button press. Every one of them is routable, which is the point:
# a message nobody answered and a message that never appeared are both things a rule should
# be able to react to, and if they were not routable they would silently vanish.
OUTCOME_TIMEOUT = "timeout"
OUTCOME_DISMISSED = "dismissed"
OUTCOME_NO_SESSION = "no_session"
OUTCOME_FAILED = "failed"
NON_BUTTON_OUTCOMES = (OUTCOME_TIMEOUT, OUTCOME_DISMISSED, OUTCOME_NO_SESSION, OUTCOME_FAILED)
OUTCOME_TEXT_KEY = "rules.outcome"

MIN_MESSAGE_TIMEOUT = 30
MAX_MESSAGE_TIMEOUT = 12 * 3600


def _validate_show_message(action, extra, allow_command, nested):
    if nested:
        # A message whose answer opens another message is a loop with a human in it. One
        # level of follow-up is expressive enough for every case the feature exists for.
        return "a message cannot be a response to another message", None
    params = action.get("params") or {}
    title = str(params.get("title") or "").strip()
    body = str(params.get("body") or "").strip()
    if not title:
        return "a message needs a title", None
    if not body:
        return "a message needs a body", None
    if len(title) > 200:
        return "message title is too long", None
    if len(body) > MAX_MESSAGE_CHARS:
        return "message body is too long", None

    style = params.get("style") or STYLE_DIALOG
    if style not in MESSAGE_STYLES:
        return f"message style must be one of {', '.join(MESSAGE_STYLES)}", None

    err, buttons = _validate_buttons(params)
    if err:
        return err, None
    button_ids = [b["id"] for b in buttons]

    default_button = params.get("default_button")
    if default_button is not None:
        default_button = str(default_button)
        if default_button not in button_ids:
            return f"default button '{default_button}' is not one of the buttons", None

    timeout = params.get("timeout_seconds")
    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return "timeout must be a number of seconds", None
        if not MIN_MESSAGE_TIMEOUT <= timeout <= MAX_MESSAGE_TIMEOUT:
            return (f"timeout must be between {MIN_MESSAGE_TIMEOUT} and "
                    f"{MAX_MESSAGE_TIMEOUT} seconds"), None

    target_session = params.get("target_session")
    if target_session is not None:
        try:
            target_session = int(target_session)
        except (TypeError, ValueError):
            return "target session must be a number", None

    # Every possible outcome is routable, and an outcome with no route is an explicit no-op
    # rather than an error -- but an outcome that CANNOT happen is a typo worth catching,
    # because a rule whose "yes" branch is spelled "Yes" would look configured and do nothing.
    routable = set(button_ids) | set(NON_BUTTON_OUTCOMES)
    on_response = action.get("on_response") or {}
    if not isinstance(on_response, dict):
        return "on_response must be an object", None
    normalised_response = {}
    for outcome, followups in on_response.items():
        if outcome not in routable:
            return (f"'{outcome}' is not a possible outcome of this message; "
                    f"expected one of {', '.join(sorted(routable))}"), None
        if not followups:
            continue
        err, clean = validate_actions(followups, extra, allow_command, _nested=True)
        if err:
            return f"response to '{outcome}': {err}", None
        normalised_response[outcome] = clean

    clean_params = {"title": title, "body": body, "style": style, "buttons": buttons}
    if default_button:
        clean_params["default_button"] = default_button
    if timeout is not None:
        clean_params["timeout_seconds"] = timeout
    if target_session is not None:
        clean_params["target_session"] = target_session
    return None, {"type": ACTION_SHOW_MESSAGE, "params": clean_params,
                  "on_response": normalised_response}


def _validate_buttons(params):
    preset = params.get("preset")
    raw = params.get("buttons")
    if not raw and preset:
        if preset not in BUTTON_PRESETS:
            return f"unknown button preset: {preset}", None
        raw = [{"id": bid} for bid in BUTTON_PRESETS[preset]]
    if not raw:
        raw = [{"id": BUTTON_OK}]
    if not isinstance(raw, list):
        return "buttons must be a list", None
    if not 1 <= len(raw) <= 4:
        return "a message needs between 1 and 4 buttons", None
    buttons, seen = [], set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            return "each button must be an object", None
        bid = str(entry.get("id") or "")
        if bid not in BUTTON_IDS:
            return (f"unknown button '{bid or '(missing)'}'; "
                    f"expected one of {', '.join(BUTTON_IDS)}"), None
        if bid in seen:
            return f"duplicate button: {bid}", None
        seen.add(bid)
        button = {"id": bid}
        label = str(entry.get("label") or "").strip()
        if label:
            if len(label) > 40:
                return "button label is too long", None
            button["label"] = label
        style = entry.get("style")
        if style in ("primary", "default", "quiet"):
            button["style"] = style
        buttons.append(button)
    return None, buttons


def _validate_webhook(params):
    url = str(params.get("url") or "").strip()
    if not url:
        return "a webhook action needs a URL", None
    if len(url) > 1000:
        return "webhook URL is too long", None
    if not url.lower().startswith("https://"):
        # Plain http would put machine names, usernames and OU paths on the wire in clear.
        # The SSRF/private-address checks happen at DELIVERY time (notify.py), because the
        # address a hostname resolves to is not knowable when the rule is saved.
        return "webhook URL must be https", None
    template = str(params.get("template") or "").strip()
    if len(template) > MAX_MESSAGE_CHARS:
        return "webhook template is too long", None
    clean = {"url": url}
    if template:
        clean["template"] = template
    return None, {"type": ACTION_WEBHOOK, "params": clean}


def _validate_email(params):
    recipients = params.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    if not isinstance(recipients, list) or not recipients:
        return "an email action needs at least one recipient", None
    if len(recipients) > 20:
        return "too many recipients", None
    clean_to = []
    for entry in recipients:
        address = str(entry or "").strip()
        # Deliberately shallow: the SMTP server is the authority on what it will accept, and
        # a stricter regex here would reject valid addresses to no benefit.
        if "@" not in address or len(address) > 254 or any(c in address for c in "\r\n"):
            return f"invalid email address: {entry!r}", None
        clean_to.append(address)
    subject = str(params.get("subject") or "").strip()
    if not subject:
        return "an email action needs a subject", None
    if len(subject) > 200 or any(c in subject for c in "\r\n"):
        # Newlines in a subject are header injection, not a formatting choice.
        return "invalid email subject", None
    body = str(params.get("body") or "").strip()
    if not body:
        return "an email action needs a body", None
    if len(body) > MAX_MESSAGE_CHARS:
        return "email body is too long", None
    return None, {"type": ACTION_EMAIL,
                  "params": {"to": clean_to, "subject": subject, "body": body}}


def rule_can_run(actions, command_actions_enabled):
    """Whether a rule's actions are currently permitted to run, and why not.

    Separate from validation because the answer changes with a SETTING, not with the rule:
    an operator can disarm every command action fleet-wide without editing (or invalidating)
    the rules that use them. Returns (allowed, reason).
    """
    for action in actions or []:
        if action.get("type") in MUTATING_ACTIONS and not command_actions_enabled:
            return False, "command actions are disabled fleet-wide (rules.command_actions_enabled)"
    return True, None


def actions_include_command(actions):
    """Does this rule (including any message follow-up) issue a command? Drives the
    cooldown floor and the extra capability check."""
    for action in actions or []:
        if action.get("type") in MUTATING_ACTIONS:
            return True
        for followups in (action.get("on_response") or {}).values():
            if actions_include_command(followups):
                return True
    return False


# ---------------------------------------------------------------------------------------
# Rule store
# ---------------------------------------------------------------------------------------

MAX_FOR_SECONDS = 7 * 86400
MAX_COOLDOWN_SECONDS = 30 * 86400


def _decode_rule(row):
    rule = dict(row)
    for column, key in (("target_json", "target"), ("condition_json", "condition"),
                        ("actions_json", "actions")):
        try:
            rule[key] = json.loads(rule.pop(column) or "null")
        except (TypeError, ValueError):
            rule[key] = None
    rule["enabled"] = bool(rule.get("enabled"))
    return rule


def list_rules(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM rules ORDER BY name, id").fetchall()
    return [_decode_rule(r) for r in rows]


def get_rule(db_path, rule_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return _decode_rule(row) if row else None


def validate_rule(db_path, payload, *, extra=None, allow_command=True,
                  max_targets_cap=25, command_cooldown_floor=3600):
    """Validate a whole rule payload. Returns (error, normalised).

    The two clamps at the end are the fuses that cannot be argued with from the UI:
    `max_targets_per_tick` is capped by the fleet-wide setting, and a rule that issues
    commands has its cooldown raised to the floor. Both are applied to the STORED value
    rather than checked at fire time, so an operator can see what they actually got.
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        return "a rule needs a name", None
    if len(name) > 120:
        return "rule name is too long", None

    if extra is None:
        extra = field_variables(db_path)

    err, target = validate_target(payload.get("target"), extra)
    if err:
        return f"target: {err}", None

    # The condition arrives either as an AST (builder) or as text (expression editor). Both
    # end up as the same AST -- that equivalence is the whole design, and it is enforced
    # here by there being exactly one place that produces a stored condition.
    if payload.get("condition_text") and not payload.get("condition"):
        err, condition = parse_expression(payload["condition_text"], extra)
        if err:
            return f"condition: {err}", None
    else:
        err, condition = validate_condition(payload.get("condition"), extra)
        if err:
            return f"condition: {err}", None

    err, actions = validate_actions(payload.get("actions"), extra, allow_command)
    if err:
        return f"actions: {err}", None

    def _int(key, default, low, high):
        raw = payload.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return max(low, min(high, value))

    for_seconds = _int("for_seconds", 0, 0, MAX_FOR_SECONDS)
    if for_seconds is None:
        return "for_seconds must be a number", None
    cooldown = _int("cooldown_seconds", 3600, 0, MAX_COOLDOWN_SECONDS)
    if cooldown is None:
        return "cooldown_seconds must be a number", None
    max_targets = _int("max_targets_per_tick", max_targets_cap, 1, max_targets_cap)
    if max_targets is None:
        return "max_targets_per_tick must be a number", None

    if actions_include_command(actions):
        cooldown = max(cooldown, command_cooldown_floor)

    return None, {
        "name": name,
        "description": str(payload.get("description") or "")[:1000],
        "enabled": bool(payload.get("enabled", True)),
        "target": target,
        "condition": condition,
        "condition_text": format_expression(condition),
        "for_seconds": for_seconds,
        "cooldown_seconds": cooldown,
        "max_targets_per_tick": max_targets,
        "actions": actions,
    }


def save_rule(db_path, payload, *, rule_id=None, actor="", now=None, **kwargs):
    """Create or update a rule. Returns (error, rule)."""
    err, rule = validate_rule(db_path, payload, **kwargs)
    if err:
        return err, None
    now = int(now if now is not None else time.time())
    values = (rule["name"], rule["description"], 1 if rule["enabled"] else 0,
              json.dumps(rule["target"]), json.dumps(rule["condition"]),
              rule["condition_text"], rule["for_seconds"], rule["cooldown_seconds"],
              rule["max_targets_per_tick"], json.dumps(rule["actions"]))
    with get_conn(db_path) as conn:
        if rule_id:
            cur = conn.execute(
                "UPDATE rules SET name=?, description=?, enabled=?, target_json=?, "
                "condition_json=?, condition_text=?, for_seconds=?, cooldown_seconds=?, "
                "max_targets_per_tick=?, actions_json=?, updated_at=?, updated_by=? "
                "WHERE id=?",
                values + (now, str(actor or ""), rule_id),
            )
            if not cur.rowcount:
                return "no such rule", None
            new_id = rule_id
        else:
            cur = conn.execute(
                "INSERT INTO rules (name, description, enabled, target_json, condition_json, "
                "condition_text, for_seconds, cooldown_seconds, max_targets_per_tick, "
                "actions_json, created_at, created_by, updated_at, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values + (now, str(actor or ""), now, str(actor or "")),
            )
            new_id = cur.lastrowid
    return None, get_rule(db_path, new_id)


def delete_rule(db_path, rule_id):
    """Drop a rule, its per-machine state, and its fire history.

    The history goes too. It is tempting to keep it, but a fire row whose rule is gone
    cannot be explained -- the condition and actions it names no longer exist -- and an
    unexplainable audit row is worse than none. The audit_log entry for the deletion is the
    durable record.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM rule_state WHERE rule_id = ?", (rule_id,))
        conn.execute("DELETE FROM rule_fires WHERE rule_id = ?", (rule_id,))
        cur = conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return cur.rowcount > 0


def set_rule_enabled(db_path, rule_id, enabled, actor="", now=None):
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE rules SET enabled=?, updated_at=?, updated_by=? WHERE id=?",
            (1 if enabled else 0, now, str(actor or ""), rule_id),
        )
        if not cur.rowcount:
            return False
        if not enabled:
            # Disabling clears the debounce clock. Otherwise a rule paused for a fortnight
            # would come back and immediately fire on every machine whose condition had been
            # quietly "true since" the day it was switched off.
            conn.execute("UPDATE rule_state SET matched_since=NULL, firing=0, updated_at=? "
                         "WHERE rule_id=?", (now, rule_id))
    return True


def get_rule_state(db_path, rule_id):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM rule_state WHERE rule_id = ?", (rule_id,)).fetchall()
    return {r["machine"]: dict(r) for r in rows}


def list_fires(db_path, rule_id=None, machine=None, limit=100):
    clauses, params = [], []
    if rule_id is not None:
        clauses.append("rule_id = ?")
        params.append(rule_id)
    if machine:
        clauses.append("machine = ?")
        params.append(machine)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(1000, int(limit))))
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM rule_fires {where} ORDER BY fired_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    out = []
    for row in rows:
        fire = dict(row)
        for key in ("actions_json", "detail"):
            try:
                fire[key.replace("_json", "")] = json.loads(fire.pop(key) or "null")
            except (TypeError, ValueError):
                fire[key.replace("_json", "")] = None
        out.append(fire)
    return out


def prune_fires(db_path, retention_days, now=None):
    now = int(now if now is not None else time.time())
    cutoff = now - int(retention_days) * 86400
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM rule_fires WHERE fired_at < ?", (cutoff,))
        return cur.rowcount


# ---------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "actions_enabled": True,
    "command_actions_enabled": False,
    "max_targets_per_tick": 25,
    "command_cooldown_floor_seconds": 3600,
    "command_ttl_seconds": fleet.DEFAULT_COMMAND_TTL_SECONDS,
}


def evaluate_once(db_path, resolve, *, now=None, config=None, deliver=None, audit=None):
    """One evaluation pass over every enabled rule. Returns a summary dict.

    `resolve(machine) -> {name: Value}` is supplied by the caller (app.py) because building a
    variable map needs extract_diagnostics, which lives in app. Keeping it a callback is what
    lets this module stay app-free and lets the caller batch its sensor reads.

    `deliver(action, context) -> (ok, detail)` handles webhook/email (notify.py, phase 5).
    None means those actions are recorded as skipped rather than attempted.

    `audit(actor, action, target, detail)` records a fire in the security trail.

    The pass is deliberately ordered read -> decide -> dispatch -> write, with no database
    connection held across the dispatch step. fleet.create_command opens its OWN connection,
    and calling it inside a `with get_conn(...)` here would be two connections contending for
    the same write lock -- the exact deadlock packages.py warns about at its dispatch site.
    """
    now = int(now if now is not None else time.time())
    config = {**DEFAULT_CONFIG, **(config or {})}
    summary = {"rules": 0, "evaluated": 0, "matched": 0, "fired": 0, "capped": [], "errors": []}

    active = [r for r in list_rules(db_path) if r["enabled"]]
    if not active:
        return summary

    machines = _all_machines(db_path)
    pending = []          # fires to dispatch once every connection is closed
    state_writes = []     # (rule_id, machine, matched_since, firing, fired)
    episode_ends = []     # (rule_id, machine) whose alert episode should close

    for rule in active:
        summary["rules"] += 1
        try:
            targets = resolve_targets(db_path, rule["target"], machines)
        except Exception as exc:                      # noqa: BLE001 - one bad rule must not
            summary["errors"].append(f"rule {rule['id']}: target: {exc}")  # stop the others
            continue
        state = get_rule_state(db_path, rule["id"])
        allowed, block_reason = rule_can_run(rule["actions"],
                                             config["command_actions_enabled"])
        fired_this_tick = 0
        cap = min(int(rule.get("max_targets_per_tick") or 25),
                  int(config["max_targets_per_tick"]))
        capped = 0

        for machine in targets:
            summary["evaluated"] += 1
            try:
                variables = resolve(machine)
                result = evaluate(rule["condition"], variables)
            except Exception as exc:                  # noqa: BLE001
                summary["errors"].append(f"rule {rule['id']}/{machine}: {exc}")
                continue

            previous = state.get(machine) or {}
            matched_since = previous.get("matched_since")

            if result is not True:
                # Anything that is not definitely TRUE resets the debounce clock -- including
                # UNKNOWN. "We lost contact with the PC" must not keep accumulating toward
                # "the condition has held for ten minutes".
                if matched_since is not None or previous.get("firing"):
                    state_writes.append((rule["id"], machine, None, 0, None))
                    episode_ends.append((rule["id"], machine))
                continue

            summary["matched"] += 1
            if matched_since is None:
                matched_since = now
            held_for = now - matched_since

            if held_for < int(rule["for_seconds"] or 0):
                state_writes.append((rule["id"], machine, matched_since, 1, None))
                continue

            snoozed_until = previous.get("snoozed_until")
            if snoozed_until and now < int(snoozed_until):
                state_writes.append((rule["id"], machine, matched_since, 1, None))
                continue

            last_fired = previous.get("last_fired_at")
            if last_fired and now - int(last_fired) < int(rule["cooldown_seconds"] or 0):
                state_writes.append((rule["id"], machine, matched_since, 1, None))
                continue

            if fired_this_tick >= cap:
                # Stall at the cap rather than firing on. The count is reported so a
                # misscoped rule shows up as "held back 380 machines" in the log instead of
                # as four hundred reboots.
                capped += 1
                state_writes.append((rule["id"], machine, matched_since, 1, None))
                continue

            fired_this_tick += 1
            pending.append({"rule": rule, "machine": machine, "variables": variables,
                            "matched_since": matched_since, "allowed": allowed,
                            "block_reason": block_reason})

        if capped:
            summary["capped"].append({"rule_id": rule["id"], "rule": rule["name"],
                                      "held_back": capped, "cap": cap})

    # -- dispatch, with no connection held ------------------------------------------------
    for fire in pending:
        try:
            _fire(db_path, fire, now=now, config=config, deliver=deliver, audit=audit)
            summary["fired"] += 1
            state_writes.append((fire["rule"]["id"], fire["machine"],
                                 fire["matched_since"], 1, now))
        except Exception as exc:                      # noqa: BLE001
            summary["errors"].append(f"rule {fire['rule']['id']}/{fire['machine']}: {exc}")
            # Still record the attempt in state, so a rule whose action keeps throwing does
            # not retry it every single tick forever.
            state_writes.append((fire["rule"]["id"], fire["machine"],
                                 fire["matched_since"], 1, now))

    _write_state(db_path, state_writes, now)
    for rule_id, machine in episode_ends:
        try:
            alerts.end_rule_episode(db_path, machine, rule_id, now=now)
        except Exception:                             # noqa: BLE001
            pass
    return summary


def _write_state(db_path, writes, now):
    if not writes:
        return
    with get_conn(db_path) as conn:
        for rule_id, machine, matched_since, firing, fired_at in writes:
            if fired_at is None:
                conn.execute(
                    """
                    INSERT INTO rule_state (rule_id, machine, matched_since, firing, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(rule_id, machine) DO UPDATE SET
                        matched_since = excluded.matched_since,
                        firing        = excluded.firing,
                        updated_at    = excluded.updated_at
                    """,
                    (rule_id, machine, matched_since, firing, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO rule_state (rule_id, machine, matched_since, firing,
                                            last_fired_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rule_id, machine) DO UPDATE SET
                        matched_since = excluded.matched_since,
                        firing        = excluded.firing,
                        last_fired_at = excluded.last_fired_at,
                        updated_at    = excluded.updated_at
                    """,
                    (rule_id, machine, matched_since, firing, fired_at, now),
                )


def _fire(db_path, fire, *, now, config, deliver=None, audit=None):
    """Run one rule's actions against one machine and record the fire."""
    rule = fire["rule"]
    machine = fire["machine"]
    variables = fire["variables"]
    fire_id = _record_fire(db_path, rule["id"], machine, now)

    dispatched = dispatch_actions(
        db_path, rule["actions"], machine, variables,
        rule=rule, now=now, config=config, deliver=deliver,
        allowed=fire["allowed"], block_reason=fire["block_reason"], fire_id=fire_id,
    )

    command_id = next((d.get("command_id") for d in dispatched if d.get("command_id")), None)
    with get_conn(db_path) as conn:
        conn.execute("UPDATE rule_fires SET actions_json=?, command_id=? WHERE id=?",
                     (json.dumps(dispatched), command_id, fire_id))
    if audit:
        audit(f"rule:{rule['id']}", "rule_fired", machine,
              {"rule_id": rule["id"], "rule": rule["name"],
               "actions": [d.get("type") for d in dispatched],
               "command_id": command_id})
    return fire_id


def _record_fire(db_path, rule_id, machine, now):
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO rule_fires (rule_id, machine, fired_at) VALUES (?, ?, ?)",
            (rule_id, machine, now),
        )
        return cur.lastrowid


def dispatch_actions(db_path, actions, machine, variables, *, rule, now, config,
                     deliver=None, allowed=True, block_reason=None, fire_id=None):
    """Run a list of actions for one machine. Returns a list of per-action result dicts.

    Shared by the evaluator and by the show_message response router, which is the point: a
    follow-up action chosen by a user clicking "Yes" goes through exactly the same kill
    switches, the same command whitelist and the same audit path as one the rule fired
    directly. Two dispatch paths would eventually mean two sets of rules about what a rule
    may do.

    Never raises for a single action's failure -- one dead SMTP host must not stop the
    restart the operator actually cared about.
    """
    results = []
    for action in actions or []:
        kind = action.get("type")
        params = action.get("params") or {}
        record = {"type": kind}
        try:
            if not config.get("actions_enabled", True):
                record["skipped"] = "actions are disabled fleet-wide (rules.actions_enabled)"
            elif kind in MUTATING_ACTIONS and not allowed:
                record["skipped"] = block_reason or "blocked"
            elif kind == ACTION_ALERT:
                text = render_template(params.get("text"), variables)
                record["alert_id"] = alerts.upsert_rule(db_path, machine, rule["id"],
                                                        rule["name"], text, now=now)
                record["text"] = text
            elif kind == ACTION_SNOOZE:
                until = now + int(params.get("seconds") or 0)
                _snooze(db_path, rule["id"], machine, until, now)
                record["snoozed_until"] = until
            elif kind == ACTION_COMMAND:
                record["command_id"] = fleet.create_command(
                    db_path, machine, params["command_type"], params.get("params") or {},
                    issued_by=f"rule:{rule['id']}",
                    ttl_seconds=int(config.get("command_ttl_seconds")
                                    or fleet.DEFAULT_COMMAND_TTL_SECONDS),
                )
                record["command_type"] = params["command_type"]
            elif kind == ACTION_SHOW_MESSAGE:
                record.update(_dispatch_message(db_path, action, machine, variables,
                                                rule=rule, now=now, config=config,
                                                fire_id=fire_id))
            elif kind in (ACTION_WEBHOOK, ACTION_EMAIL):
                if deliver is None:
                    record["skipped"] = "no delivery channel is configured"
                else:
                    ok, detail = deliver(action, {"machine": machine, "rule": rule,
                                                  "variables": variables, "now": now})
                    record["delivered"] = bool(ok)
                    if detail:
                        record["detail"] = detail
            else:
                record["skipped"] = f"unknown action type: {kind}"
        except Exception as exc:                      # noqa: BLE001
            record["error"] = str(exc)
        results.append(record)
    return results


def _dispatch_message(db_path, action, machine, variables, *, rule, now, config, fire_id):
    """Queue a show_message command and remember how to route its answer.

    The on_response map is SNAPSHOTTED into the fire row rather than read from the rule when
    the answer comes back. A dialog can sit on someone's screen for hours; editing the rule
    in the meantime must not change what the button they are looking at is about to do.

    The TTL is stretched to cover the dialog's own timeout. The default command TTL is 15
    minutes (fleet.DEFAULT_COMMAND_TTL_SECONDS) and a message may legitimately wait longer
    than that for a human, so without this the hub would expire a command whose dialog is
    still on screen -- and the answer would arrive against a command that no longer exists.
    """
    params = dict(action.get("params") or {})
    params["title"] = render_template(params.get("title"), variables)
    params["body"] = render_template(params.get("body"), variables)

    timeout = int(params.get("timeout_seconds") or 0)
    ttl = int(config.get("command_ttl_seconds") or fleet.DEFAULT_COMMAND_TTL_SECONDS)
    if timeout:
        ttl = max(ttl, timeout + 300)

    command_id = fleet.create_command(db_path, machine, ACTION_SHOW_MESSAGE, params,
                                      issued_by=f"rule:{rule['id']}", ttl_seconds=ttl)
    if fire_id:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE rule_fires SET detail=? WHERE id=?",
                         (json.dumps({"on_response": action.get("on_response") or {},
                                      "buttons": params.get("buttons") or []}), fire_id))
    return {"command_id": command_id, "title": params["title"]}


def handle_message_result(db_path, command_id, *, status=None, result=None, now=None,
                          config=None, deliver=None, audit=None):
    """Route a show_message command's answer to its follow-up actions.

    Called from the agent command-result endpoint. Returns None when the command was not a
    rule-issued message (the overwhelmingly common case, so the lookup is one indexed read
    and an early return), else a summary of what the answer triggered.

    Outcome precedence: what the agent reported, else `failed` if the command failed at all,
    else `timeout`. An unmapped outcome is recorded and does nothing -- an explicit no-op, so
    that "the user clicked No and nothing happened" is visible in the history rather than
    indistinguishable from the message never having been shown.
    """
    now = int(now if now is not None else time.time())
    config = {**DEFAULT_CONFIG, **(config or {})}

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM rule_fires WHERE command_id = ? ORDER BY id DESC LIMIT 1",
            (str(command_id),),
        ).fetchone()
    if not row or row["outcome"] is not None:
        # No such fire, or the answer already landed. The second case matters: a result
        # endpoint can be retried by an agent that did not see our 200, and a retry must not
        # restart the machine a second time.
        return None

    try:
        routing = json.loads(row["detail"] or "{}") or {}
    except (TypeError, ValueError):
        routing = {}
    on_response = routing.get("on_response") or {}

    outcome = None
    if isinstance(result, dict):
        outcome = str(result.get("outcome") or "").strip() or None
    if not outcome:
        outcome = OUTCOME_FAILED if status == fleet.STATUS_FAILED else OUTCOME_TIMEOUT

    with get_conn(db_path) as conn:
        conn.execute("UPDATE rule_fires SET outcome=?, outcome_at=? WHERE id=?",
                     (outcome, now, row["id"]))

    followups = on_response.get(outcome) or []
    rule = get_rule(db_path, row["rule_id"])
    if not followups or not rule:
        return {"outcome": outcome, "actions": []}

    machine = row["machine"]
    # Re-resolve nothing: the follow-up's templates are rendered against the variables as
    # they were when the message was SENT is tempting, but wrong -- an hour may have passed
    # and "your PC has been up 9 days" would now be stale. Resolve fresh, cheaply, from the
    # DB only; a follow-up template referencing a live metric gets today's answer or UNKNOWN.
    variables = resolve_machine_vars(db_path, machine, now=now)

    allowed, block_reason = rule_can_run(followups, config["command_actions_enabled"])
    dispatched = dispatch_actions(db_path, followups, machine, variables,
                                  rule=rule, now=now, config=config, deliver=deliver,
                                  allowed=allowed, block_reason=block_reason,
                                  fire_id=row["id"])
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE rule_fires SET detail=? WHERE id=?",
            (json.dumps({**routing, "outcome_actions": dispatched}), row["id"]),
        )
    if audit:
        audit(f"rule:{rule['id']}", "rule_message_answered", machine,
              {"rule_id": rule["id"], "rule": rule["name"], "outcome": outcome,
               "actions": [d.get("type") for d in dispatched]})
    return {"outcome": outcome, "actions": dispatched}


def collect_probes_once(db_path, machines, *, now=None, max_per_tick=50, ttl_seconds=None):
    """Issue collect_probe commands for every collection that is due. Returns how many.

    `machines` is supplied by the caller (app.py), already narrowed to enrolled machines that
    are online -- issuing to an offline PC would just fill its queue with probes that all
    expire together and then all re-fire the moment it comes back.

    `requested_at` is stamped BEFORE the command is created, so a probe is not re-issued on
    the next tick while the first one is still in flight. That ordering also means a probe
    whose command creation throws simply waits for its next interval rather than retrying in
    a tight loop.
    """
    now = int(now if now is not None else time.time())
    due = probes_due(db_path, machines, now=now)[:max_per_tick]
    if not due:
        return 0

    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO machine_probe_values (machine, name, requested_at)
            VALUES (?, ?, ?)
            ON CONFLICT(machine, name) DO UPDATE SET requested_at = excluded.requested_at
            """,
            [(machine, probe["name"], now) for machine, probe in due],
        )

    issued = 0
    for machine, probe in due:
        try:
            fleet.create_command(
                db_path, machine, "collect_probe",
                {"probe": probe["name"], "kind": probe["kind"], "spec": probe["spec"],
                 "timeout_seconds": probe["timeout_seconds"]},
                issued_by="rules:probe",
                ttl_seconds=int(ttl_seconds or fleet.DEFAULT_COMMAND_TTL_SECONDS),
            )
            issued += 1
        except Exception:                             # noqa: BLE001
            # One machine's queue being unwritable must not stop the other forty-nine.
            continue
    return issued


def handle_probe_result(db_path, command_id, *, success=True, result=None, output=None,
                        now=None):
    """File a collect_probe answer against its probe. Returns the probe name, or None.

    Like handle_message_result this returns None immediately for any command that is not a
    probe collection, so the shared result hook stays one indexed read for the overwhelming
    majority of results.
    """
    command = fleet.get_command(db_path, command_id)
    if not command or command.get("type") != "collect_probe":
        return None
    params = command.get("params") or {}
    name = params.get("probe")
    machine = command.get("machine")
    if not name or not machine:
        return None

    value, error = None, ""
    if isinstance(result, dict):
        value = result.get("value")
        error = str(result.get("error") or "")
    elif output:
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                value = parsed.get("value")
                error = str(parsed.get("error") or "")
            else:
                value = parsed
        except (TypeError, ValueError):
            # A pre-probe agent, or one that answered in prose. Not a value we can file, and
            # storing the prose would put arbitrary text where a typed value belongs.
            error = "the agent did not return a probe value"
    if not success and not error:
        error = "the probe failed on the machine"

    record_probe_value(db_path, machine, name, value, error=error, now=now)
    return name


def _snooze(db_path, rule_id, machine, until, now):
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO rule_state (rule_id, machine, snoozed_until, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(rule_id, machine) DO UPDATE SET
                snoozed_until = excluded.snoozed_until, updated_at = excluded.updated_at
            """,
            (rule_id, machine, int(until), now),
        )
