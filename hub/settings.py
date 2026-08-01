"""Operator-settable configuration -- the knobs behind the Settings tab.

Every operational tunable used to be a module-level constant in app.py or fleet.py,
so changing a retention window or an offline threshold meant editing code and
restarting the service. This module moves them into the DB, behind one registry.

THE REGISTRY IS THE SINGLE SOURCE OF TRUTH. One `_s(...)` line in REGISTRY below
drives all of: the default value, type coercion, range validation, the JSON schema
the API serves, and the form field the Settings tab renders. Nothing else in the
codebase enumerates settings. Adding a knob is one line here plus its words in
`locales/en.json` under the same key -- no JS, no HTML, no endpoint change. Keep it
that way; the moment a second place lists setting keys, they start drifting.

The WORDS (label, help, placeholder, unit) live in the translation catalogs rather
than in the tuples, so the Settings page reads in the operator's language. See the
block above REGISTRY for why, and what it costs.

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

# Only for LANGUAGE_CODES, so hub.default_language's choices cannot drift from the
# catalogs actually shipped in locales/. i18n imports nothing from here, so there is no
# cycle.
import i18n

# ---------------------------------------------------------------- the registry

Setting = namedtuple("Setting", [
    "key",       # "data.retention_days" -- the section is the prefix, by convention
    "section",   # "computer" | "hub" | "data" | "fleet"
    "type",      # "int"|"float"|"bool"|"str"|"enum"|"str_list"|"path_list"|"url_list"
    "default",   # MUST equal the constant this replaced
    "minimum",   # numeric bounds; None for non-numeric types
    "maximum",
    "unit",      # a UNIT SLUG ("seconds", "celsius") resolved through settings.unit.*
    "choices",   # for "enum"/"str_list": a list, or callable(db_path) -> list
    "agent",     # True => shipped to agents over the heartbeat config channel
])


def _s(key, section, type, default, *, minimum=None, maximum=None,
       unit="", choices=None, agent=False):
    return Setting(key, section, type, default, minimum, maximum, unit, choices, agent)


# ------------------------------------------------------- where the words live
#
# A setting's LABEL, HELP and PLACEHOLDER are in the translation catalogs, under
# `settings.field.<key>.{label,help,placeholder}`, and its UNIT under
# `settings.unit.<slug>`. They were literals in the tuples above while the console was
# English-only; roadmap #7 moved them because two homes for one string is how a catalog
# rots, and `en.json` is the schema.
#
# What that costs is real and worth naming: reading REGISTRY no longer tells you what a
# knob MEANS, only what shape it is. The section comments carry the design notes, and the
# prose is one grep away in `locales/en.json` under the same key. What it buys is that
# the Settings page reads in the operator's language, and that a knob added without words
# fails `tests/test_i18n.py` instead of shipping a page captioned
# `settings.field.foo.bar.label`.

FIELD_TEXT_KEY = "settings.field"
UNIT_TEXT_KEY = "settings.unit"
SECTION_TEXT_KEY = "settings.section"
CHOICE_TEXT_KEY = "settings.choice"
ERROR_TEXT_KEY = "settings.error"


def field_label(setting, lang=None):
    """The setting's label in `lang` (default: the request in flight, else English)."""
    return i18n.translate(f"{FIELD_TEXT_KEY}.{setting.key}.label", lang or i18n.current())


def field_help(setting, lang=None):
    """The help text, or "" for a setting that has none.

    Empty rather than the key: help is genuinely optional (a boolean whose label says it
    all needs none), so an absent entry is not the error a missing label is, and the UI
    already renders nothing for an empty string.
    """
    lang = lang or i18n.current()
    key = f"{FIELD_TEXT_KEY}.{setting.key}.help"
    text = i18n.translate(key, lang)
    return "" if text == key else text


def field_placeholder(setting, lang=None):
    lang = lang or i18n.current()
    key = f"{FIELD_TEXT_KEY}.{setting.key}.placeholder"
    text = i18n.translate(key, lang)
    return "" if text == key else text


def unit_label(setting, lang=None):
    if not setting.unit:
        return ""
    return i18n.translate(f"{UNIT_TEXT_KEY}.{setting.unit}", lang or i18n.current())


def choice_label(setting, choice, lang=None):
    """A label for one enum choice, falling back to the choice itself.

    The fallback is load-bearing rather than defensive: `backup.hub_destination`'s
    choices are destination ids an operator named, and translating those is neither
    possible nor ours to do -- so an enum with no catalog block shows its own values,
    which is the right answer for every data-driven vocabulary.
    """
    lang = lang or i18n.current()
    key = f"{CHOICE_TEXT_KEY}.{setting.key}.{choice}"
    text = i18n.translate(key, lang)
    return choice if text == key else text


