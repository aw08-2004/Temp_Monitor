"""Operator-settable configuration -- the knobs behind the Settings tab.

Every operational tunable used to be a module-level constant in app.py or fleet.py,
so changing a retention window or an offline threshold meant editing code and
restarting the service. This module moves them into the DB, behind one registry.

THE REGISTRY IS THE SINGLE SOURCE OF TRUTH. One `_s(...)` line in REGISTRY below
drives all of: the default value, type coercion, range validation, the JSON schema
the API serves, the form field the Settings tab renders, and the help text under it.
Nothing else in the codebase enumerates settings. Adding a knob later is one line
here and nothing anywhere else -- no JS, no HTML, no endpoint change. Keep it that
way; the moment a second place lists setting keys, they start drifting.

Defaults MUST equal the constants they replaced, exactly. The table is sparse -- only
an overridden key gets a row -- so a hub with an empty settings table behaves
bit-identically to the version before this module existed. That guarantee is
structural rather than something anyone has to remember to preserve, and
tests/test_settings.py pins the numbers as literals so an accidental edit fails loudly.

NO SECRETS LIVE HERE. AGENT_ENROLLMENT_SECRET, the OAuth client secret, ALLOWED_EMAILS
and FLASK_SECRET_KEY stay in .env: they are deployment identity, not operator-tunable
policy, and this table is editable by anyone with a console session. The registry is
the enforcement -- set_many() rejects any key not in it -- so "just add the enrollment
secret to settings, it's more convenient" cannot quietly happen.

Nor does anything here redirect the agent's TRUST ROOTS. The subset of settings marked
agent=True is shipped to agents over the authenticated heartbeat; it carries operational
tuning only. The update manifest URL, the Ed25519 update key, and the hub base URL are
deliberately NOT settable -- per fleet.py's docstring the signed-manifest chain is the
one control that survives a compromised hub, and making it hub-settable would trade
that property away for nothing.

Kept free of Flask so it can be unit-tested in isolation, exactly like fleet.py and
alerts.py; settings_web.py wires thin HTTP endpoints on top of these functions.
"""
import hashlib
import json
import sqlite3
import threading
import time
from collections import namedtuple

# ---------------------------------------------------------------- the registry

Setting = namedtuple("Setting", [
    "key",       # "data.retention_days" -- the section is the prefix, by convention
    "section",   # "computer" | "hub" | "data" | "fleet"
    "label",     # human label in the form
    "type",      # "int" | "float" | "bool" | "str" | "enum" | "str_list" | "path_list"
    "default",   # MUST equal the constant this replaced
    "minimum",   # numeric bounds; None for non-numeric types
    "maximum",
    "unit",      # rendered as a suffix after the input ("seconds", "days", "°C")
    "help",      # one or two sentences, shown under the field
    "choices",   # for "enum"/"str_list": a list, or callable(db_path) -> list
    "agent",     # True => shipped to agents over the heartbeat config channel
])


def _s(key, section, label, type, default, *, minimum=None, maximum=None,
       unit="", help="", choices=None, agent=False):
    return Setting(key, section, label, type, default, minimum, maximum,
                   unit, help, choices, agent)


# Default for computer.primary_sensor_preference. Mirrors SensorReader.cs's
# PreferredSensors (and companion.py's PREFERRED_SENSORS) exactly -- best first,
# matched as a lowercased substring of the sensor name.
DEFAULT_SENSOR_PREFERENCE = [
    "cpu package",
    "core (tctl/tdie)",
    "core average",
    "core max",
    "cpu cores",
]

