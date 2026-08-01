"""BIOS/firmware settings inventory and writes (roadmap #9).

The hub-side half of "what is this machine's firmware configured to do", and of changing it.
An agent enumerates its own BIOS settings through whichever vendor interface its hardware
exposes and reports them on the heartbeat; this module stores that report, answers questions
about it, and turns an operator's chosen values into a verified change.

**A change is confirmed by re-reading the attribute, never by assuming a reboot.** Whether a
write takes effect immediately is per vendor *and* per setting -- Dell applies many attributes
live and holds others until POST, and HP and Lenovo each draw that line somewhere else again.
So a per-vendor "requires reboot" table would be the cross-vendor alias map in another
costume: guessed from documentation, wrong on the third vendor, and wrong per-attribute even
where the vendor is right. Instead the agent writes, re-reads, and reports what the firmware
now says; `classify_result` compares. If the new value is there the change is APPLIED and
nobody is told to reboot. If the old value is still there and the write itself did not fail
the change is PENDING_REBOOT, and only then does the console ask for a restart. This needs no
vendor knowledge, is correct per-attribute for free, and degrades honestly on hardware nobody
has tested -- the worst case is "pending" on a machine that applied it live, which the next
inventory report corrects.

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
import uuid

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

# ---------------------------------------------------------------- change states
#: Written, awaiting the agent. The command exists; nothing has touched the firmware yet.
CHANGE_PENDING = "pending"
#: The agent has fetched the payload and is writing.
CHANGE_RUNNING = "running"
#: Every attribute was written AND re-read back at the requested value. Done, no reboot.
CHANGE_APPLIED = "applied"
#: Written without error, but the firmware still reports the old value -- it takes effect at
#: the next POST. This is the ONLY state in which the console asks for a restart.
CHANGE_PENDING_REBOOT = "pending_reboot"
#: Some attributes landed and some did not. Deliberately not folded into `failed`: an
#: operator who changed six settings needs to know which two did not take.
CHANGE_PARTIAL = "partial"
#: Nothing landed.
CHANGE_FAILED = "failed"

CHANGE_STATES = frozenset({
    CHANGE_PENDING, CHANGE_RUNNING, CHANGE_APPLIED, CHANGE_PENDING_REBOOT,
    CHANGE_PARTIAL, CHANGE_FAILED,
})
#: States a change can still move out of. Used to stop a second write racing the first.
CHANGE_OPEN_STATES = frozenset({CHANGE_PENDING, CHANGE_RUNNING})

# ---------------------------------------------------------------- per-attribute outcomes
OUTCOME_APPLIED = "applied"
OUTCOME_PENDING_REBOOT = "pending_reboot"
OUTCOME_FAILED = "failed"
#: The write reported no error and the re-read came back as neither the old value nor the new
#: one. Its own outcome rather than a guess in either direction: some firmware normalises what
#: you write ("Enable" stored as "Enabled"), and some silently substitutes something else.
#: Calling that "applied" would confirm a change nobody made.
OUTCOME_UNKNOWN = "unknown"

#: How many changes to keep per machine. This is an audit-adjacent history an operator reads
#: after the fact ("who turned Secure Boot off"), not a log -- the audit trail is the log.
MAX_CHANGES_PER_MACHINE = 50
MAX_CHANGES_PER_REQUEST = 64

# ---------------------------------------------------------------- BIOS setup password
# Stored through backups.store_secret -- the same .env-master-key-wrapped sidecar the backup
# destinations use, keyed by an opaque id. Not in the `settings` table, which is rendered into
# a form, dumped by as_dict() and partly shipped to agents in agent_config().
#
# Fleet-wide with per-machine override, because a helpdesk images a fleet with one password
# and then has exceptions. The per-machine entry wins outright when present; there is no
# merging to do with a single opaque string.
SECRET_ID_FLEET = "bios.password"


def secret_id_for(machine):
    """The secret-store id for one machine's BIOS password override.

    The machine name is part of the id and therefore part of the AAD, so a password blob
    copied between machines fails to decrypt rather than being sent to the wrong PC.
    """
    return f"bios.password.machine:{machine}"


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
        # One row per set_bios_settings request. Separate from machine_bios rather than a
        # pending-value column on each attribute, for three reasons: a change is a thing an
        # operator DID (with an author and a time) and the inventory is a thing the machine
        # reported; the next heartbeat overwrites the whole inventory blob, which would take
        # per-attribute pending markers with it; and a history answers "who turned Secure Boot
        # off in March", which a current-state column cannot.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bios_changes (
                id             TEXT PRIMARY KEY,
                machine        TEXT NOT NULL,
                status         TEXT NOT NULL,
                requested_by   TEXT NOT NULL DEFAULT '',
                requested_at   INTEGER NOT NULL,
                finished_at    INTEGER,
                command_id     TEXT NOT NULL DEFAULT '',
                changes_json   TEXT NOT NULL DEFAULT '[]',
                results_json   TEXT NOT NULL DEFAULT '[]',
                error          TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bios_changes_machine "
                     "ON bios_changes(machine, requested_at DESC)")


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
    """Drop a machine's inventory and change history. Called from the machine-delete path, on
    the same lifecycle-hook discipline as permissions.forget_machine -- a deleted machine that
    left its firmware inventory behind would hand the next machine to take that hostname
    somebody else's attribute list.

    The stored BIOS password override is NOT dropped here: it lives in the secret file, not
    the database, and deleting it needs the master key this module is never handed. The caller
    (bios_web) drops it, where the key is in scope.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_bios WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM bios_changes WHERE machine = ?", (machine,))


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
        # The change HISTORY always moves, even when the inventory did not: a merge folds two
        # records of one physical machine together, and "who changed this PC's boot order" is
        # the same question afterwards. Unlike the inventory there is no newer-wins conflict
        # to resolve -- both sides are just rows, and both belong to the survivor.
        conn.execute("UPDATE bios_changes SET machine = ? WHERE machine = ?",
                     (new_machine, old_machine))


