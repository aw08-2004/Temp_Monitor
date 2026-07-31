"""BIOS/firmware settings inventory (roadmap #9).

The hub-side half of "what is this machine's firmware configured to do". An agent enumerates
its own BIOS settings through whichever vendor interface its hardware exposes and reports
them on the heartbeat; this module stores that report and answers questions about it.

**Vendor dispatch belongs on the agent, not here.** Dell, HP and Lenovo each expose a
different WMI namespace and a different attribute vocabulary, and only the machine itself can
say which one answers. What crosses the wire is therefore already normalised into one shape --
`{name, value, kind, possible_values, read_only}` -- and the vendor is carried alongside as a
label, not as a schema selector. The hub never branches on it.

**Three outcomes, not two.** `supported` (we read N attributes), `unsupported` (this hardware
has no manageable BIOS -- a VM, a whitebox), and `error` (there is an interface and it failed).
Collapsing the last two is the failure the roadmap called out: every VM in the fleet would show
a red error forever, and a real failure would be indistinguishable from a machine that was
never going to work. They are separate stored states with separate UI.

**Setting names stay per-vendor in v1.** No alias layer maps Dell's `WakeOnLan` onto HP's
`Wake On LAN` -- a mapping guessed from documentation is how the wrong attribute gets written
on the third vendor. What is shown is what the machine calls it.

Written from the heartbeat, so the whole ingest path is trimmed, type-checked and
non-raising: a malformed report costs a stale inventory, never a heartbeat. A heartbeat that
500s takes the machine offline fleet-wide, which is a much worse outcome than an out-of-date
attribute list.

Kept free of Flask so it can be unit-tested in isolation; bios_web.py wires HTTP on top.
"""
import json
import sqlite3
import time

# ---------------------------------------------------------------- support states
#: The machine enumerated its firmware settings.
SUPPORT_SUPPORTED = "supported"
#: No vendor interface is present at all -- a VM, a whitebox, an unmanaged board. Not an error,
#: and deliberately not rendered as one: this is the permanent, correct state for that hardware.
SUPPORT_UNSUPPORTED = "unsupported"
#: An interface exists and reading it failed. This one IS worth an operator's attention.
SUPPORT_ERROR = "error"

SUPPORT_STATES = frozenset({SUPPORT_SUPPORTED, SUPPORT_UNSUPPORTED, SUPPORT_ERROR})

# ---------------------------------------------------------------- attribute kinds
#: An enumeration with a known set of accepted values (the common case: Enabled/Disabled).
KIND_ENUM = "enum"
#: Free text -- an asset tag field, an owner string.
KIND_STRING = "string"
#: An integer with optional bounds.
KIND_INTEGER = "integer"
#: The agent could not classify it. Readable, and writable only as an opaque string.
KIND_UNKNOWN = "unknown"

ATTRIBUTE_KINDS = frozenset({KIND_ENUM, KIND_STRING, KIND_INTEGER, KIND_UNKNOWN})

# ---------------------------------------------------------------- ingest caps
# A real BIOS exposes 100-400 attributes; Lenovo's list is the longest and still well under
# this. The caps exist so a misbehaving or hostile agent cannot grow the row without bound --
# this lands in the database the hub backs up, so an unbounded blob is a storage problem twice.
MAX_ATTRIBUTES = 1000
MAX_NAME_CHARS = 200
MAX_VALUE_CHARS = 512
MAX_POSSIBLE_VALUES = 64
MAX_ERROR_CHARS = 500


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_bios_db(db_path):
    """Create the BIOS inventory table if absent. Idempotent -- safe to call on every hub
    start next to app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_bios (
                machine        TEXT PRIMARY KEY,
                support        TEXT NOT NULL,
                vendor         TEXT NOT NULL DEFAULT '',
                interface      TEXT NOT NULL DEFAULT '',
                bios_version   TEXT NOT NULL DEFAULT '',
                password_set   INTEGER,
                error          TEXT NOT NULL DEFAULT '',
                settings_json  TEXT NOT NULL DEFAULT '[]',
                reported_at    INTEGER NOT NULL
            )
            """
        )