REGISTRY = (
    # ---------------- Computer: how a machine's telemetry is interpreted ----------------
    _s("computer.primary_sensor_preference", "computer",
       "CPU temperature sensor preference", "str_list", DEFAULT_SENSOR_PREFERENCE,
       help="Ordered, best first. The hub re-derives each machine's primary temperature "
            "from its reported sensor block, using the first name that matches. If none "
            "match, the temperature the agent picked is kept. Applies to new readings "
            "only -- history keeps the value recorded at the time.",
       agent=True),

    # ---------------- Hub: thresholds and internals ----------------
    _s("hub.overheat_threshold", "hub", "Overheat threshold", "int", 85,
       minimum=40, maximum=120, unit="°C",
       help="At or above this, a reading is flagged as overheating on the dashboard "
            "and the machine page."),
    _s("hub.low_load_threshold", "hub", "Low-load threshold", "int", 40,
       minimum=0, maximum=100, unit="%",
       help="A high temperature recorded below this CPU load reads 'investigate' rather "
            "than 'expected' -- hot while idle is the interesting case."),
    _s("hub.live_status_cache_seconds", "hub", "Live status fallback age", "int", 600,
       minimum=60, maximum=86400, unit="seconds",
       help="After a hub restart the last known temperature and uptime are read back from "
            "the database. Past this age they are treated as unknown rather than shown as "
            "current."),
    _s("hub.live_default_window_hours", "hub", "Default chart window", "int", 3,
       minimum=1, maximum=168, unit="hours",
       help="How much history the live charts show before you pick a range."),
    _s("hub.auto_update", "hub", "Hub auto-update", "bool", None,
       help="Let the hub pull and apply its own updates from the main branch. Leave unset "
            "to follow HUB_AUTO_UPDATE in .env; set here to override that."),

    # ---------------- Data & retention ----------------
    _s("data.retention_days", "data", "Keep readings for", "int", 30,
       minimum=1, maximum=3650, unit="days",
       help="Readings older than this are PERMANENTLY DELETED by the background pruner. "
            "Lowering this destroys history that cannot be recovered."),
    _s("data.prune_interval_seconds", "data", "Run the pruner every", "int", 86400,
       minimum=300, maximum=604800, unit="seconds",
       help="How often the retention pruner wakes up. Takes effect within 30 seconds, "
            "without a hub restart."),
    _s("data.ingest_max_backdate_days", "data", "Accept backdated reports up to", "int", 30,
       minimum=1, maximum=3650, unit="days",
       help="An agent that was offline sends its buffered readings with their original "
            "timestamps; older than this they are treated as clock skew and stamped with "
            "the arrival time instead. Deliberately independent of the retention window -- "
            "shortening retention must not start silently flattening reconnect backfills."),
    _s("data.command_output_retention_seconds", "data", "Keep terminal scrollback for",
       "int", 86400, minimum=3600, maximum=2592000, unit="seconds",
       help="Live terminal output is kept this long so you can scroll back. The durable "
            "command result is not affected."),

    # ---------------- History metrics: which sensors are recorded to history ----------------
    # One on/off toggle per chartable metric on the per-machine History dashboard. Off means
    # the hub stops recording that metric into new readings (stored NULL) -- "what sensor
    # should be read". collect_network is additionally agent=True: it tells the agent whether
    # to collect the network sensor category at all (see the agent's RuntimeConfig allow-list).
    # Temperature has no toggle -- it is the core metric and drives overheat alerts, so it is
    # always recorded.
    _s("metrics.collect_cpu_load", "metrics", "Record CPU load", "bool", True,
       help="Chart CPU load % over time on the machine History dashboard."),
    _s("metrics.collect_memory", "metrics", "Record memory usage", "bool", True,
       help="Chart memory usage % over time."),
    _s("metrics.collect_gpu", "metrics", "Record GPU temperature & load", "bool", True,
       help="Chart discrete-GPU temperature and load. No effect on machines whose GPU "
            "reports nothing."),
    _s("metrics.collect_disk", "metrics", "Record disk usage", "bool", True,
       help="Chart disk used-space % over time."),
    _s("metrics.collect_network", "metrics", "Record network throughput", "bool", True,
       help="Chart network in/out (bytes per second). Also tells the agent whether to "
            "collect the network sensor category at all.",
       agent=True),

    # ---------------- Fleet: liveness and command timings ----------------
    # These next two are different windows that operators WILL confuse, so the labels
    # describe what you observe rather than what the code does. Keep them adjacent.
    _s("fleet.dashboard_online_window_seconds", "fleet",
       "Drop a machine off the Dashboard after", "int", 120,
       minimum=30, maximum=3600, unit="seconds",
       help="A machine that hasn't reported a temperature within this window leaves the "
            "live Dashboard and reads as offline in Asset Inventory. Agents report every "
            "few seconds, so this tolerates a couple of missed reports without flapping."),
    _s("fleet.offline_after_seconds", "fleet", "Mark an agent offline after", "int", 90,
       minimum=30, maximum=3600, unit="seconds",
       help="Separate from the Dashboard window above: this is the command channel's view "
            "of whether an agent is reachable, used when you issue commands. Agents poll "
            "every 10 seconds."),
    _s("fleet.command_ttl_seconds", "fleet", "Expire unclaimed commands after", "int", 900,
       minimum=60, maximum=86400, unit="seconds",
       help="A command not picked up by its target within this window expires instead of "
            "running much later on a machine that just came back."),

    # ---------------- Deploy: package pushes ----------------
    # Retry defaults are per-deployment values the schedule form pre-fills; changing them
    # here does NOT alter deployments already created, which carry their own copy. That
    # is deliberate -- a retry policy an operator agreed to when scheduling shouldn't
    # change under them because someone edited a default mid-push.
    _s("deploy.default_max_attempts", "deploy", "Default attempts per machine", "int", 3,
       minimum=1, maximum=10,
       help="How many times a deployment tries a machine before giving up. An attempt is "
            "spent when the install fails OR when the machine never picks the command up."),
    _s("deploy.default_retry_backoff_seconds", "deploy", "Default retry backoff", "int", 900,
       minimum=60, maximum=86400, unit="seconds",
       help="Wait before the first retry. It doubles each attempt (15 min, 30 min, 1 h...), "
            "so a machine that's off for the weekend isn't retried every quarter hour."),
    _s("deploy.max_upload_mb", "deploy", "Largest package file", "int", 512,
       minimum=1, maximum=4096, unit="MB",
       help="Upload limit for a hub-hosted installer. Files are stored beside the database "
            "and shared between packages built from the same installer."),
    _s("deploy.scheduler_interval_seconds", "deploy", "Run the deploy scheduler every",
       "int", 30, minimum=10, maximum=3600, unit="seconds",
       help="How often the hub checks for deployments that are due and reads finished "
            "attempts back. Also the floor on how quickly a scheduled window starts."),

    # ---------------- Backups: the hub's own database, offsite ----------------
    # Credentials are deliberately ABSENT from this registry -- they live encrypted in a
    # sidecar file (see backups.py's secret store). Settings are rendered into a form,
    # returned wholesale by as_dict(), and partly shipped to agents by agent_config();
    # an S3 secret key belongs in none of those places. What lives here is only the
    # schedule, and which destination it aims at.
    _s("backup.hub_enabled", "backup", "Back up the hub database on a schedule", "bool",
       False,
       help="Off until you have created a destination and stored the encryption key "
            "somewhere other than this server. Nothing is uploaded while this is off."),
    _s("backup.hub_destination", "backup", "Back up to", "str", "",
       help="The id of the destination scheduled backups are written to. Set this from "
            "the Backups page, which offers the configured destinations by name -- this "
            "field is the raw id it writes."),
    _s("backup.hub_interval_hours", "backup", "Back up every", "int", 24,
       minimum=1, maximum=720, unit="hours",
       help="Measured from the last ATTEMPT, not the last success -- a destination "
            "that has been down for a week is retried on this cadence rather than on "
            "every scheduler tick."),
    _s("backup.hub_keep_generations", "backup", "Keep this many backups", "int", 14,
       minimum=1, maximum=365, unit="generations",
       help="After a successful upload, older backups beyond this count are DELETED "
            "from the destination. At the default daily cadence this is two weeks of "
            "history. Counted from what the destination actually holds, so a file you "
            "delete by hand is not silently replaced."),

    # ---------------- Backups: per-PC files ----------------
    # Edited on the Backups page's "Backup Settings" tab, which offers the token
    # reference and a live preview against a real machine. They live in the registry all
    # the same, so they get the same validation, audit trail and reset behaviour as
    # everything else -- the tab is a better editor, not a second store.
    _s("backup.files_enabled", "backup", "Back up files on managed PCs", "bool", False,
       help="Off until you have chosen a destination and reviewed the included paths. "
            "Individual machines can opt out (or in) on their own Backup tab."),
    _s("backup.files_destination", "backup", "Back up PC files to", "str", "",
       help="The id of the destination per-PC backups are written to. Set this from the "
            "Backups page; each machine gets its own folder under it."),
    _s("backup.files_include", "backup", "Include these paths", "path_list",
       ["%Desktop%", "%Documents%", "%Pictures%", "%Favorites%"],
       help="Tokens expand on each machine: %Users% covers every real profile, and "
            "%Desktop%/%Documents% follow OneDrive folder redirection -- which a literal "
            "C:\\Users\\name\\Desktop does not, so it would back up an empty stub on any "
            "PC using Known Folder Move."),
    _s("backup.files_exclude", "backup", "Never back up these", "path_list",
       ["*.tmp", "~$*", "thumbs.db", "**\\AppData\\Local\\Temp\\**",
        "**\\node_modules\\**", "**\\.git\\**", "*.iso", "*.vhdx", "*.vmdk"],
       help="Matched against the full path, case-insensitively. A pattern with no "
            "backslash matches on filename anywhere; ** crosses folders. Excluding a "
            "folder also excludes everything inside it."),
    _s("backup.files_interval_hours", "backup", "Back up PC files every", "int", 24,
       minimum=1, maximum=720, unit="hours",
       help="Measured from the last attempt on each machine, so a laptop that was off "
            "is picked up when it returns rather than skipped."),
    _s("backup.files_full_every", "backup", "Take a full backup every", "int", 7,
       minimum=1, maximum=90, unit="runs",
       help="Runs in between upload only files that changed. A shorter chain restores "
            "faster and survives a damaged archive better; a longer one uses less "
            "bandwidth."),
    _s("backup.files_keep_chains", "backup", "Keep this many backup chains", "int", 4,
       minimum=1, maximum=52, unit="chains",
       help="A chain is one full backup plus the incrementals that follow it. Whole "
            "chains are deleted together -- never a full on its own, which would strand "
            "every incremental depending on it."),
    _s("backup.files_max_file_mb", "backup", "Skip files larger than", "int", 2048,
       minimum=1, maximum=102400, unit="MB",
       help="Skipped files are named in the run result rather than failing the run. "
            "Stops one forgotten disk image from consuming a night's upload."),
    _s("backup.files_max_set_gb", "backup", "Abort a run larger than", "int", 100,
       minimum=1, maximum=10240, unit="GB",
       help="A safety stop: if the selected paths add up to more than this, the run "
            "fails with a message instead of uploading for two days. Raise it "
            "deliberately rather than by accident."),
    _s("backup.files_use_vss", "backup", "Use a shadow copy (VSS)", "bool", True,
       help="Reads from a point-in-time snapshot so files that are open -- an Outlook "
            "PST, a document someone left up -- are captured consistently. If a snapshot "
            "cannot be created the run continues against the live filesystem and reports "
            "which files were locked."),
)