def _error(key, lang=None, **params):
    return i18n.translate(f"{ERROR_TEXT_KEY}.{key}", lang or i18n.current(), **params)


# Default for computer.primary_sensor_preference. Mirrors SensorReader.cs's
# PreferredSensors exactly -- best first,
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
    _s("computer.primary_sensor_preference", "computer", "str_list",
       DEFAULT_SENSOR_PREFERENCE, agent=True),

    # ---------------- Hub: thresholds and internals ----------------
    _s("hub.high_temp_threshold", "hub", "int", 85, minimum=40, maximum=120, unit="celsius"),
    _s("hub.high_temp_avg_window_seconds", "hub", "int", 300, minimum=60, maximum=3600,
       unit="seconds"),
    _s("hub.low_load_threshold", "hub", "int", 40, minimum=0, maximum=100, unit="percent"),
    _s("hub.live_status_cache_seconds", "hub", "int", 600, minimum=60, maximum=86400,
       unit="seconds"),
    _s("hub.live_default_window_hours", "hub", "int", 3, minimum=1, maximum=168,
       unit="hours"),
    _s("hub.auto_update", "hub", "bool", None),
    # Ships as AUTO, not "en". A concrete default here is indistinguishable from an admin
    # deliberately choosing English, which made i18n.resolve's Accept-Language step
    # unreachable on every untouched hub -- see the note on i18n.AUTO.
    _s("hub.default_language", "hub", "enum", i18n.AUTO,
       choices=[i18n.AUTO] + list(i18n.LANGUAGE_CODES)),

    # ---------------- Data & retention ----------------
    _s("data.retention_days", "data", "int", 30, minimum=1, maximum=3650, unit="days"),
    _s("data.prune_interval_seconds", "data", "int", 86400, minimum=300, maximum=604800,
       unit="seconds"),
    _s("data.ingest_max_backdate_days", "data", "int", 30, minimum=1, maximum=3650,
       unit="days"),
    _s("data.command_output_retention_seconds", "data", "int", 86400, minimum=3600,
       maximum=2592000, unit="seconds"),

    # ---------------- History metrics: which sensors are recorded to history ----------------
    # One on/off toggle per chartable metric on the per-machine History dashboard. Off means
    # the hub stops recording that metric into new readings (stored NULL) -- "what sensor
    # should be read". collect_network is additionally agent=True: it tells the agent whether
    # to collect the network sensor category at all (see the agent's RuntimeConfig allow-list).
    # Temperature has no toggle -- it is the core metric and drives high-temperature alerts,
    # so it is always recorded.
    _s("metrics.collect_cpu_load", "metrics", "bool", True),
    _s("metrics.collect_memory", "metrics", "bool", True),
    _s("metrics.collect_gpu", "metrics", "bool", True),
    _s("metrics.collect_disk", "metrics", "bool", True),
    _s("metrics.collect_disk_io", "metrics", "bool", True),
    _s("metrics.collect_network", "metrics", "bool", True, agent=True),

    # ---------------- Fleet: liveness and command timings ----------------
    # These next two are different windows that operators WILL confuse, so the labels
    # describe what you observe rather than what the code does. Keep them adjacent.
    _s("fleet.dashboard_online_window_seconds", "fleet", "int", 120, minimum=30,
       maximum=3600, unit="seconds"),
    _s("fleet.offline_after_seconds", "fleet", "int", 90, minimum=30, maximum=3600,
       unit="seconds"),
    _s("fleet.command_ttl_seconds", "fleet", "int", 900, minimum=60, maximum=86400,
       unit="seconds"),

    # ---------------- Deploy: package pushes ----------------
    # Retry defaults are per-deployment values the schedule form pre-fills; changing them
    # here does NOT alter deployments already created, which carry their own copy. That
    # is deliberate -- a retry policy an operator agreed to when scheduling shouldn't
    # change under them because someone edited a default mid-push.
    _s("deploy.default_max_attempts", "deploy", "int", 3, minimum=1, maximum=10),
    _s("deploy.default_retry_backoff_seconds", "deploy", "int", 900, minimum=60,
       maximum=86400, unit="seconds"),
    _s("deploy.max_upload_mb", "deploy", "int", 512, minimum=1, maximum=4096, unit="mb"),
    _s("deploy.scheduler_interval_seconds", "deploy", "int", 30, minimum=10, maximum=3600,
       unit="seconds"),

    # ---------------- Backups: the hub's own database, offsite ----------------
    # Credentials are deliberately ABSENT from this registry -- they live encrypted in a
    # sidecar file (see backups.py's secret store). Settings are rendered into a form,
    # returned wholesale by as_dict(), and partly shipped to agents by agent_config();
    # an S3 secret key belongs in none of those places. What lives here is only the
    # schedule, and which destination it aims at.
    _s("backup.hub_enabled", "backup", "bool", False),
    _s("backup.hub_destination", "backup", "str", ""),
    _s("backup.hub_interval_hours", "backup", "int", 24, minimum=1, maximum=720,
       unit="hours"),
    _s("backup.hub_keep_generations", "backup", "int", 14, minimum=1, maximum=365,
       unit="generations"),

    # ---------------- Backups: per-PC files ----------------
    # Edited on the Backups page's "Backup Settings" tab, which offers the token
    # reference and a live preview against a real machine. They live in the registry all
    # the same, so they get the same validation, audit trail and reset behaviour as
    # everything else -- the tab is a better editor, not a second store.
    _s("backup.files_enabled", "backup", "bool", False),
    _s("backup.files_destination", "backup", "str", ""),
    _s("backup.files_include", "backup", "path_list",
       ["%Desktop%", "%Documents%", "%Pictures%", "%Favorites%"]),
    _s("backup.files_exclude", "backup", "path_list",
       ["*.tmp", "~$*", "thumbs.db", "**\\AppData\\Local\\Temp\\**",
        "**\\node_modules\\**", "**\\.git\\**", "*.iso", "*.vhdx", "*.vmdk"]),
    _s("backup.files_interval_hours", "backup", "int", 24, minimum=1, maximum=720,
       unit="hours"),
    _s("backup.files_full_every", "backup", "int", 7, minimum=1, maximum=90, unit="runs"),
    _s("backup.files_keep_chains", "backup", "int", 4, minimum=1, maximum=52, unit="chains"),
    _s("backup.files_max_file_mb", "backup", "int", 2048, minimum=1, maximum=102400,
       unit="mb"),
    _s("backup.files_max_set_gb", "backup", "int", 100, minimum=1, maximum=10240, unit="gb"),
    _s("backup.files_use_vss", "backup", "bool", True),
    _s("backup.files_max_concurrent", "backup", "int", 3, minimum=0, maximum=100,
       unit="pcs"),

    # ---------------- Remote view/control (roadmap #2) ----------------
    _s("remote.enabled", "remote", "bool", True),
    _s("remote.consent_mode", "remote", "enum", "unattended",
       choices=["unattended", "attended"]),
    _s("remote.session_ttl_seconds", "remote", "int", 4 * 60 * 60, minimum=300,
       maximum=86400, unit="seconds"),
    _s("remote.turn_ttl_seconds", "remote", "int", 600, minimum=60, maximum=86400,
       unit="seconds"),
    _s("remote.stun_urls", "remote", "url_list", []),
    _s("remote.turn_urls", "remote", "url_list", []),

    # ---------------- Active Directory (roadmap #4) ----------------
    # Entirely opt-in: with `enabled` off nothing runs, ldap3 is never imported, and the
    # AD columns on machine_info stay NULL. The bind PASSWORD is deliberately absent from
    # this registry -- it is a credential, and the settings table is readable by anyone
    # holding manage_settings and is dumped into the hub-database backup. It lives in .env
    # as DIRECTORY_BIND_PASSWORD, the same rule backups.py applies to destination secrets.
    _s("directory.enabled", "directory", "bool", False),
    _s("directory.server", "directory", "str", ""),
    _s("directory.base_dn", "directory", "str", ""),
    _s("directory.bind_dn", "directory", "str", ""),
    _s("directory.computer_filter", "directory", "str", "(objectClass=computer)"),
    _s("directory.sync_interval_minutes", "directory", "int", 60, minimum=5,
       maximum=10080, unit="minutes"),
    _s("directory.page_size", "directory", "int", 500, minimum=50, maximum=5000),
    _s("directory.timeout_seconds", "directory", "int", 15, minimum=5, maximum=300,
       unit="seconds"),
    _s("directory.alert_on_unmatched", "directory", "bool", True),
    _s("directory.tls_verify", "directory", "bool", True),
    _s("directory.allow_insecure", "directory", "bool", False),

    # ---------------- Firmware updates (roadmap #9) ----------------
    # The two timeouts are wall-clock windows around an operation the hub cannot observe:
    # the flash completes during POST, so the only evidence it worked is the machine coming
    # back reporting a new BIOS version. They are deliberately far apart -- a vendor tool
    # that hangs is a couple of hours, while a machine flashed on a Friday evening should
    # not be written off before somebody switches it on again on Monday.
    #
    # The power preconditions are enforced on the AGENT, at the moment of the flash, and
    # ride down with the payload rather than through agent_config: a battery reading taken
    # when the config was last pushed says nothing about the battery now.
    _s("firmware.max_upload_mb", "firmware", "int", 128, minimum=1, maximum=4096,
       unit="mb"),
    _s("firmware.scheduler_interval_seconds", "firmware", "int", 60, minimum=10,
       maximum=3600, unit="seconds"),
    _s("firmware.flashing_timeout_seconds", "firmware", "int", 2 * 3600, minimum=300,
       maximum=86400, unit="seconds"),
    _s("firmware.confirm_timeout_seconds", "firmware", "int", 24 * 3600, minimum=600,
       maximum=7 * 86400, unit="seconds"),
    _s("firmware.require_ac_power", "firmware", "bool", True),
    _s("firmware.min_battery_percent", "firmware", "int", 30, minimum=0, maximum=100,
       unit="percent"),
)