# --------------------------------------------------------------------------- writes
class ChangeRejected(ValueError):
    """A requested change the hub refuses to send. Its own type so the web layer can answer
    400 for it while a genuine bug still becomes a 500."""


def _attribute_index(inventory):
    return {a["name"].casefold(): a for a in inventory.get("settings") or []}


def _same_value(left, right):
    """Firmware value comparison, which is not string equality.

    Case and surrounding space vary between what you write and what comes back on every
    vendor -- writing "Enable" and reading "enable" is the normal case, not a mismatch. Being
    strict here would report PENDING_REBOOT on a change that applied perfectly, and then tell
    an operator to restart a machine for nothing.
    """
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def validate_changes(inventory, requested):
    """Resolve an operator's chosen values against the machine's own reported attributes.

    Everything here is checked against the INVENTORY, not against a vendor rulebook: v1 maps
    no cross-vendor vocabulary, so the only thing that knows whether `WakeOnLan` exists and
    what it accepts is the machine that reported it. A hub-side rulebook would be the alias
    map by another name.

    Refusals are per attribute and named -- an operator who ticked six settings and got one
    rejection needs to know which. Returns the cleaned list; raises ChangeRejected otherwise.
    """
    if inventory.get("support") != SUPPORT_SUPPORTED:
        raise ChangeRejected("This machine has not reported any writable firmware settings.")
    if not isinstance(requested, list) or not requested:
        raise ChangeRejected("No settings were selected.")
    if len(requested) > MAX_CHANGES_PER_REQUEST:
        raise ChangeRejected(f"At most {MAX_CHANGES_PER_REQUEST} settings can be changed in "
                             f"one request.")

    index = _attribute_index(inventory)
    cleaned = []
    seen = set()
    for raw in requested:
        if not isinstance(raw, dict):
            raise ChangeRejected("Each change must name a setting and a value.")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ChangeRejected("A change was sent with no setting name.")
        attribute = index.get(name.casefold())
        if attribute is None:
            raise ChangeRejected(f"{name!r} is not a setting this machine reported. Re-read "
                                 f"the firmware inventory and try again.")
        # The machine's own spelling wins over the caller's. The name is the identity the
        # write targets, and some vendors are case-sensitive about it.
        name = attribute["name"]
        if name.casefold() in seen:
            raise ChangeRejected(f"{name!r} was given a value twice.")
        seen.add(name.casefold())

        if attribute.get("read_only"):
            raise ChangeRejected(f"{name!r} is read-only on this machine.")

        value = str(raw.get("value") if raw.get("value") is not None else "").strip()
        if not value:
            # Deliberately refused rather than sent through: an empty write means something
            # different on each vendor, and on at least one it clears the attribute.
            raise ChangeRejected(f"{name!r} was given no value.")
        if len(value) > MAX_VALUE_CHARS:
            raise ChangeRejected(f"The value for {name!r} is too long.")

        possible = attribute.get("possible_values") or []
        if attribute.get("kind") == KIND_ENUM and possible:
            match = next((p for p in possible if _same_value(p, value)), None)
            if match is None:
                raise ChangeRejected(
                    f"{value!r} is not one of the values {name!r} accepts "
                    f"({', '.join(possible)}).")
            # Send the firmware's own spelling, not the operator's.
            value = match
        elif attribute.get("kind") == KIND_INTEGER:
            try:
                int(value, 10)
            except ValueError:
                raise ChangeRejected(f"{name!r} takes a whole number.")

        current = attribute.get("value", "")
        if _same_value(current, value):
            # A no-op is refused rather than quietly dropped. Verification works by comparing
            # the re-read against the old value, so a change from X to X has no observable
            # outcome at all -- it would sit in the history as PENDING_REBOOT forever.
            raise ChangeRejected(f"{name!r} is already set to {value!r}.")

        cleaned.append({"name": name, "from": current, "to": value,
                        "kind": attribute.get("kind", KIND_UNKNOWN)})
    return cleaned