BY_KEY = {s.key: s for s in REGISTRY}
SECTIONS = ("computer", "hub", "data", "metrics", "fleet", "deploy", "backup")

# The subset backups_web.py is allowed to write on behalf of a `manage_backups` holder
# who does not also hold `manage_settings`. Configuring backups IS managing backups;
# requiring the broader capability to turn one on would make the narrow one useless.
# Derived from the registry rather than typed out again, so a new backup.* key cannot be
# added to the Backups page and silently stay unwritable -- but still an explicit
# allow-list at the point of use, so this can never become a general settings-write path.
BACKUP_SETTING_KEYS = tuple(s.key for s in REGISTRY if s.section == "backup")


# ---------------------------------------------------------------- storage

def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_settings_db(db_path):
    """Create the settings table if absent. Idempotent -- safe to call next to
    app.init_db()/fleet.init_fleet_db()/alerts.init_alerts_db() on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,   -- JSON so type survives the round-trip
                updated_at INTEGER NOT NULL,
                updated_by TEXT
            )
            """
        )


# ---------------------------------------------------------------- coercion & validation

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def coerce_and_validate(setting, raw):
    """Coerce `raw` to the setting's declared type and enforce its bounds.

    Raises ValueError naming the key -- the message is shown verbatim next to the
    field in the UI, so it has to read like something an operator can act on.

    JSON is sloppy about types (a number arrives as "85" from some clients, a bool as
    1), so coerce rather than reject: the operator typed a valid value and shouldn't
    be told otherwise because of a transport detail.
    """
    label = setting.label

    if setting.type == "bool":
        # Tri-state: None is a real, meaningful value for hub.auto_update ("follow .env").
        if raw is None or raw == "":
            return None
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"{label}: expected true or false, got {raw!r}")

    if setting.type in ("int", "float"):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise ValueError(f"{label}: a value is required")
        try:
            value = int(raw) if setting.type == "int" else float(raw)
        except (TypeError, ValueError):
            kind = "whole number" if setting.type == "int" else "number"
            raise ValueError(f"{label}: expected a {kind}, got {raw!r}")
        if setting.minimum is not None and value < setting.minimum:
            raise ValueError(
                f"{label}: must be at least {setting.minimum}{_unit_suffix(setting)}")
        if setting.maximum is not None and value > setting.maximum:
            raise ValueError(
                f"{label}: must be at most {setting.maximum}{_unit_suffix(setting)}")
        return value

    if setting.type == "str":
        if raw is None:
            return None
        return str(raw).strip()

    if setting.type == "enum":
        text = str(raw).strip()
        allowed = setting.choices if isinstance(setting.choices, (list, tuple)) else None
        if allowed is not None and text not in allowed:
            raise ValueError(f"{label}: {text!r} is not one of {', '.join(allowed)}")
        return text

    if setting.type == "str_list":
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"{label}: expected a list of names")
        items = [str(v).strip() for v in raw if str(v).strip()]
        if not items:
            raise ValueError(f"{label}: needs at least one entry")
        # Preference lists are matched case-insensitively downstream; normalise here so
        # what is stored is what is matched, and a stray "CPU Package" can't look
        # different from "cpu package" in the UI while behaving identically.
        return [v.lower() for v in items]

    if setting.type == "path_list":
        # Backup include/exclude patterns. Deliberately NOT str_list, which is wrong here
        # twice: it refuses an empty list (an empty exclude list is a perfectly good
        # answer) and it lowercases every entry (right for sensor names, but it would
        # hand the operator back `c:\users\%users%\desktop` after they typed
        # `C:\Users\%Users%\Desktop` -- and a settings page that visibly mangles what you
        # typed is one you stop trusting).
        #
        # Each entry is validated through the shared grammar, so a typo'd token is
        # refused HERE rather than silently expanding to nothing on every machine in the
        # fleet. Imported lazily: settings.py is imported by nearly everything, and
        # backup_paths.py is only needed on this one path.
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"{label}: expected a list of paths")
        import backup_paths
        kind = "exclude" if setting.key.endswith("_exclude") else "include"
        try:
            return backup_paths.validate_patterns(raw, kind=kind)
        except ValueError as e:
            raise ValueError(f"{label}: {e}")

    raise ValueError(f"{label}: unsupported setting type {setting.type!r}")