BY_KEY = {s.key: s for s in REGISTRY}
SECTIONS = ("computer", "hub", "data", "metrics", "fleet", "deploy", "backup", "remote",
            "directory", "firmware")

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


# Registry keys that have been renamed, as (old key, new key). Only overridden settings are
# stored as rows, and _build() SKIPS a row whose key is not in the registry -- so without
# this an upgrade would silently revert a customised value to the shipped default while the
# operator's real choice sat unreachable in the table.
RENAMED_KEYS = (
    ("hub.overheat_threshold", "hub.high_temp_threshold"),
    ("hub.overheat_avg_window_seconds", "hub.high_temp_avg_window_seconds"),
)


def _migrate_renamed_keys(conn):
    """Move override rows onto their current key. Returns True if anything moved.

    Idempotent: there are no old rows on a fresh DB or on a second run. UPDATE OR IGNORE
    then DELETE, so if a row already exists under the NEW key (an operator who saved the
    setting after upgrading) their newer value wins and the stale row is dropped rather
    than colliding on the primary key.
    """
    moved = False
    for old_key, new_key in RENAMED_KEYS:
        cur = conn.execute("UPDATE OR IGNORE settings SET key=? WHERE key=?",
                           (new_key, old_key))
        moved = moved or cur.rowcount > 0
        cur = conn.execute("DELETE FROM settings WHERE key=?", (old_key,))
        moved = moved or cur.rowcount > 0
    return moved


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
        moved = _migrate_renamed_keys(conn)
    # Outside the connection block: the cache may already have been built from the
    # pre-migration rows in this process (tests re-init per case), and invalidate() takes
    # its own lock.
    if moved:
        invalidate()


