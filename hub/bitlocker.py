"""BitLocker posture and recovery-key escrow (roadmap #19).

The hub-side half of "is this PC encrypted, and do we still have the key if it locks itself
out". A Windows agent reads its own volume encryption state on the inventory loop and carries
it on the heartbeat; this module stores that, decides which recovery passwords the hub is
still missing, and keeps the ones it has been handed.

**The silent failure this exists to prevent is the one that costs a machine.** BitLocker
locks a volume on its own -- a firmware update that moves the TPM measurement, a disk moved
into another chassis, a user who fails the PIN often enough -- and at that point the recovery
password is the only way back in. Until now this product could see that a PC existed and
could not tell you whether anybody held its key, which means the honest answer to "we are
locked out of OTTOWIENS" was to reimage it. A key nobody escrowed is discovered missing
exactly once, at the worst moment, by somebody who cannot do anything about it.

**Status lives in this database; the recovery passwords never do.** The passwords go into the
same `.env`-master-key-wrapped sidecar `backups.py` gives the BIOS setup password
(`backups.store_secret`), and the database keeps only the protector *ids* -- a GUID is an
index, not a secret. That split is what makes the rest of the design cheap: deciding what the
hub is still missing is a plain SQL read on the heartbeat path, and the secret store is
opened only when a key arrives or an operator asks for one. It also keeps every recovery
password out of `fleet.db`, which is the file `backup.hub_enabled` copies to an S3 bucket.

**Keys do not ride the heartbeat.** The heartbeat carries posture and the hub answers with
`bitlocker_escrow_wanted` -- the protector ids it has no row for -- and only then does the
agent POST those passwords to an endpoint of its own. Three things fall out of that and each
one was the point: a settled fleet transmits no key material at all, a protector rotated on a
machine is picked up within one heartbeat without anybody scheduling anything, and the hub
asks for exactly what it lacks rather than being handed everything every time.

**Escrowing is a setting, and it can be off.** `security.escrow_bitlocker_keys` gates whether
the hub asks for keys at all. The custody warning on `BACKUP_MASTER_KEY` applies double here:
this hub holding every recovery password in the fleet is a real concentration of risk, and a
group that would rather keep that in AD or Entra needs to be able to say so without
uninstalling anything. What the setting does NOT do is throw away what is already escrowed --
turning collection off is a decision about the future, and reading it as "delete the keys"
would be the single most destructive thing a checkbox in this product could do.

**A key is never deleted because a machine stopped reporting its protector.** A volume that
reports nothing (decrypted, or an inventory that failed halfway) leaves its escrowed password
exactly where it was until the machine itself is forgotten. The alternative -- prune what the
last report did not mention -- makes one bad inventory the thing that erases the only copy of
a key, which is the failure this whole module exists to prevent, arriving by a new route.

Written from the heartbeat, so ingest is trimmed, type-checked and non-raising in the same
way `bios.record_inventory` is: a malformed report costs a stale posture, never a heartbeat.

Kept free of Flask so it can be unit-tested standalone; bitlocker_web.py wires HTTP on top.
"""
import json
import sqlite3
import time

# ---------------------------------------------------------------- support states
#: The agent enumerated this machine's encryptable volumes.
SUPPORT_SUPPORTED = "supported"
#: No BitLocker on this machine at all -- a Home edition, a Linux or Android agent, a VM
#: image built without it. Not an error, and deliberately not rendered as one; for a lot of
#: hardware this is the permanent and correct answer.
SUPPORT_UNSUPPORTED = "unsupported"
#: The interface is there and reading it failed. This one is worth an operator's attention.
SUPPORT_ERROR = "error"

SUPPORT_STATES = frozenset({SUPPORT_SUPPORTED, SUPPORT_UNSUPPORTED, SUPPORT_ERROR})

# ---------------------------------------------------------------- protection states
#: Encrypted and its key protectors are active. The state an operator wants to see.
PROTECTION_ON = "on"
#: The volume is not protected right now. That covers a decrypted volume AND an encrypted one
#: whose protection is suspended -- which is a real state Windows leaves a machine in after a
#: firmware update, and one that reads as "encrypted" to anybody looking at the disk icon.
PROTECTION_OFF = "off"
#: The agent could not tell. Stored distinctly rather than folded into `off`, for the reason
#: the rest of this file keeps repeating: "not protected" is a finding somebody acts on, and
#: "we do not know" is not.
PROTECTION_UNKNOWN = "unknown"