def open_change_for(db_path, machine):
    """The machine's in-flight change, if it has one. A second write must not be queued while
    the first is unresolved: the two would race in the firmware, and verification -- which
    reads a single current value per attribute -- could not say which one it was looking at."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bios_changes WHERE machine = ? AND status IN (?, ?) "
            "ORDER BY requested_at DESC, rowid DESC LIMIT 1",
            (machine, CHANGE_PENDING, CHANGE_RUNNING),
        ).fetchone()
    return _change_row(row) if row is not None else None


def create_change(db_path, machine, changes, requested_by):
    """Record a validated change request. Returns its id.

    Written BEFORE the command is created, deliberately -- the same claim-then-queue
    discipline the deploy and file-backup schedulers use. A crash between the two leaves a
    change row with no command, which the console shows as pending and an operator can
    cancel; the other order leaves a command whose change id resolves to nothing, and the
    agent would fetch a payload that does not exist.
    """
    change_id = uuid.uuid4().hex
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO bios_changes(id, machine, status, requested_by, requested_at, "
            "                         changes_json) VALUES (?, ?, ?, ?, ?, ?)",
            (change_id, machine, CHANGE_PENDING, requested_by, now, json.dumps(changes)),
        )
        # Trim oldest-first, and only terminal rows: an in-flight change is never evicted by
        # history pressure, however much of it there is.
        conn.execute(
            "DELETE FROM bios_changes WHERE machine = ? AND status NOT IN (?, ?) AND id NOT IN "
            "(SELECT id FROM bios_changes WHERE machine = ? AND status NOT IN (?, ?) "
            " ORDER BY requested_at DESC, rowid DESC LIMIT ?)",
            (machine, CHANGE_PENDING, CHANGE_RUNNING,
             machine, CHANGE_PENDING, CHANGE_RUNNING, MAX_CHANGES_PER_MACHINE),
        )
    return change_id


def attach_command(db_path, change_id, command_id):
    with get_conn(db_path) as conn:
        conn.execute("UPDATE bios_changes SET command_id = ? WHERE id = ?",
                     (command_id, change_id))


def get_change(db_path, change_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM bios_changes WHERE id = ?", (change_id,)).fetchone()
    return _change_row(row) if row is not None else None


def list_changes(db_path, machine, limit=20):
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bios_changes WHERE machine = ? "
            "ORDER BY requested_at DESC, rowid DESC LIMIT ?",
            (machine, int(limit)),
        ).fetchall()
    return [_change_row(row) for row in rows]


def _change_row(row):
    change = dict(row)
    for key, raw in (("changes", change.pop("changes_json", "[]")),
                     ("results", change.pop("results_json", "[]"))):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = []
        change[key] = parsed if isinstance(parsed, list) else []
    return change


def start_change(db_path, change_id):
    """Mark a change as being written. Conditional on it still being pending, which closes the
    race between two agent polls delivering the same command twice -- the second fetch is
    refused rather than replaying the writes."""
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE bios_changes SET status = ? WHERE id = ? AND status = ?",
            (CHANGE_RUNNING, change_id, CHANGE_PENDING),
        )
    return cursor.rowcount > 0


def cancel_change(db_path, change_id):
    """Give up on a change the machine never picked up. Only a PENDING change can be
    cancelled: once the agent has fetched the payload it may already have written to the
    firmware, and a console row saying "cancelled" over a machine whose Secure Boot is now off
    is worse than no cancel at all. Same honesty as the backup cancel, for the same reason."""
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE bios_changes SET status = ?, finished_at = ?, error = ? "
            "WHERE id = ? AND status = ?",
            (CHANGE_FAILED, int(time.time()), "cancelled before the machine picked it up",
             change_id, CHANGE_PENDING),
        )
    return cursor.rowcount > 0


def classify_result(change, item):
    """Decide what happened to ONE attribute, from what the agent read back afterwards.

    This is the verification the roadmap chose over a per-vendor reboot table, and the whole
    of it is here: compare the re-read against what was asked for and what was there before.

      * the agent reported an error          -> FAILED (nothing else is knowable)
      * the re-read is the requested value   -> APPLIED, and say nothing about rebooting
      * the re-read is still the old value   -> PENDING_REBOOT, and only now ask for one
      * anything else, or nothing read back  -> UNKNOWN

    The last case is real: some firmware normalises what you write, and some substitutes
    something else entirely. Calling either "applied" would confirm a change nobody made.
    """
    requested = str(item.get("to") if item.get("to") is not None else "")
    observed = item.get("observed")
    if item.get("error"):
        return OUTCOME_FAILED
    if observed is None or str(observed).strip() == "":
        return OUTCOME_UNKNOWN
    if _same_value(observed, requested):
        return OUTCOME_APPLIED
    if _same_value(observed, change.get("from", "")):
        return OUTCOME_PENDING_REBOOT
    return OUTCOME_UNKNOWN


def _overall_status(outcomes):
    """Roll per-attribute outcomes into the change's own state.

    Ordered worst-news-first on purpose. A change where five attributes applied and one failed
    is PARTIAL, not APPLIED -- reporting the majority outcome is how the one that did not take
    goes unnoticed, and on firmware that is the one that matters.
    """
    if not outcomes:
        return CHANGE_FAILED
    if all(o == OUTCOME_FAILED for o in outcomes):
        return CHANGE_FAILED
    if any(o in (OUTCOME_FAILED, OUTCOME_UNKNOWN) for o in outcomes):
        return CHANGE_PARTIAL
    if any(o == OUTCOME_PENDING_REBOOT for o in outcomes):
        return CHANGE_PENDING_REBOOT
    return CHANGE_APPLIED


def ingest_change_result(db_path, change_id, payload):
    """Store what the agent reported back for one change, classified per attribute.

    Like record_inventory this is written from a machine's report and is therefore trimmed and
    non-raising on shape. It answers with the resulting change row so the caller can log it.
    """
    change = get_change(db_path, change_id)
    if change is None:
        return None
    if change["status"] not in CHANGE_OPEN_STATES:
        # A late result for a change already resolved (a retry after the hub answered, a
        # cancelled row). Dropped rather than reopening a terminal row.
        return change

    payload = payload if isinstance(payload, dict) else {}
    reported = {}
    for raw in list(payload.get("items") or [])[:MAX_CHANGES_PER_REQUEST]:
        if isinstance(raw, dict) and str(raw.get("name") or "").strip():
            reported[str(raw["name"]).strip().casefold()] = raw

    results = []
    for requested in change["changes"]:
        item = reported.get(requested["name"].casefold(), {})
        observed = item.get("observed")
        error = str(item.get("error") or "")[:MAX_ERROR_CHARS]
        if not item and not payload.get("items"):
            # The agent failed before it could write anything -- a missing vendor interface, a
            # rejected password. The run-level error is the honest answer for every attribute;
            # inventing a per-attribute one would suggest we know which ones were reached.
            error = str(payload.get("error") or "the machine reported no result")[:MAX_ERROR_CHARS]
        merged = {
            "name": requested["name"],
            "from": requested.get("from", ""),
            "to": requested.get("to", ""),
            "observed": None if observed is None else str(observed)[:MAX_VALUE_CHARS],
            "error": error,
        }
        merged["outcome"] = classify_result(requested, merged)
        results.append(merged)

    status = _overall_status([r["outcome"] for r in results])
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE bios_changes SET status = ?, finished_at = ?, results_json = ?, error = ? "
            "WHERE id = ?",
            (status, int(time.time()), json.dumps(results),
             str(payload.get("error") or "")[:MAX_ERROR_CHARS], change_id),
        )
    return get_change(db_path, change_id)


def expire_stale_changes(db_path, older_than_seconds):
    """Close out changes whose machine never answered.

    Without this a machine that goes down mid-write leaves a RUNNING row forever, and
    open_change_for would refuse every subsequent change to that machine -- one dead agent
    would permanently lock its own firmware tab. Marked PARTIAL rather than FAILED, and that
    is the honest state: the agent had fetched the payload, so some of it may well have been
    written. Returns how many were closed.
    """
    cutoff = int(time.time()) - int(older_than_seconds)
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE bios_changes SET status = ?, finished_at = ?, error = ? "
            "WHERE status IN (?, ?) AND requested_at < ?",
            (CHANGE_PARTIAL, int(time.time()),
             "the machine never reported the outcome; re-read the firmware inventory to see "
             "what actually changed",
             CHANGE_PENDING, CHANGE_RUNNING, cutoff),
        )
    return cursor.rowcount