# ---------------------------------------------------------------- coercion & validation

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def coerce_and_validate(setting, raw, lang=None):
    """Coerce `raw` to the setting's declared type and enforce its bounds.

    Raises ValueError naming the setting -- the message is shown verbatim next to the
    field in the UI, so it has to read like something an operator can act on, and it is
    built in the caller's language for the same reason the label beside it is. `lang`
    defaults to the request in flight (i18n.current()), which is English outside one.

    JSON is sloppy about types (a number arrives as "85" from some clients, a bool as
    1), so coerce rather than reject: the operator typed a valid value and shouldn't
    be told otherwise because of a transport detail.
    """
    lang = lang or i18n.current()
    label = field_label(setting, lang)

    def bad(key, **params):
        return ValueError(f"{label}: {_error(key, lang, **params)}")

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
        raise bad("expected_bool", value=repr(raw))

    if setting.type in ("int", "float"):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise bad("required")
        try:
            value = int(raw) if setting.type == "int" else float(raw)
        except (TypeError, ValueError):
            raise bad("expected_whole_number" if setting.type == "int"
                      else "expected_number", value=repr(raw))
        if setting.minimum is not None and value < setting.minimum:
            raise bad("min", min=f"{setting.minimum}{_unit_suffix(setting, lang)}")
        if setting.maximum is not None and value > setting.maximum:
            raise bad("max", max=f"{setting.maximum}{_unit_suffix(setting, lang)}")
        return value

    if setting.type == "str":
        if raw is None:
            return None
        return str(raw).strip()

    if setting.type == "enum":
        text = str(raw).strip()
        allowed = setting.choices if isinstance(setting.choices, (list, tuple)) else None
        if allowed is not None and text not in allowed:
            raise bad("not_a_choice", value=repr(text), choices=", ".join(allowed))
        return text

    if setting.type == "str_list":
        if not isinstance(raw, (list, tuple)):
            raise bad("expected_name_list")
        items = [str(v).strip() for v in raw if str(v).strip()]
        if not items:
            raise bad("needs_entry")
        # Preference lists are matched case-insensitively downstream; normalise here so
        # what is stored is what is matched, and a stray "CPU Package" can't look
        # different from "cpu package" in the UI while behaving identically.
        return [v.lower() for v in items]

    if setting.type == "url_list":
        # STUN/TURN URLs. Deliberately NOT str_list, for the same two reasons path_list
        # isn't: str_list refuses an empty list (but "no STUN servers" is a perfectly good
        # answer on a LAN, and the help text says so), and it lowercases every entry, which
        # is right for sensor names and wrong for anything an operator typed verbatim.
        #
        # The scheme is validated because a typo here fails silently and expensively: a
        # malformed URL is carried all the way into the browser's RTCPeerConnection and the
        # agent's ICE config, where it is skipped without comment, and the only symptom is
        # that sessions stop connecting from some networks.
        if not isinstance(raw, (list, tuple)):
            raise bad("expected_url_list")
        items = []
        for value in raw:
            text = str(value).strip()
            if not text:
                continue
            scheme, sep, rest = text.partition(":")
            if not sep or scheme.lower() not in ("stun", "stuns", "turn", "turns"):
                raise bad("bad_url_scheme", value=repr(text))
            if not rest.strip():
                raise bad("url_without_host", value=repr(text))
            items.append(text)
        return items

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
            raise bad("expected_path_list")
        import backup_paths
        kind = "exclude" if setting.key.endswith("_exclude") else "include"
        try:
            return backup_paths.validate_patterns(raw, kind=kind)
        except ValueError as e:
            raise ValueError(f"{label}: {e}")

    raise bad("unsupported_type", type=repr(setting.type))