PROTECTION_STATES = frozenset({PROTECTION_ON, PROTECTION_OFF, PROTECTION_UNKNOWN})

# ---------------------------------------------------------------- protector kinds
#: The 48-digit numerical password. **The only kind this module escrows** -- it is the only
#: one that is a secret the hub can hold and a human can type at the recovery screen.
PROTECTOR_RECOVERY_PASSWORD = "recovery_password"
#: A TPM, TPM+PIN, startup key, certificate, password or anything else the machine reported.
#: Recorded in the posture so an operator can see what is actually protecting the volume, and
#: never escrowed: there is nothing transferable to hold.
PROTECTOR_OTHER = "other"

PROTECTOR_KINDS = frozenset({PROTECTOR_RECOVERY_PASSWORD, PROTECTOR_OTHER})

# ---------------------------------------------------------------- ingest caps
# A machine has a handful of volumes and a volume a handful of protectors. These bounds exist
# so a misbehaving or hostile agent cannot grow its row without limit -- this lands in the
# database the hub backs up, so an unbounded blob is a storage problem twice.
MAX_VOLUMES = 64
MAX_PROTECTORS_PER_VOLUME = 16
MAX_TEXT_CHARS = 200
MAX_ERROR_CHARS = 500

#: How many escrowed protectors one machine may hold. Well past what real hardware produces
#: (a laptop with four encrypted volumes rotating a protector a year takes decades to reach
#: it), and the refusal past it is a refusal to store the NEW key rather than a rotation that
#: drops the oldest. Dropping the oldest is how the one key somebody needs disappears without
#: anybody being told; refusing loudly is recoverable.
MAX_KEYS_PER_MACHINE = 64

#: A recovery password is 48 digits in eight groups. The cap is generous rather than an exact
#: format check -- Microsoft has changed protector formats before, and a hub that refuses to
#: hold a key because the shape surprised it is a hub that loses the machine.
MAX_PASSWORD_CHARS = 256

#: The secret-store id holding one machine's escrowed recovery passwords.
#: The machine name is part of the id and therefore part of the AAD, so a key blob copied
#: between machines fails to decrypt rather than being handed to the wrong PC -- the same
#: property `bios.secret_id_for` relies on, and here it is the whole custody argument.
SECRET_ID_PREFIX = "bitlocker.keys.machine:"


def secret_id_for(machine):
    """The secret-store id for one machine's escrowed recovery passwords."""
    return f"{SECRET_ID_PREFIX}{machine}"


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_bitlocker_db(db_path):
    """Create the encryption posture tables if absent. Idempotent -- safe to call on every
    hub start next to app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_bitlocker (
                machine      TEXT PRIMARY KEY,
                support      TEXT NOT NULL,
                error        TEXT NOT NULL DEFAULT '',
                volumes_json TEXT NOT NULL DEFAULT '[]',
                reported_at  INTEGER NOT NULL
            )
            """
        )
        # The index over what is escrowed. One row per protector the hub holds a password
        # for; the password itself is in the secret store and never here.
        #
        # Separate from machine_bitlocker rather than a column on it, because the two answer
        # different questions with different lifetimes: the posture blob is replaced wholesale
        # by every report, and an escrow row must survive a report that no longer mentions it
        # (see the module docstring -- a bad inventory must never be what erases a key).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bitlocker_keys (
                machine      TEXT NOT NULL,
                protector_id TEXT NOT NULL,
                volume       TEXT NOT NULL DEFAULT '',
                escrowed_at  INTEGER NOT NULL,
                last_read_at INTEGER,
                read_count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (machine, protector_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bitlocker_keys_machine "
                     "ON bitlocker_keys(machine)")