# --------------------------------------------------------------------------- ingest
def _clean_attribute(raw):
    """Normalise one reported attribute, or None if it is unusable.

    A nameless attribute is dropped rather than stored under a placeholder: the name is the
    identity a future `set_bios_settings` writes against, so an attribute nobody can name is
    an attribute nobody can set.
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:MAX_NAME_CHARS]
    if not name:
        return None

    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in ATTRIBUTE_KINDS:
        kind = KIND_UNKNOWN

    possible = []
    for value in list(raw.get("possible_values") or [])[:MAX_POSSIBLE_VALUES]:
        # `None` is skipped rather than stringified: str(None) is "None", which would offer an
        # operator a choice spelled like a real one and write the literal word if picked.
        if value is None:
            continue
        text = str(value).strip()[:MAX_VALUE_CHARS]
        if text and text not in possible:
            possible.append(text)

    return {
        "name": name,
        "value": str(raw.get("value") if raw.get("value") is not None else "")[:MAX_VALUE_CHARS],
        "kind": kind,
        "possible_values": possible,
        "read_only": bool(raw.get("read_only")),
        "display_name": str(raw.get("display_name") or "").strip()[:MAX_NAME_CHARS],
    }


def record_inventory(db_path, machine, payload):
    """Store a machine's reported BIOS settings. Returns True if something was stored.

    Never raises on payload shape -- see the module docstring. The one thing that IS enforced
    is the support state: an unrecognised state is treated as `error` rather than stored
    verbatim, so the value the console branches on can only ever be one of three things.
    """
    if not isinstance(payload, dict) or not machine:
        return False

    support = str(payload.get("support") or "").strip().lower()
    if support not in SUPPORT_STATES:
        # An agent reporting something we do not understand HAS an interface and is failing to
        # describe it -- that is the error case, not the unsupported one. Guessing "unsupported"
        # here would hide a real fault behind the state that is never shown to anyone.
        support = SUPPORT_ERROR

    settings_list = []
    if support == SUPPORT_SUPPORTED:
        for raw in list(payload.get("settings") or [])[:MAX_ATTRIBUTES]:
            attribute = _clean_attribute(raw)
            if attribute is not None:
                settings_list.append(attribute)
        settings_list.sort(key=lambda a: a["name"].casefold())
        if not settings_list:
            # "Supported, and here are zero attributes" is not a support claim anyone can act
            # on; the interface answered with nothing, which is a failure to enumerate.
            support = SUPPORT_ERROR

    password_set = payload.get("password_set")
    if password_set is not None:
        password_set = 1 if password_set else 0

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO machine_bios(machine, support, vendor, interface, bios_version, "
            "                         password_set, error, settings_json, reported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET support = excluded.support, "
            "vendor = excluded.vendor, interface = excluded.interface, "
            "bios_version = excluded.bios_version, password_set = excluded.password_set, "
            "error = excluded.error, settings_json = excluded.settings_json, "
            "reported_at = excluded.reported_at",
            (
                machine,
                support,
                str(payload.get("vendor") or "")[:MAX_NAME_CHARS],
                str(payload.get("interface") or "")[:MAX_NAME_CHARS],
                str(payload.get("bios_version") or "")[:MAX_NAME_CHARS],
                password_set,
                str(payload.get("error") or "")[:MAX_ERROR_CHARS],
                json.dumps(settings_list),
                int(time.time()),
            ),
        )
    return True


# --------------------------------------------------------------------------- read
def get_inventory(db_path, machine):
    """The last reported BIOS inventory for `machine`.

    A machine that has never reported (an agent older than this feature, or one that has not
    heartbeated yet) is `support: null` -- deliberately a fourth value at the read boundary
    and not one of the three stored states. "We have not been told" is not the same claim as
    "this hardware cannot do it", and an unknown rendered as `unsupported` would quietly
    write off every machine in the fleet the day before the agent release lands.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT support, vendor, interface, bios_version, password_set, error, "
            "       settings_json, reported_at FROM machine_bios WHERE machine = ?",
            (machine,),
        ).fetchone()
    if row is None:
        return {
            "support": None, "vendor": "", "interface": "", "bios_version": "",
            "password_set": None, "error": "", "settings": [], "reported_at": None,
        }
    try:
        settings_list = json.loads(row["settings_json"])
    except (TypeError, ValueError):
        settings_list = []
    return {
        "support": row["support"],
        "vendor": row["vendor"],
        "interface": row["interface"],
        "bios_version": row["bios_version"],
        "password_set": None if row["password_set"] is None else bool(row["password_set"]),
        "error": row["error"],
        "settings": settings_list,
        "reported_at": row["reported_at"],
    }


def forget_machine(db_path, machine):
    """Drop a machine's inventory. Called from the machine-delete path, on the same
    lifecycle-hook discipline as permissions.forget_machine -- a deleted machine that left its
    firmware inventory behind would hand the next machine to take that hostname somebody
    else's attribute list."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_bios WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move an inventory during a duplicate-serial merge. The surviving row wins: the merge
    target has already reported for itself, and its own reading is newer than the one being
    folded in."""
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM machine_bios WHERE machine = ?", (new_machine,)
        ).fetchone()
        if existing is not None:
            conn.execute("DELETE FROM machine_bios WHERE machine = ?", (old_machine,))
        else:
            conn.execute("UPDATE machine_bios SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