def _unit_suffix(setting):
    return f" {setting.unit}" if setting.unit else ""


# ---------------------------------------------------------------- the cache
#
# derive_machine_status() reads a setting once per machine per /api/machines request,
# so neither a DB round-trip nor a lock can sit in the read path. The hub is one
# waitress process: many request threads plus the background pruner and version
# watchers, sharing one address space, with very hot reads and very rare writes.
#
# Hence copy-on-write. Readers take a single module-global reference (one attribute
# read, atomic under the GIL) and only ever read the dict it points at. Writers build
# a COMPLETE new state and rebind the global in one assignment, so a reader in flight
# sees either the whole old state or the whole new one, never a torn mix. The cached
# dict is never mutated in place -- doing that is how you eventually get a
# "dictionary changed size during iteration" under load, once, months later.
#
# This cache is per-process, which is correct under waitress and would be silently
# wrong under gunicorn with workers > 1: a save in worker A would stay invisible to
# worker B until a restart. If the hub ever moves to multiple workers, the fix is a
# settings_version row polled at most every few seconds -- not a read-through cache.
# Noting it here because the failure mode is silent.

_state = None                    # dict[key] -> value, or None when cold
_state_lock = threading.Lock()   # serialises writers and cold loads ONLY, never readers


def _build(db_path):
    """Registry defaults overlaid with whatever the DB overrides. Returns the full
    state: the values dict plus the agent-config hash derived from it."""
    values = {s.key: s.default for s in REGISTRY}
    try:
        with get_conn(db_path) as conn:
            rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
    except sqlite3.Error as e:
        # Degrade to defaults rather than propagate. These values are read from the
        # request path (derive_machine_status) and from background threads, so a raise
        # here would turn a bootstrap problem into a dashboard outage -- and because
        # every default equals the constant it replaced, running on defaults is exactly
        # the pre-settings behaviour rather than some arbitrary fallback. Loud, though:
        # silently serving defaults forever is its own failure mode.
        print(f"[settings] Could not read the settings table ({e}); using defaults.")
        rows = []
    for row in rows:
        key = row["key"]
        if key not in BY_KEY:
            continue      # a knob that was removed in a later version; ignore the row
        try:
            values[key] = json.loads(row["value_json"])
        except (TypeError, ValueError):
            pass          # corrupt row -> fall back to the registry default
    return {"values": values, "agent_version": _agent_version_for(values)}