# --------------------------------------------------------------------------- ingest
def _clean_protector(raw):
    """Normalise one reported key protector, or None if it is unusable.

    An id-less protector is dropped rather than stored under a placeholder: the id is what an
    escrowed password is filed under and what `escrow_wanted` asks for by name, so a protector
    nobody can name is a key nobody could ever match back to it.
    """
    if not isinstance(raw, dict):
        return None
    protector_id = str(raw.get("id") or "").strip()[:MAX_TEXT_CHARS]
    if not protector_id:
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in PROTECTOR_KINDS:
        # Anything we do not recognise is `other`, which means "shown, never escrowed". The
        # asymmetry is deliberate: mis-filing an unknown protector as a recovery password
        # would have the hub ask the agent for a secret that does not exist, forever.
        kind = PROTECTOR_OTHER
    return {
        "id": protector_id,
        "kind": kind,
        "label": str(raw.get("label") or "").strip()[:MAX_TEXT_CHARS],
    }


def _clean_volume(raw):
    """Normalise one reported volume, or None if it is unusable."""
    if not isinstance(raw, dict):
        return None
    mount = str(raw.get("mount") or "").strip()[:MAX_TEXT_CHARS]
    device_id = str(raw.get("device_id") or "").strip()[:MAX_TEXT_CHARS]
    # A volume with neither a mount point nor a device id cannot be named on a page or matched
    # to a protector, so it is not a volume anybody can act on. An unmounted-but-identified
    # volume is kept: a data disk with no drive letter is exactly the one people forget.
    if not mount and not device_id:
        return None

    protection = str(raw.get("protection") or "").strip().lower()
    if protection not in PROTECTION_STATES:
        protection = PROTECTION_UNKNOWN

    protectors = []
    seen = set()
    for entry in list(raw.get("protectors") or [])[:MAX_PROTECTORS_PER_VOLUME]:
        protector = _clean_protector(entry)
        if protector is not None and protector["id"] not in seen:
            seen.add(protector["id"])
            protectors.append(protector)

    percentage = raw.get("percentage")
    try:
        percentage = None if percentage is None else max(0, min(100, int(percentage)))
    except (TypeError, ValueError):
        percentage = None

    return {
        "mount": mount,
        "device_id": device_id,
        "protection": protection,
        "conversion": str(raw.get("conversion") or "").strip().lower()[:MAX_TEXT_CHARS],
        "percentage": percentage,
        "method": str(raw.get("method") or "").strip()[:MAX_TEXT_CHARS],
        "protectors": protectors,
    }


def record_inventory(db_path, machine, payload):
    """Store a machine's reported encryption posture. Returns True if something was stored.

    Never raises on payload shape -- see the module docstring. The support state IS enforced:
    an unrecognised one is stored as `error` rather than verbatim, so the value the console
    and `escrow_wanted` branch on can only ever be one of three things.
    """
    if not isinstance(payload, dict) or not machine:
        return False

    support = str(payload.get("support") or "").strip().lower()
    if support not in SUPPORT_STATES:
        # An agent reporting something we do not understand HAS an interface and is failing to
        # describe it. Guessing `unsupported` would file a real fault under the one state
        # nobody is ever shown, which is how a fleet quietly stops being encrypted.
        support = SUPPORT_ERROR

    volumes = []
    if support == SUPPORT_SUPPORTED:
        for raw in list(payload.get("volumes") or [])[:MAX_VOLUMES]:
            volume = _clean_volume(raw)
            if volume is not None:
                volumes.append(volume)
        volumes.sort(key=lambda v: (v["mount"] or v["device_id"]).casefold())
        # "Supported, and here are zero volumes" is not a claim anybody can act on. A Windows
        # machine always has at least the system volume, so an empty list is an enumeration
        # that failed rather than a machine with nothing to encrypt.
        if not volumes:
            support = SUPPORT_ERROR

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO machine_bitlocker(machine, support, error, volumes_json, reported_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET support = excluded.support, "
            "error = excluded.error, volumes_json = excluded.volumes_json, "
            "reported_at = excluded.reported_at",
            (
                machine,
                support,
                str(payload.get("error") or "")[:MAX_ERROR_CHARS],
                json.dumps(volumes),
                int(time.time()),
            ),
        )
    return True