def _unit_suffix(setting, lang=None):
    unit = unit_label(setting, lang)
    return f" {unit}" if unit else ""


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

def schema(db_path, lang=None):
    """Registry + current values, grouped by section -- everything the Settings tab
    needs to render itself. The UI builds its form entirely from this, which is what
    lets a new registry entry appear with no JS or HTML change.

    All the words are resolved HERE, in `lang` (default: the request in flight), so the
    browser receives finished text rather than keys. The alternative -- ship keys and let
    the page translate -- would build every key by concatenating the setting key, which
    the literal-key scan in tests/test_i18n.py cannot see, so a knob added without catalog
    entries would caption itself `settings.field.x.label` with nothing to catch it.

    `choice_labels` is a parallel map rather than a replacement for `choices`: the values
    are what gets SAVED and must stay verbatim, while the labels are only shown.
    """
    lang = lang or i18n.current()
    values = _current(db_path)["values"]
    sections = []
    for name in SECTIONS:
        fields = []
        for setting in REGISTRY:
            if setting.section != name:
                continue
            value = values.get(setting.key, setting.default)
            choices = _resolve_choices(setting, db_path)
            fields.append({
                "key": setting.key,
                "label": field_label(setting, lang),
                "type": setting.type,
                "value": value,
                "default": setting.default,
                "is_default": value == setting.default,
                "min": setting.minimum,
                "max": setting.maximum,
                "unit": unit_label(setting, lang),
                "help": field_help(setting, lang),
                "choices": choices,
                "choice_labels": {c: choice_label(setting, c, lang)
                                  for c in (choices or [])},
                "agent": setting.agent,
                "placeholder": field_placeholder(setting, lang),
            })
        if fields:
            sections.append({
                "name": name,
                "label": i18n.translate(f"{SECTION_TEXT_KEY}.{name}", lang),
                "fields": fields,
            })
    return {"sections": sections}


_SECTION_LABELS = {
    "computer": "Computer",
    "hub": "Hub",
    "data": "Data & Retention",
    "metrics": "History Metrics",
    "fleet": "Fleet",
    "deploy": "Package Deployment",
    "backup": "Backups",
    "remote": "Remote Control",
    "directory": "Active Directory",
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