def _current(db_path):
    global _state
    state = _state                       # single read; never dereference _state twice
    if state is None:
        with _state_lock:
            if _state is None:
                _state = _build(db_path)
            state = _state
    return state


def get(db_path, key):
    """Effective value for `key` -- the DB override if there is one, else the registry
    default. Hot path: one global read and one dict lookup, no lock and no DB."""
    values = _current(db_path)["values"]
    if key in values:
        return values[key]
    setting = BY_KEY.get(key)
    return setting.default if setting else None


def get_int(db_path, key):
    value = get(db_path, key)
    return int(value) if value is not None else None


def get_bool(db_path, key):
    return get(db_path, key)


def get_list(db_path, key):
    value = get(db_path, key)
    return list(value) if isinstance(value, (list, tuple)) else []


def as_dict(db_path):
    """Every key -> effective value. A copy, so a caller can't mutate the cache."""
    return dict(_current(db_path)["values"])


def invalidate():
    """Drop the cache; the next read rebuilds it. Writers call this via set_many/reset,
    but tests and any out-of-band DB edit need it too."""
    global _state
    with _state_lock:
        _state = None


def set_many(db_path, updates, updated_by=None):
    """Validate every update, then apply them all. Returns {key: coerced value}.

    All-or-nothing on purpose: the Settings tab saves a whole section at once, and a
    partial save that applied three fields of five would leave the operator with no
    idea which took effect. One bad field rejects the batch, with a ValueError whose
    message names it.
    """
    if not isinstance(updates, dict):
        raise ValueError("updates must be an object of key -> value")

    coerced = {}
    for key, raw in updates.items():
        setting = BY_KEY.get(key)
        if setting is None:
            # Also the guard that keeps secrets out: not in the registry, not settable.
            raise ValueError(f"unknown setting: {key}")
        coerced[key] = coerce_and_validate(setting, raw)

    now = int(time.time())
    with _state_lock:
        with get_conn(db_path) as conn:
            for key, value in coerced.items():
                conn.execute(
                    "INSERT INTO settings(key, value_json, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                    "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                    (key, json.dumps(value), now, updated_by),
                )
        global _state
        _state = None      # rebuilt on the next read; writes are rare, so this is cheap
    return coerced