# --------------------------------------------------------------------------- read
def get_inventory(db_path, machine):
    """The last reported posture for `machine`, with the escrow state merged onto each
    protector.

    A machine that has never reported is `support: null` -- deliberately a fourth value at the
    read boundary and not one of the three stored states, exactly as `bios.get_inventory` does
    it. "We have not been told" is not the claim "this machine cannot do it", and rendering an
    unknown as `unsupported` would write off every PC in the fleet the day before the agent
    release lands.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT support, error, volumes_json, reported_at FROM machine_bitlocker "
            "WHERE machine = ?", (machine,)
        ).fetchone()
        escrow = {
            r["protector_id"]: dict(r)
            for r in conn.execute(
                "SELECT protector_id, volume, escrowed_at, last_read_at, read_count "
                "FROM bitlocker_keys WHERE machine = ?", (machine,)
            ).fetchall()
        }

    if row is None:
        # Escrowed keys are returned even with no posture at all. A machine that was wiped and
        # rebuilt still has its old keys here, and that is precisely when somebody goes looking
        # for them -- an empty answer would read as "there was never a key".
        return {"support": None, "error": "", "volumes": [], "reported_at": None,
                "escrowed": _orphan_escrow(escrow, set())}

    try:
        volumes = json.loads(row["volumes_json"]) or []
    except (TypeError, ValueError):
        volumes = []

    matched = set()
    for volume in volumes:
        for protector in volume.get("protectors") or []:
            record = escrow.get(protector["id"])
            protector["escrowed"] = record is not None
            protector["escrowed_at"] = record["escrowed_at"] if record else None
            protector["last_read_at"] = record["last_read_at"] if record else None
            protector["read_count"] = record["read_count"] if record else 0
            if record is not None:
                matched.add(protector["id"])

    return {
        "support": row["support"],
        "error": row["error"],
        "volumes": volumes,
        "reported_at": row["reported_at"],
        # Keys the hub holds that no reported volume claims any more -- a decrypted disk, a
        # rotated protector, a machine rebuilt under the same name. Surfaced rather than
        # hidden, because "the hub still has a key for a volume that is gone" is a fact an
        # operator should be able to see and decide about, and because a key nobody can see is
        # a key nobody knows to delete.
        "escrowed": _orphan_escrow(escrow, matched),
    }


def _orphan_escrow(escrow, matched):
    return sorted(
        ({"protector_id": pid, **{k: v for k, v in record.items() if k != "protector_id"}}
         for pid, record in escrow.items() if pid not in matched),
        key=lambda r: r["escrowed_at"],
    )


def escrowed_ids(db_path, machine):
    """The protector ids the hub already holds a password for."""
    with get_conn(db_path) as conn:
        return {r["protector_id"] for r in conn.execute(
            "SELECT protector_id FROM bitlocker_keys WHERE machine = ?", (machine,)
        ).fetchall()}


def escrow_wanted(db_path, machine):
    """Which recovery-password protector ids the hub is still missing for `machine`.

    This is what the heartbeat answers with, so it is a plain read over two indexed tables and
    nothing else -- no secret store, no decryption, no file I/O. That is the whole reason the
    protector ids live in the database while the passwords do not.

    Only `recovery_password` protectors are ever asked for. A TPM protector has no
    transferable secret, and asking for one would be a request the agent can never satisfy and
    would therefore answer on every heartbeat for the life of the machine.
    """
    posture = None
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT volumes_json FROM machine_bitlocker WHERE machine = ?",
                           (machine,)).fetchone()
        if row is None:
            return []
        try:
            posture = json.loads(row["volumes_json"]) or []
        except (TypeError, ValueError):
            return []
        held = {r["protector_id"] for r in conn.execute(
            "SELECT protector_id FROM bitlocker_keys WHERE machine = ?", (machine,)
        ).fetchall()}

    wanted = []
    for volume in posture:
        for protector in volume.get("protectors") or []:
            if protector.get("kind") != PROTECTOR_RECOVERY_PASSWORD:
                continue
            pid = protector.get("id")
            if pid and pid not in held and pid not in wanted:
                wanted.append(pid)
    return wanted


def count_keys(db_path, machine):
    with get_conn(db_path) as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM bitlocker_keys WHERE machine = ?",
                            (machine,)).fetchone()["n"]


# --------------------------------------------------------------------------- escrow
def clean_key_submission(raw, wanted):
    """Normalise one submitted key, or None if it is unusable or was not asked for.

    Pure, and separate from the storing so a test can assert on what the hub will accept
    without a secret store anywhere near it.

    **`wanted` is enforced here rather than trusted from the agent.** An enrolled agent could
    otherwise post a thousand protector ids of its own invention and grow its own secret blob;
    filtering against what the hub actually asked for means the only ids that can ever be
    stored are ones this machine reported as its own recovery protectors.
    """
    if not isinstance(raw, dict):
        return None
    protector_id = str(raw.get("protector_id") or "").strip()[:MAX_TEXT_CHARS]
    password = str(raw.get("recovery_password") or "").strip()[:MAX_PASSWORD_CHARS]
    if not protector_id or not password or protector_id not in wanted:
        return None
    return {
        "protector_id": protector_id,
        "recovery_password": password,
        "volume": str(raw.get("volume") or "").strip()[:MAX_TEXT_CHARS],
    }


def merge_keys(existing, submissions, now=None):
    """The replacement secret-store blob, given what is stored and what just arrived.

    Pure, for the same reason `clean_key_submission` is, and because this is the one function
    in the escrow path whose mistakes are unrecoverable -- a blob written without a key that
    was in it before is a key that no longer exists anywhere.

    **A protector id already held is never overwritten.** The recovery password for a given
    protector does not change (rotating one produces a NEW protector with a new id), so a
    second value arriving for an id we hold is either a duplicate or something wrong, and in
    neither case is replacing the stored key the safe reading.
    """
    now = int(now if now is not None else time.time())
    merged = dict(existing or {})
    added = []
    for submission in submissions:
        pid = submission["protector_id"]
        if pid in merged:
            continue
        merged[pid] = {
            "recovery_password": submission["recovery_password"],
            "volume": submission["volume"],
            "escrowed_at": now,
        }
        added.append(submission)
    return merged, added


def record_escrowed(db_path, machine, submissions, now=None):
    """Write the index rows for keys that have just gone into the secret store.

    Called AFTER the store write, never before: an index row without a stored password is a
    hub that believes it has a key it cannot produce, and the heartbeat would stop asking for
    the one thing it actually needs.
    """
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        for submission in submissions:
            conn.execute(
                "INSERT OR IGNORE INTO bitlocker_keys(machine, protector_id, volume, "
                "                                     escrowed_at) VALUES (?, ?, ?, ?)",
                (machine, submission["protector_id"], submission["volume"], now),
            )


def note_read(db_path, machine, protector_id, now=None):
    """Record that somebody read this key. The audit log is the record of WHO; this is what
    lets the console show, beside the key, that it has been read before and when."""
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE bitlocker_keys SET last_read_at = ?, read_count = read_count + 1 "
            "WHERE machine = ? AND protector_id = ?", (now, machine, protector_id))


# --------------------------------------------------------------------------- lifecycle
def rename_machine(db_path, old, new):
    """Follow a machine through a rename, exactly as bios.rename_machine does.

    The posture and the escrow index both move. The secret-store blob is keyed by machine name
    and is moved by the caller (bitlocker_web), because only it holds the master key -- and a
    rename that moved the index but not the passwords would leave the hub listing keys it
    could no longer decrypt, which is worse than either half alone.
    """
    with get_conn(db_path) as conn:
        # Keep the survivor's own posture when both machines share the same hardware;
        # INSERT OR IGNORE preserves the destination row on collision.
        conn.execute(
            "INSERT OR IGNORE INTO machine_bitlocker (machine, protector_count, latest_protector_id, updated_at) "
            "SELECT ?, protector_count, latest_protector_id, updated_at FROM machine_bitlocker WHERE machine = ?",
            (new, old))
        conn.execute("DELETE FROM machine_bitlocker WHERE machine = ?", (old,))
        # Merge source escrow-index rows into destination using collision-safe inserts;
        # the survivor's own keys win when protector_ids collide (same physical device).
        conn.execute(
            "INSERT OR IGNORE INTO bitlocker_keys (machine, protector_id, volume, escrowed_at) "
            "SELECT ?, protector_id, volume, escrowed_at FROM bitlocker_keys WHERE machine = ?",
            (new, old))
        conn.execute("DELETE FROM bitlocker_keys WHERE machine = ?", (old,))


def forget_machine(db_path, machine):
    """Drop a forgotten machine's posture and escrow index.

    The secret store entry is deleted by the caller alongside this, the same way app.py
    already deletes `bios.secret_id_for(machine)` -- see the note there about a stored secret
    surviving its machine.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_bitlocker WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM bitlocker_keys WHERE machine = ?", (machine,))