def reset(db_path, keys, updated_by=None):
    """Delete the override rows for `keys`, so they fall back to registry defaults.
    Returns the keys that actually had an override."""
    keys = [k for k in keys if k in BY_KEY]
    if not keys:
        return []
    with _state_lock:
        with get_conn(db_path) as conn:
            placeholders = ",".join("?" for _ in keys)
            existing = [
                r["key"] for r in conn.execute(
                    f"SELECT key FROM settings WHERE key IN ({placeholders})", keys)
            ]
            conn.execute(f"DELETE FROM settings WHERE key IN ({placeholders})", keys)
        global _state
        _state = None
    return existing


# ---------------------------------------------------------------- schema for the UI

def schema(db_path):
    """Registry + current values, grouped by section -- everything the Settings tab
    needs to render itself. The UI builds its form entirely from this, which is what
    lets a new registry entry appear with no JS or HTML change."""
    values = _current(db_path)["values"]
    sections = []
    for name in SECTIONS:
        fields = []
        for setting in REGISTRY:
            if setting.section != name:
                continue
            value = values.get(setting.key, setting.default)
            fields.append({
                "key": setting.key,
                "label": setting.label,
                "type": setting.type,
                "value": value,
                "default": setting.default,
                "is_default": value == setting.default,
                "min": setting.minimum,
                "max": setting.maximum,
                "unit": setting.unit,
                "help": setting.help,
                "choices": _resolve_choices(setting, db_path),
                "agent": setting.agent,
            })
        if fields:
            sections.append({"name": name, "label": _SECTION_LABELS[name], "fields": fields})
    return {"sections": sections}


_SECTION_LABELS = {
    "computer": "Computer",
    "hub": "Hub",
    "data": "Data & Retention",
    "metrics": "History Metrics",
    "fleet": "Fleet",
    "deploy": "Package Deployment",
    "backup": "Backups",
}


def _resolve_choices(setting, db_path):
    """Choices may be a static list or a callable(db_path) that discovers them at
    request time (the sensor list comes from what machines are actually reporting).
    A discovery failure must not take the whole Settings page down with it."""
    if setting.choices is None:
        return None
    if callable(setting.choices):
        try:
            return list(setting.choices(db_path))
        except Exception:
            return []
    return list(setting.choices)


# ---------------------------------------------------------------- agent config channel

def agent_config(db_path):
    """The subset of settings shipped to agents over the authenticated heartbeat.

    Only agent=True registry entries -- operational tuning, never trust roots and never
    secrets. See the module docstring: an agent must ignore anything that would redirect
    where it gets its code or which key verifies it, and the C# side enforces that with
    an allow-list rather than trusting this to stay honest.
    """
    values = _current(db_path)["values"]
    return {s.key: values.get(s.key, s.default) for s in REGISTRY if s.agent}


def agent_config_version(db_path):
    """Short content hash of agent_config(). Agents send back the version they hold and
    the hub ships config only when it differs, so the steady-state 10-second heartbeat
    stays a two-field response.

    Content-derived rather than a counter: it is stateless (nothing to keep in sync
    across a hub restart or a DB restore), and a change that is made and then reverted
    hashes back to the original, so agents that never observed the intermediate state
    don't re-apply anything. A counter would tick twice and churn the whole fleet.
    """
    return _current(db_path)["agent_version"]


def _agent_version_for(values):
    payload = {s.key: values.get(s.key, s.default) for s in REGISTRY if s.agent}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
