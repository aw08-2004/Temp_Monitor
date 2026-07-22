"""Backups -- roadmap #1, subject 1a: the hub's own database, scheduled and offsite.

The hub is the only place fleet history, permission groups, package recipes and audit
trail exist. A disk failure on the box running it loses all of that, so this module
takes a consistent copy on a schedule and pushes it somewhere the hub cannot reach and
delete by accident.

Five ideas carry the design, and each exists because the naive version is wrong:

  * **`VACUUM INTO`, never a file copy.** The database is opened WAL and written live by
    the ingest path and the db_writer thread. Copying `temp_v2.db` while that is
    happening yields a torn file plus a `-wal` sidecar you did not copy, i.e. a backup
    that restores to "database disk image is malformed" -- and you find out on the day
    you need it. `VACUUM INTO` asks SQLite for a transactionally consistent, already
    compacted snapshot instead.

  * **Encryption happens here, before the bytes leave the process.** The storage
    provider only ever sees ciphertext, which is what makes "offsite" and "someone
    else's disk" the same sentence. AES-256-GCM in 4 MiB chunks (see the envelope
    section) rather than one giant buffer, because a hub database is allowed to be
    bigger than RAM.

  * **The master key lives in `.env`, and NOTHING else can decrypt.** That is the
    sharpest edge of the whole feature and it is deliberate: losing the key loses every
    backup ever taken, and no amount of hub database is a substitute. So the key is
    generated once, held outside the database, and the console makes an operator
    acknowledge that they have stored it offline (see `KEY_ESCROW_STATE_KEY`).
    `restore_backup.py` at the repo root decrypts with the key and the artifact ALONE --
    no hub, no database, no this module -- which is the only form of "we can restore"
    that survives losing the server.

  * **Destination credentials are never in the `settings` table.** Settings are dumped
    into the hub database, rendered in a form, and shipped around in `agent_config`;
    an S3 secret key has no business in any of those. They live in a sidecar file,
    themselves encrypted with the master key, keyed by an opaque destination id.

  * **Rotation reads the remote listing, not a local record.** What generations exist is
    whatever the bucket says exists -- an operator who deleted one by hand, or a run that
    uploaded and then crashed before recording, both stay consistent. Object keys are
    timestamp-prefixed so "newest N" is a lexicographic sort, with no dependence on
    remote mtime, which S3 and WebDAV disagree about the format of anyway.

Two storage kinds, chosen per destination by the Admin: `s3` (any S3-compatible
endpoint -- AWS, MinIO, Backblaze, Wasabi) and `webdav`. S3 is signed with SigV4
implemented here in ~100 lines of stdlib hmac rather than by taking an 80 MB botocore
dependency onto a hub whose entire sparse install is 0.3 MB; `presigned_url()` is the
same signer in its query-string form, which is what roadmap #1b will hand to agents so
a machine can upload its own files without ever holding the master credential.

Authorization lives entirely upstream, at the `manage_backups` capability (see
backups_web.py). Nothing here checks a session, exactly like fleet.py and packages.py.

Kept free of Flask AND of settings.py -- the scheduler's knobs are passed in by app.py,
the same way packages.tick() takes its TTL -- so the whole module is unit-testable
against a temp directory and a fake destination.
"""
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import struct
import threading
import time
import uuid
import zlib
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlsplit

import requests

import backup_paths
import fleet

# AES-GCM comes from `cryptography`, which Authlib already pulls in -- so this is not a
# new install-time dependency in practice, only a newly explicit one in requirements.txt.
# Imported softly all the same: app.py's self-updater treats `pip install` as best-effort
# (a release that adds a dependency must not crash-loop the restart), so a hub can briefly
# be running new code against old site-packages. An ImportError here would take the whole
# console down; a clear error when someone actually presses "Back up now" would not.
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on a half-updated hub
    AESGCM = None
    CRYPTO_AVAILABLE = False


# ================================
# VOCABULARY
# ================================
# Storage kinds. `s3` is any S3-compatible endpoint; `webdav` is Nextcloud/ownCloud/IIS
# and friends. Deliberately two, not a plugin system -- every kind here is a protocol
# that has to keep working, and the roadmap asks for exactly these.
KIND_S3 = "s3"
KIND_WEBDAV = "webdav"
DESTINATION_KINDS = (KIND_S3, KIND_WEBDAV)

DESTINATION_LABELS = {
    KIND_S3: ("S3-compatible",
              "AWS S3, MinIO, Backblaze B2, Wasabi -- anything speaking the S3 API. "
              "Signed with SigV4; the hub can also mint scoped upload URLs from it."),
    KIND_WEBDAV: ("WebDAV",
                  "Nextcloud, ownCloud, IIS, or any WebDAV share. Authenticated with a "
                  "username and password over HTTPS."),
}

# What a run backed up. Only `hub_db` runs today; `machine_files` is roadmap #1b and is
# why `backup_runs` carries a `machine` column already -- see init_backups_db().
BACKUP_HUB_DB = "hub_db"
BACKUP_MACHINE_FILES = "machine_files"

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_STATUSES = (RUN_RUNNING, RUN_SUCCEEDED, RUN_FAILED)

# How a run was started. Worth distinguishing in the UI: a failing schedule is an
# outage, a failing manual run is usually someone testing a new destination.
TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"

# `backup_state` keys. Bookkeeping that is neither a setting (operators don't set it) nor
# a run (it outlives any single one).
LAST_ATTEMPT_STATE_KEY = "hub_db.last_attempt_at"
LAST_SUCCESS_STATE_KEY = "hub_db.last_success_at"
# Set when an operator confirms they have stored the master key somewhere other than this
# server. Until then the console nags, because a key that only exists on the machine being
# backed up is not a backup strategy.
KEY_ESCROW_STATE_KEY = "master_key.escrowed_at"

# Envelope format. The magic is versioned in the bytes themselves so a future format can
# be detected rather than guessed at from a file extension.
MAGIC = b"FHBK1\n"
ENVELOPE_VERSION = 1
FILE_EXTENSION = ".fhb"

# 4 MiB of plaintext per AES-GCM chunk. Large enough that the 16-byte tag and 4-byte
# framing per chunk are noise, small enough that neither encrypt nor decrypt ever needs a
# database-sized buffer.
CHUNK_BYTES = 4 * 1024 * 1024

MASTER_KEY_ENV = "BACKUP_MASTER_KEY"
MASTER_KEY_BYTES = 32          # AES-256
SECRETS_FILENAME = "backup_secrets.json"

MAX_NAME_CHARS = 80
MAX_ERROR_CHARS = 1000

# Requests timeouts: (connect, read). Reads are generous because a multi-gigabyte PUT to a
# slow endpoint is normal; connects are not, because an unreachable host should fail the
# run in seconds rather than wedge the scheduler thread.
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 300

# Where each kind of backup lands under a destination's prefix. Machine backups get a
# per-machine folder because that is the unit roadmap #1b scopes a credential to.
HUB_DB_FOLDER = "hub-db"
MACHINE_FOLDER = "machines"


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_backups_db(db_path):
    """Create the backup tables if absent. Idempotent -- safe to call next to the other
    init_*_db() functions on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # Connection details ONLY. Credentials for this row live in the sidecar secret
        # store, encrypted, addressed by this id -- see load_secret().
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_destinations (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                kind        TEXT NOT NULL,   -- DESTINATION_KINDS
                config_json TEXT NOT NULL,   -- endpoint/bucket/prefix -- never secrets
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL,
                created_by  TEXT,
                updated_by  TEXT
            )
            """
        )
        # Case-insensitive, like package and permission-group names: two destinations
        # differing only in case is a configuration accident every time.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_backup_destinations_name "
            "ON backup_destinations(name COLLATE NOCASE)"
        )
        # One row per attempt, written `running` before any work starts so a hub that dies
        # mid-upload leaves evidence rather than silence.
        #
        # `machine` is NULL for every row this version writes. It is here from day one
        # because roadmap #1b (per-PC file backups) writes machine-scoped runs into this
        # same table, and adding a column to the table the restore UI reads from is a
        # migration nobody should have to do later -- the same reasoning as
        # permissions.permission_group_members.ad_group_dn.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_runs (
                id             TEXT PRIMARY KEY,
                kind           TEXT NOT NULL,   -- BACKUP_HUB_DB | BACKUP_MACHINE_FILES
                machine        TEXT,            -- NULL for hub_db; roadmap #1b fills it
                destination_id TEXT,
                status         TEXT NOT NULL,   -- RUN_STATUSES
                trigger        TEXT NOT NULL,   -- TRIGGER_SCHEDULE | TRIGGER_MANUAL
                actor          TEXT,
                object_key     TEXT,
                source_bytes   INTEGER,         -- the snapshot, before gzip+encrypt
                stored_bytes   INTEGER,         -- what was actually PUT
                artifact_sha256 TEXT,
                error          TEXT,
                started_at     INTEGER NOT NULL,
                finished_at    INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_backup_runs_started "
            "ON backup_runs(kind, started_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_backup_runs_machine ON backup_runs(machine)"
        )
        # Columns added after backup_runs first shipped (hub 1.28.0). CREATE TABLE IF NOT
        # EXISTS does nothing to a table that already exists, so a hub upgrading from
        # 1.28.0 needs these added explicitly -- the same pattern app.init_db() uses for
        # machine_info. Every one is nullable, so old rows simply read NULL.
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(backup_runs)")}
        for column, ddl in (
            ("file_count", "INTEGER"),        # files in this run's archive (#1b)
            ("chain_id", "TEXT"),             # which chain a machine run belongs to
            ("sequence", "INTEGER"),          # 0 = the full that starts the chain
            ("command_id", "TEXT"),           # the fleet command carrying it to the agent
        ):
            if column not in run_columns:
                conn.execute(f"ALTER TABLE backup_runs ADD COLUMN {column} {ddl}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_state (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        # Per-machine file-backup overrides. Every column but `machine` is nullable
        # because a row's ABSENCE is the common case and means "follow fleet defaults" --
        # see the PER-MACHINE FILE BACKUP CONFIG section.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_machine_config (
                machine        TEXT PRIMARY KEY,
                enabled        INTEGER,   -- NULL = follow the fleet setting
                destination_id TEXT,      -- NULL = follow the fleet setting
                include_json   TEXT,      -- EXTRA includes, added to the fleet list
                exclude_json   TEXT,      -- EXTRA excludes
                profiles_json  TEXT,      -- what the agent last reported (for preview)
                reported_at    INTEGER,
                updated_at     INTEGER,
                updated_by     TEXT
            )
            """
        )
        # Added in hub 1.32.0, same ALTER-if-missing reason as backup_runs above.
        # `run_requested_at` is the manual-backup queue: an operator pressing "Back up
        # now" sets it, and the next dispatch pass that finds the machine ONLINE clears
        # it. It lives here rather than in a table of its own because there is at most
        # one outstanding request per machine -- pressing the button twice while a PC is
        # offline must not queue two backups.
        config_columns = {row["name"] for row in
                          conn.execute("PRAGMA table_info(backup_machine_config)")}
        for column, ddl in (
            ("run_requested_at", "INTEGER"),
            ("run_requested_by", "TEXT"),
        ):
            if column not in config_columns:
                conn.execute(
                    f"ALTER TABLE backup_machine_config ADD COLUMN {column} {ddl}")
        # One row per uploaded machine archive. `chain_id` groups a full with the
        # incrementals that depend on it, and `sequence` is 0 for that full -- the two
        # together are what makes rotation able to delete a whole chain and never strand
        # an incremental whose base is gone.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_file_sets (
                id           TEXT PRIMARY KEY,
                machine      TEXT NOT NULL,
                run_id       TEXT NOT NULL,
                chain_id     TEXT NOT NULL,
                sequence     INTEGER NOT NULL,   -- 0 = the full this chain starts with
                object_key   TEXT NOT NULL,
                stored_bytes INTEGER,
                file_count   INTEGER,
                created_at   INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_backup_file_sets_machine "
            "ON backup_file_sets(machine, chain_id, sequence)"
        )
        # The manifest: one row per file VERSION, not per file. The current state of a
        # machine is the newest row per path across its live chain, minus deletions --
        # which is what lets a restore fetch only the archives it actually needs instead
        # of unpacking the whole chain.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_files (
                set_id  TEXT NOT NULL,
                machine TEXT NOT NULL,
                path    TEXT NOT NULL,      -- the original absolute path on the PC
                size    INTEGER,
                mtime   INTEGER,
                sha256  TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (set_id, path)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_backup_files_machine "
            "ON backup_files(machine, path)"
        )
        # One row per restore an operator asked for. Separate from `backup_runs` because
        # a restore is not a backup with the arrow reversed: it carries a PLAN (which
        # archives, which members inside them) that the hub must be able to re-read later
        # -- the WebDAV download proxy resolves `(restore_id, index)` against it, so an
        # agent can never name an object key of its own choosing. That plan has nowhere to
        # live on a run row.
        #
        # `machine` is where the files are being WRITTEN and `source_machine` is whose
        # backup they came from. The two differ on the case this feature exists for:
        # replacing dead hardware, where yesterday's PC-3 is restored onto a new PC-9.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_restores (
                id             TEXT PRIMARY KEY,
                machine        TEXT NOT NULL,   -- target: where files are written
                source_machine TEXT NOT NULL,   -- whose archives these are
                destination_id TEXT,
                target_dir     TEXT,            -- "" = back to the original locations
                overwrite      INTEGER NOT NULL DEFAULT 0,
                status         TEXT NOT NULL,   -- RUN_STATUSES
                plan_json      TEXT NOT NULL,   -- [{object_key, files:[...]}]
                file_count     INTEGER NOT NULL,
                restored_count INTEGER,
                bytes_restored INTEGER,
                error          TEXT,
                actor          TEXT,
                command_id     TEXT,
                started_at     INTEGER NOT NULL,
                finished_at    INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_backup_restores_machine "
            "ON backup_restores(machine, started_at DESC)"
        )


def get_state(db_path, key, default=None):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT value FROM backup_state WHERE key = ?",
                           (key,)).fetchone()
    return row["value"] if row else default


def set_state(db_path, key, value):
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO backup_state(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, str(value), int(time.time())),
        )


# ================================
# MASTER KEY
# ================================
def generate_master_key():
    """A fresh base64 master key. `os.urandom`, not `random` -- this is the only secret
    standing between the storage provider and every backup."""
    return base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode("ascii")


def decode_master_key(raw):
    """Parse a base64 master key into 32 raw bytes, or raise ValueError.

    The error text is shown to an operator pasting a key into the restore tool, so it
    says what is wrong with what they pasted rather than 'Invalid base64-encoded string'.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("No backup master key is configured.")
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("The backup master key is not valid base64.")
    if len(key) != MASTER_KEY_BYTES:
        raise ValueError(
            f"The backup master key must decode to {MASTER_KEY_BYTES} bytes "
            f"(this one is {len(key)}).")
    return key


def master_key_b64():
    """The configured master key as the operator would write it down, or "".

    Read from the environment every call rather than cached at import: ensure_master_key()
    may create one during the first request of a hub's life, and a cached value would then
    be wrong until restart.
    """
    return os.environ.get(MASTER_KEY_ENV, "").strip()


def load_master_key():
    """The configured master key as raw bytes, or None if there isn't one."""
    raw = master_key_b64()
    return decode_master_key(raw) if raw else None


def key_id(key):
    """A short, non-reversible label for a key, stored in every artifact header.

    Lets a restore say "this file was encrypted with a different key" instead of
    "decryption failed", which is the difference between finding the right key and
    concluding the backup is corrupt. A truncated HMAC rather than a plain hash so the
    label leaks nothing usable about the key itself.
    """
    return hmac.new(key, b"fleethub-backup-key-id", hashlib.sha256).hexdigest()[:16]


def derive_machine_key(master_key, machine):
    """The key ONE machine's file backups are encrypted with. HKDF-SHA256, 32 bytes.

    An agent is given this and never the master key. That distinction is the whole
    blast-radius story for roadmap #1b: a machine has to hold the key it encrypts with,
    so a stolen laptop's key is readable by whoever stole it -- and if that key were the
    master, they would then be able to decrypt the HUB DATABASE backup and every other
    machine's files. Derived, they get exactly what they already had access to.

    Keyed on the lowercased machine name because that is what the rest of the hub treats
    as the machine's identity (see fleet.py's hostname-primary-key model). A machine
    renamed after a backup therefore needs the OLD name to decrypt the old archives,
    which is why the envelope header records the name it derived from rather than
    expecting the reader to know it.

    Written out rather than taking hkdf from `cryptography`: it is nine lines, it is
    exercised by the test suite against a fixed vector, and the C# side has to reimplement
    it anyway (see the agent's BackupEnvelope).
    """
    info = b"fleethub-backup-machine:" + str(machine or "").strip().lower().encode("utf-8")
    # HKDF-Extract with a fixed salt, then one Expand block -- 32 bytes needs exactly one.
    prk = hmac.new(b"fleethub-backup-hkdf-salt", master_key, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def machine_key_for(machine, master_key=None):
    """derive_machine_key against the configured master key, or None if there isn't one."""
    master_key = master_key if master_key is not None else load_master_key()
    if master_key is None:
        return None
    return derive_machine_key(master_key, machine)


def ensure_master_key(env_path):
    """Return (key_b64, created). Generates and persists a key to `.env` if absent.

    Appending to `.env` rather than writing a settings row is the whole point: the key
    must survive the database it protects, and it must not be readable by anything that
    can read the database. Written UTF-8 with no BOM -- `load_dotenv` is called with
    `encoding="utf-8-sig"` so a BOM would be tolerated at the top of the file, but a BOM
    mid-file (which is what a naive PowerShell `Set-Content` append produces) corrupts the
    line it precedes.

    An unwritable `.env` raises: silently keeping the key in memory would mean every hub
    restart generates a new one and yesterday's backup becomes undecryptable.
    """
    existing = os.environ.get(MASTER_KEY_ENV, "").strip()
    if existing:
        decode_master_key(existing)     # validate now, not at the first backup
        return existing, False

    key_b64 = generate_master_key()
    line = f"{MASTER_KEY_ENV}={key_b64}\n"
    try:
        needs_newline = False
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8-sig") as fh:
                current = fh.read()
            needs_newline = bool(current) and not current.endswith("\n")
        with open(env_path, "a", encoding="utf-8", newline="\n") as fh:
            if needs_newline:
                fh.write("\n")
            fh.write(line)
    except OSError as e:
        raise ValueError(
            f"Could not write the backup master key to {env_path}: {e}. Add the line "
            f"'{MASTER_KEY_ENV}=<key>' yourself and restart the hub.")
    os.environ[MASTER_KEY_ENV] = key_b64
    return key_b64, True


# ================================
# SECRET STORE
# ================================
# Destination credentials, encrypted with the master key, in a file beside the database.
# Not in the `settings` table because settings are rendered into a form, dumped by
# as_dict(), and partially shipped to agents in agent_config() -- an S3 secret key has no
# business anywhere near any of that. Not in `.env` either, because destinations are
# created and deleted from the console at runtime and rewriting `.env` on every edit is a
# good way to lose the file that also holds FLASK_SECRET_KEY.
def secrets_path(log_dir):
    return os.path.join(log_dir, SECRETS_FILENAME)


def _read_secret_file(log_dir):
    path = secrets_path(log_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A corrupt store must not take the console down -- every destination will simply
        # report missing credentials, which is a fixable state an operator can see.
        return {}


def _write_secret_file(log_dir, data):
    path = secrets_path(log_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows ignores POSIX modes; the file inherits the log directory's ACL, which
        # is already the directory holding the database. Best effort, never fatal.
        pass


def store_secret(log_dir, master_key, destination_id, secret):
    """Encrypt and persist one destination's credentials.

    The destination id is the AAD, so a secret blob copied from one destination row to
    another fails to decrypt rather than silently authenticating against the wrong
    endpoint.
    """
    _require_crypto()
    plaintext = json.dumps(secret, sort_keys=True).encode("utf-8")
    nonce = os.urandom(12)
    ct = AESGCM(master_key).encrypt(nonce, plaintext, destination_id.encode("utf-8"))
    data = _read_secret_file(log_dir)
    data[destination_id] = {
        "key_id": key_id(master_key),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ct).decode("ascii"),
    }
    _write_secret_file(log_dir, data)


def load_secret(log_dir, master_key, destination_id):
    """Decrypt one destination's credentials, or raise ValueError explaining why not."""
    _require_crypto()
    entry = _read_secret_file(log_dir).get(destination_id)
    if not entry:
        raise ValueError("This destination has no stored credentials. Edit it and enter "
                         "them again.")
    if entry.get("key_id") and entry["key_id"] != key_id(master_key):
        raise ValueError("These credentials were encrypted with a different master key. "
                         "Restore the original BACKUP_MASTER_KEY, or re-enter the "
                         "credentials.")
    try:
        nonce = base64.b64decode(entry["nonce"])
        ct = base64.b64decode(entry["ciphertext"])
        plaintext = AESGCM(master_key).decrypt(nonce, ct,
                                               destination_id.encode("utf-8"))
    except Exception:
        raise ValueError("Stored credentials for this destination could not be "
                         "decrypted. Edit it and enter them again.")
    return json.loads(plaintext.decode("utf-8"))


def delete_secret(log_dir, destination_id):
    data = _read_secret_file(log_dir)
    if data.pop(destination_id, None) is not None:
        _write_secret_file(log_dir, data)


def has_secret(log_dir, destination_id):
    return destination_id in _read_secret_file(log_dir)


def _require_crypto():
    if not CRYPTO_AVAILABLE:
        raise ValueError(
            "The 'cryptography' package is not installed, so backups cannot be "
            "encrypted. Run: pip install -r requirements.txt")


# ================================
# ENVELOPE  (gzip -> AES-256-GCM, streamed)
# ================================
# Layout:
#
#   MAGIC                     6 bytes, b"FHBK1\n"
#   header length             uint32 big-endian
#   header                    UTF-8 JSON, see below
#   repeated, until final:
#       ciphertext length     uint32 big-endian
#       final flag            uint8, 1 on the last chunk only
#       ciphertext            AES-256-GCM output (plaintext + 16-byte tag)
#
# Each chunk gets nonce = 4-byte per-file random prefix || 8-byte big-endian counter, so
# no nonce is ever reused under one data key even across a restart mid-write. The AAD
# binds sha256(header) || counter || final-flag, which is what makes the three attacks
# GCM alone does not cover fail closed:
#
#   * edit the header (swap the wrapped key, claim no compression) -> every chunk fails,
#   * reorder or drop a chunk from the middle              -> counter mismatch, fails,
#   * truncate the file                                    -> no chunk carries final=1,
#                                                             and read_envelope raises.
#
# The last one matters most: a truncated upload is by far the most likely corruption, and
# it is exactly the one a plain per-chunk MAC would happily accept.
def iter_file(fileobj, chunk_bytes=CHUNK_BYTES):
    while True:
        block = fileobj.read(chunk_bytes)
        if not block:
            return
        yield block


def iter_gzip(chunks, level=6):
    """Compress an iterable of byte blocks into gzip-framed blocks.

    Level 6, not 9: a hub database is mostly already-compact integers, and the extra
    minutes 9 costs on a multi-gigabyte VACUUM output buy a couple of percent.
    """
    compressor = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    for block in chunks:
        out = compressor.compress(block)
        if out:
            yield out
    out = compressor.flush()
    if out:
        yield out


def iter_gunzip(chunks):
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    for block in chunks:
        out = decompressor.decompress(block)
        if out:
            yield out
    out = decompressor.flush()
    if out:
        yield out


def _rechunk(chunks, size):
    """Regroup an arbitrary byte stream into fixed-size blocks.

    gzip emits whatever it feels like -- a few bytes, then a megabyte -- and encrypting
    those directly would mean a chunk count (and so a per-chunk 20-byte overhead) driven
    by zlib's internal buffering rather than by the data.
    """
    buffer = bytearray()
    for block in chunks:
        buffer.extend(block)
        while len(buffer) >= size:
            yield bytes(buffer[:size])
            del buffer[:size]
    if buffer:
        yield bytes(buffer)


def write_envelope(chunks, dst, master_key, header_extra=None, chunk_bytes=CHUNK_BYTES):
    """Encrypt `chunks` into `dst`, returning (header, bytes_written, sha256_hex).

    A random per-artifact data key is generated and wrapped with the master key, rather
    than encrypting with the master key directly. It costs one AES call and buys the
    property that no two artifacts share a key stream, and that a future key-rotation
    feature only has to rewrite headers.

    The sha256 returned is of the CIPHERTEXT file -- it is what gets handed to S3 as
    `x-amz-content-sha256` and recorded on the run row, so an operator can verify what
    landed in the bucket is what left the hub. It says nothing about the plaintext, which
    is the GCM tags' job.
    """
    _require_crypto()
    data_key = os.urandom(32)
    wrap_nonce = os.urandom(12)
    wrapped = AESGCM(master_key).encrypt(wrap_nonce, data_key, b"fleethub-backup-wrap")
    nonce_prefix = os.urandom(4)

    header = {
        "v": ENVELOPE_VERSION,
        "cipher": "AES-256-GCM",
        "compression": "gzip",
        "chunk_bytes": int(chunk_bytes),
        "key_id": key_id(master_key),
        "wrap_nonce": base64.b64encode(wrap_nonce).decode("ascii"),
        "wrapped_key": base64.b64encode(wrapped).decode("ascii"),
        "nonce_prefix": base64.b64encode(nonce_prefix).decode("ascii"),
        "created_at": int(time.time()),
    }
    header.update(header_extra or {})
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
    header_digest = hashlib.sha256(header_bytes).digest()

    digest = hashlib.sha256()
    written = 0

    def emit(data):
        nonlocal written
        dst.write(data)
        digest.update(data)
        written += len(data)

    emit(MAGIC)
    emit(struct.pack(">I", len(header_bytes)))
    emit(header_bytes)

    aead = AESGCM(data_key)
    counter = 0
    pending = None
    # One block of lookahead: the final flag has to be authenticated INSIDE the last
    # chunk, so we can only encrypt a block once we know whether another follows.
    for block in _rechunk(chunks, chunk_bytes):
        if pending is not None:
            emit(_seal(aead, nonce_prefix, counter, pending, False, header_digest))
            counter += 1
        pending = block
    emit(_seal(aead, nonce_prefix, counter, pending or b"", True, header_digest))

    return header, written, digest.hexdigest()


def _seal(aead, nonce_prefix, counter, plaintext, final, header_digest):
    nonce = nonce_prefix + struct.pack(">Q", counter)
    aad = header_digest + struct.pack(">Q?", counter, final)
    ct = aead.encrypt(nonce, plaintext, aad)
    return struct.pack(">IB", len(ct), 1 if final else 0) + ct


def read_envelope(src, master_key):
    """Return (header, chunk generator) for an artifact. Raises ValueError on anything
    that isn't a decryptable FHBK1 file.

    `master_key` is always the MASTER key, even for a machine-file archive: if the header
    names a machine, the per-machine key is re-derived here. That is what keeps restore a
    one-argument operation -- `restore_backup.py` never has to be told which machine a
    file came from, because the file says so, and the master key can produce every
    derived key.

    The generator is lazy, so nothing large is held in memory -- but it also means a
    corrupt tail raises while the caller is writing output, which is why restore_backup.py
    writes to a temp file and renames only on success.
    """
    _require_crypto()
    if src.read(len(MAGIC)) != MAGIC:
        raise ValueError("Not a FleetHub backup file (bad magic).")
    raw_len = src.read(4)
    if len(raw_len) != 4:
        raise ValueError("Truncated backup file (no header).")
    header_bytes = src.read(struct.unpack(">I", raw_len)[0])
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("Corrupt backup header.")
    if header.get("v") != ENVELOPE_VERSION:
        raise ValueError(f"Unsupported backup format version {header.get('v')!r}.")

    # A machine archive is sealed with a derived key. Deriving from the master here means
    # the caller passes one key for every artifact type; passing an already-derived key
    # also works, since key_id then already matches.
    unwrap_key = master_key
    if header.get("machine") and header.get("key_id") != key_id(master_key):
        unwrap_key = derive_machine_key(master_key, header["machine"])
    if header.get("key_id") and header["key_id"] != key_id(unwrap_key):
        raise ValueError("This backup was encrypted with a different master key.")
    master_key = unwrap_key

    try:
        data_key = AESGCM(master_key).decrypt(
            base64.b64decode(header["wrap_nonce"]),
            base64.b64decode(header["wrapped_key"]),
            b"fleethub-backup-wrap",
        )
    except Exception:
        raise ValueError("The master key does not decrypt this backup.")

    nonce_prefix = base64.b64decode(header["nonce_prefix"])
    header_digest = hashlib.sha256(header_bytes).digest()
    return header, _envelope_chunks(src, data_key, nonce_prefix, header_digest)


def _envelope_chunks(src, data_key, nonce_prefix, header_digest):
    aead = AESGCM(data_key)
    counter = 0
    while True:
        framing = src.read(5)
        if len(framing) != 5:
            raise ValueError("Truncated backup file -- it has no final chunk, so the "
                             "upload did not complete.")
        length, final_flag = struct.unpack(">IB", framing)
        ct = src.read(length)
        if len(ct) != length:
            raise ValueError("Truncated backup file (short chunk).")
        final = bool(final_flag)
        nonce = nonce_prefix + struct.pack(">Q", counter)
        aad = header_digest + struct.pack(">Q?", counter, final)
        try:
            plaintext = aead.decrypt(nonce, ct, aad)
        except Exception:
            raise ValueError(f"Backup chunk {counter} failed authentication -- the file "
                             f"is corrupt or was tampered with.")
        if plaintext:
            yield plaintext
        counter += 1
        if final:
            return


# ================================
# S3 SIGNATURE VERSION 4
# ================================
_ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def encode_path(path):
    """Percent-encode a URL path, preserving separators.

    S3 signs the path ONCE-encoded (unlike every other AWS service, which encodes twice),
    so this must run EXACTLY once between an object key and the signature. It splits on
    "/" and encodes segments rather than calling quote() on the whole thing with
    safe="/": a key legitimately containing "%" must encode as "%25", which safe="/"
    would leave alone.

    That "exactly once" is the whole contract, and it is why the URL builders call this
    and the signers below do NOT -- they take the already-encoded path straight off the
    URL. Encoding in both places gives "%2520" for a space and a signature that no
    provider will accept.
    """
    if not path:
        return "/"
    return "/".join(quote(segment, safe="") for segment in path.split("/"))


def _canonical_query(query):
    """Sorted, encoded query string. Sorting happens AFTER encoding, per the spec --
    sorting the decoded names gives a different order for anything non-alphanumeric."""
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((quote(unquote(name), safe=""), quote(unquote(value), safe="")))
    pairs.sort()
    return "&".join(f"{name}={value}" for name, value in pairs)


def _signing_key(secret_key, date_stamp, region, service):
    def sign(key, message):
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    key = sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    key = sign(key, region)
    key = sign(key, service)
    return sign(key, "aws4_request")


def sigv4_signature(canonical_request, stamp, region, service, secret_key):
    """The hex signature for an already-built canonical request.

    Split out from the two callers below so the test suite can feed it AWS's published
    `get-vanilla` vector verbatim and compare the signature -- a signer verified only
    against itself is a signer verified against nothing, and the failure mode is a 403
    from a real bucket at 3am rather than a red test.
    """
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        _ALGORITHM, stamp, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(_signing_key(secret_key, stamp[:8], region, service),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return signature, scope


def sigv4_headers(method, url, region, access_key, secret_key, payload_sha256,
                  extra_headers=None, now=None, service="s3"):
    """Authorization + x-amz-* headers for one request. Pure -- `now` is injectable so
    the test suite can assert against AWS's published vectors.

    `url` must already carry a percent-encoded path (encode_path did it when the URL was
    built). Signing what will actually be sent, rather than re-encoding it here, is what
    keeps the signature and the request in agreement.
    """
    parsed = urlsplit(url)
    stamp = time.strftime("%Y%m%dT%H%M%SZ",
                          time.gmtime(time.time() if now is None else now))
    date_stamp = stamp[:8]

    headers = {"host": parsed.netloc, "x-amz-date": stamp,
               "x-amz-content-sha256": payload_sha256}
    for name, value in (extra_headers or {}).items():
        headers[name.lower()] = str(value).strip()

    names = sorted(headers)
    signed_headers = ";".join(names)
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in names)
    canonical_request = "\n".join([
        method,
        parsed.path or "/",
        _canonical_query(parsed.query),
        canonical_headers,
        signed_headers,
        payload_sha256,
    ])
    signature, scope = sigv4_signature(canonical_request, stamp, region, service,
                                       secret_key)

    signed = dict(headers)
    signed["Authorization"] = (f"{_ALGORITHM} Credential={access_key}/{scope}, "
                               f"SignedHeaders={signed_headers}, Signature={signature}")
    return signed


def sigv4_presign(method, url, region, access_key, secret_key, expires_seconds,
                  now=None, service="s3"):
    """A URL carrying its own signature in the query string, valid for `expires_seconds`.

    Nothing in this module calls it: it exists for roadmap #1b, where the hub mints a PUT
    URL scoped to `<prefix>/machines/<machine>/...` and hands it to that machine's agent.
    That is the whole reason the S3 signer is written out here rather than delegated --
    an agent must be able to upload without ever holding the master credential, and the
    same code must therefore be able to sign for a request it does not itself make.

    Only `host` is signed, because the agent controls every other header it sends.
    """
    parsed = urlsplit(url)
    stamp = time.strftime("%Y%m%dT%H%M%SZ",
                          time.gmtime(time.time() if now is None else now))
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"

    query = {
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(int(expires_seconds)),
        "X-Amz-SignedHeaders": "host",
    }
    query_string = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(query.items()))
    canonical_request = "\n".join([
        method,
        parsed.path or "/",
        query_string,
        f"host:{parsed.netloc}\n",
        "host",
        UNSIGNED_PAYLOAD,
    ])
    signature, _ = sigv4_signature(canonical_request, stamp, region, service, secret_key)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{base}?{query_string}&X-Amz-Signature={signature}"


# ================================
# DESTINATIONS
# ================================
class BackupError(Exception):
    """A destination could not be reached, or refused the operation. Carries a message
    written for an operator, because it is rendered verbatim on the run row."""


def _http_error(action, response):
    body = (response.text or "")[:300].replace("\n", " ").strip()
    return BackupError(f"{action} failed: HTTP {response.status_code}"
                       + (f" -- {body}" if body else ""))


class S3Destination:
    """An S3-compatible bucket, signed with SigV4.

    `path_style` exists because MinIO and most self-hosted gateways address buckets as
    `https://host/bucket/key`, while AWS wants `https://bucket.host/key`. Getting it
    wrong produces a signature mismatch rather than a 404, which is an unpleasant thing
    to debug from a log line -- so it is an explicit switch on the destination, not a
    guess from the endpoint hostname.
    """

    kind = KIND_S3

    def __init__(self, config, secret):
        self.endpoint = config["endpoint"].rstrip("/")
        self.bucket = config["bucket"]
        self.region = config.get("region") or "us-east-1"
        self.path_style = bool(config.get("path_style", True))
        self.access_key = secret["access_key_id"]
        self.secret_key = secret["secret_access_key"]

    def _url(self, key="", query=""):
        """The absolute URL for an object, with its path encoded EXACTLY once.

        encode_path runs here and nowhere else on this path -- sigv4_headers signs
        `urlsplit(url).path` verbatim, so what gets signed is byte-for-byte what requests
        puts on the wire. Encoding in both places would produce "%2520" for a space and a
        signature every provider rejects.
        """
        parsed = urlsplit(self.endpoint)
        raw = f"/{self.bucket}/{key}" if self.path_style else f"/{key}"
        if not key:
            raw = f"/{self.bucket}" if self.path_style else "/"
        host = parsed.netloc if self.path_style else f"{self.bucket}.{parsed.netloc}"
        url = f"{parsed.scheme}://{host}{encode_path(raw)}"
        return url + (f"?{query}" if query else "")

    def _request(self, method, url, payload_sha256, data=None, headers=None,
                 stream=False):
        signed = sigv4_headers(method, url, self.region, self.access_key,
                               self.secret_key, payload_sha256, extra_headers=headers)
        try:
            return requests.request(method, url, data=data, headers=signed,
                                    stream=stream,
                                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException as e:
            raise BackupError(f"Could not reach {urlsplit(url).netloc}: {e}")

    def put(self, key, fileobj, size, sha256_hex):
        url = self._url(key)
        headers = {"content-length": str(size),
                   "content-type": "application/octet-stream"}
        response = self._request("PUT", url, sha256_hex, data=fileobj, headers=headers)
        if response.status_code not in (200, 201):
            raise _http_error("Upload", response)

    def open(self, key):
        """A streaming GET. The caller must close the response."""
        response = self._request("GET", self._url(key), EMPTY_SHA256, stream=True)
        if response.status_code != 200:
            raise _http_error("Download", response)
        return response

    def delete(self, key):
        response = self._request("DELETE", self._url(key), EMPTY_SHA256)
        # 204 is the documented success; 404 means someone got there first, which for a
        # rotation pass is the desired end state either way.
        if response.status_code not in (200, 204, 404):
            raise _http_error("Delete", response)

    def list(self, prefix):
        """Every object under `prefix`, as [{"key", "size"}]. Follows continuation
        tokens, so a bucket with more than 1000 generations still rotates correctly."""
        namespace = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        objects = []
        token = None
        while True:
            query = f"list-type=2&prefix={quote(prefix, safe='')}&max-keys=1000"
            if token:
                query += f"&continuation-token={quote(token, safe='')}"
            response = self._request("GET", self._url("", query), EMPTY_SHA256)
            if response.status_code != 200:
                raise _http_error("Listing", response)
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as e:
                raise BackupError(f"Listing returned malformed XML: {e}")
            for node in root.findall(f"{namespace}Contents"):
                name = node.findtext(f"{namespace}Key") or ""
                size = node.findtext(f"{namespace}Size") or "0"
                objects.append({"key": name, "size": int(size)})
            if (root.findtext(f"{namespace}IsTruncated") or "").lower() != "true":
                return objects
            token = root.findtext(f"{namespace}NextContinuationToken")
            if not token:
                return objects

    def presigned_url(self, key, method="PUT", expires_seconds=3600):
        return sigv4_presign(method, self._url(key), self.region, self.access_key,
                             self.secret_key, expires_seconds)


class WebDavDestination:
    """A WebDAV share, authenticated with HTTP Basic over TLS.

    Two differences from S3 shape the code. Collections must exist before a PUT, so
    upload MKCOLs its way down the path (a 405 means "already there", which is success).
    And there is no pre-signed-URL concept at all -- roadmap #1b's per-machine scoping
    here is a per-machine subfolder plus its own credential, minted by the hub, rather
    than a signed URL; that is why `presigned_url` is absent rather than stubbed.
    """

    kind = KIND_WEBDAV

    def __init__(self, config, secret):
        self.base_url = config["base_url"].rstrip("/")
        self.auth = (secret["username"], secret["password"])

    def _url(self, key=""):
        return self.base_url + ("/" + quote(key, safe="/") if key else "")

    def _request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
        try:
            return requests.request(method, url, auth=self.auth, **kwargs)
        except requests.RequestException as e:
            raise BackupError(f"Could not reach {urlsplit(url).netloc}: {e}")

    def _ensure_collections(self, key):
        parts = [p for p in key.split("/")[:-1] if p]
        walked = ""
        for part in parts:
            walked = f"{walked}/{part}" if walked else part
            response = self._request("MKCOL", self._url(walked))
            # 201 created, 405 already exists, 301/302 some servers redirect a collection
            # to its trailing-slash form. Anything else is a real problem worth reporting
            # now rather than as a confusing PUT failure two lines later.
            if response.status_code not in (200, 201, 301, 302, 405):
                raise _http_error(f"Creating folder {walked!r}", response)

    def put(self, key, fileobj, size, sha256_hex):
        self._ensure_collections(key)
        response = self._request(
            "PUT", self._url(key), data=fileobj,
            headers={"content-length": str(size),
                     "content-type": "application/octet-stream"})
        if response.status_code not in (200, 201, 204):
            raise _http_error("Upload", response)

    def open(self, key):
        response = self._request("GET", self._url(key), stream=True)
        if response.status_code != 200:
            raise _http_error("Download", response)
        return response

    def delete(self, key):
        response = self._request("DELETE", self._url(key))
        if response.status_code not in (200, 204, 404):
            raise _http_error("Delete", response)

    def list(self, prefix):
        """PROPFIND with Depth: 1 over the folder holding `prefix`.

        Depth: 1 rather than infinity because most servers (Nextcloud included) refuse
        infinity outright. That is exactly why backup keys are laid out one flat folder
        deep -- `<prefix>/hub-db/<stamp>-...` -- instead of the year/month tree an S3-only
        design would reach for.
        """
        folder = prefix.rstrip("/")
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:"><d:prop>'
                '<d:getcontentlength/><d:resourcetype/>'
                '</d:prop></d:propfind>')
        response = self._request(
            "PROPFIND", self._url(folder), data=body.encode("utf-8"),
            headers={"Depth": "1", "Content-Type": "application/xml"})
        if response.status_code == 404:
            return []      # nothing uploaded yet -- an empty generation list, not an error
        if response.status_code != 207:
            raise _http_error("Listing", response)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as e:
            raise BackupError(f"Listing returned malformed XML: {e}")

        base_path = urlsplit(self.base_url).path.rstrip("/")
        objects = []
        for node in root.findall("{DAV:}response"):
            href = node.findtext("{DAV:}href") or ""
            if node.find(".//{DAV:}collection") is not None:
                continue        # the folder itself, and any subfolder
            path = unquote(urlsplit(href).path)
            if base_path and path.startswith(base_path):
                path = path[len(base_path):]
            key = path.lstrip("/")
            size = node.findtext(".//{DAV:}getcontentlength") or "0"
            try:
                objects.append({"key": key, "size": int(size)})
            except ValueError:
                objects.append({"key": key, "size": 0})
        return objects


def _normalize_prefix(prefix):
    """A prefix with no leading or trailing slashes, so key building is unambiguous."""
    return str(prefix or "").strip().strip("/")


def build_client(record, secret):
    """Turn a destination row plus its decrypted credentials into a client."""
    config = record["config"] if isinstance(record.get("config"), dict) else {}
    if record["kind"] == KIND_S3:
        return S3Destination(config, secret)
    if record["kind"] == KIND_WEBDAV:
        return WebDavDestination(config, secret)
    raise ValueError(f"Unknown destination kind {record['kind']!r}.")


def object_key(prefix, kind, filename, machine=None):
    """Where an artifact lives under a destination's prefix.

    Machine backups get their own folder per machine because that is the unit roadmap
    #1b scopes an upload credential to -- a pre-signed URL or a WebDAV credential is
    minted for `<prefix>/machines/<machine>/`, and nothing outside it.
    """
    parts = [p for p in [_normalize_prefix(prefix)] if p]
    if kind == BACKUP_MACHINE_FILES:
        parts += [MACHINE_FOLDER, str(machine)]
    else:
        parts.append(HUB_DB_FOLDER)
    parts.append(filename)
    return "/".join(parts)


def folder_key(prefix, kind, machine=None):
    """The folder `object_key` puts things in -- what rotation lists."""
    key = object_key(prefix, kind, "", machine=machine)
    return key.rstrip("/")


# ================================
# DESTINATION CRUD
# ================================
_URL_RE = re.compile(r"^https?://[^\s/]+")
# Bucket/prefix/name characters that survive both an S3 key and a WebDAV path without
# needing escaping games. Deliberately strict: a backup path is not a place to discover
# that a provider treats "+" specially.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


def _validate_name(name):
    text = str(name or "").strip()
    if not text:
        raise ValueError("A destination needs a name.")
    if len(text) > MAX_NAME_CHARS:
        raise ValueError(f"Name is limited to {MAX_NAME_CHARS} characters.")
    return text


def _validate_url(value, field):
    """An http(s) URL, refusing plain http anywhere but a loopback host.

    The whole feature is 'backups via HTTPS'. Allowing http to localhost is not a
    loophole -- it is how a MinIO container on the same box is tested -- but allowing it
    to a remote host would silently ship the ciphertext AND the credentials in clear.
    """
    text = str(value or "").strip().rstrip("/")
    if not _URL_RE.match(text):
        raise ValueError(f"{field} must be a URL starting with https://.")
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"{field} must use https:// -- plain http would send your "
                         f"credentials in clear.")
    return text


def _validate_segment(value, field, required=True):
    text = str(value or "").strip().strip("/")
    if not text:
        if required:
            raise ValueError(f"{field} is required.")
        return ""
    if not _SAFE_SEGMENT_RE.match(text):
        raise ValueError(f"{field} may only contain letters, numbers, dot, dash, "
                         f"underscore and /.")
    # The character class above permits "." and "/", so it alone still admits
    # "../../etc". A prefix is concatenated straight into an object key, and for WebDAV
    # that key becomes a URL PATH -- where ".." is resolved by the server, not by us, and
    # would let a prefix walk out of the backup folder and write (or, via rotation,
    # DELETE) somewhere else on the share. Reject the segments rather than trying to
    # normalise them: there is no legitimate prefix that needs either.
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{field} may not contain '.', '..' or empty path segments.")
    return text


def validate_destination(kind, config):
    """Normalize and check one destination's non-secret config. Returns a clean dict.

    Unknown keys are dropped rather than rejected, matching packages.validate_detection:
    a stale field from an older console build should not break the form.
    """
    if kind not in DESTINATION_KINDS:
        raise ValueError(f"Unknown destination kind {kind!r}.")
    config = config or {}
    if kind == KIND_S3:
        return {
            "endpoint": _validate_url(config.get("endpoint"), "Endpoint"),
            "bucket": _validate_segment(config.get("bucket"), "Bucket"),
            "region": _validate_segment(config.get("region"), "Region",
                                        required=False) or "us-east-1",
            "prefix": _validate_segment(config.get("prefix"), "Prefix", required=False),
            "path_style": bool(config.get("path_style", True)),
        }
    return {
        "base_url": _validate_url(config.get("base_url"), "Base URL"),
        "prefix": _validate_segment(config.get("prefix"), "Prefix", required=False),
    }


def validate_secret(kind, secret):
    """Normalize the credential half. Empty means 'keep what is already stored', which
    is what lets the edit form render without ever sending a secret back to the browser."""
    secret = secret or {}
    if kind == KIND_S3:
        access = str(secret.get("access_key_id") or "").strip()
        key = str(secret.get("secret_access_key") or "").strip()
        if not access and not key:
            return None
        if not access or not key:
            raise ValueError("An S3 destination needs both an access key id and a "
                             "secret access key.")
        return {"access_key_id": access, "secret_access_key": key}
    username = str(secret.get("username") or "").strip()
    password = str(secret.get("password") or "")
    if not username and not password:
        return None
    if not username or not password:
        raise ValueError("A WebDAV destination needs both a username and a password.")
    return {"username": username, "password": password}


def _destination_row(row, log_dir=None):
    record = {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "config": json.loads(row["config_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
        "updated_by": row["updated_by"],
    }
    if log_dir is not None:
        # Whether credentials EXIST, never what they are. The console needs to show
        # "credentials missing" on a destination restored without its secret file.
        record["has_credentials"] = has_secret(log_dir, row["id"])
    return record


def list_destinations(db_path, log_dir=None):
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM backup_destinations ORDER BY name COLLATE NOCASE").fetchall()
    return [_destination_row(row, log_dir) for row in rows]


def get_destination(db_path, destination_id, log_dir=None):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM backup_destinations WHERE id = ?",
                           (destination_id,)).fetchone()
    return _destination_row(row, log_dir) if row else None


def create_destination(db_path, log_dir, master_key, *, name, kind, config, secret,
                       actor="system"):
    name = _validate_name(name)
    clean_config = validate_destination(kind, config)
    clean_secret = validate_secret(kind, secret)
    if clean_secret is None:
        raise ValueError("Enter the credentials for this destination.")

    destination_id = uuid.uuid4().hex
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_destinations(id, name, kind, config_json, "
                "created_at, updated_at, created_by, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (destination_id, name, kind, json.dumps(clean_config, sort_keys=True),
                 now, now, actor, actor),
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"A destination named {name!r} already exists.")
    # Written after the row, so a failed insert cannot leave an orphan secret behind.
    store_secret(log_dir, master_key, destination_id, clean_secret)
    fleet.audit(db_path, actor=actor, action="backup_destination_create", target=name,
                detail={"id": destination_id, "kind": kind})
    return destination_id


def update_destination(db_path, log_dir, master_key, destination_id, *, name=None,
                       config=None, secret=None, actor="system"):
    current = get_destination(db_path, destination_id)
    if current is None:
        raise KeyError(destination_id)

    new_name = _validate_name(name) if name is not None else current["name"]
    new_config = (validate_destination(current["kind"], config)
                  if config is not None else current["config"])
    clean_secret = validate_secret(current["kind"], secret)

    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE backup_destinations SET name = ?, config_json = ?, "
                "updated_at = ?, updated_by = ? WHERE id = ?",
                (new_name, json.dumps(new_config, sort_keys=True), int(time.time()),
                 actor, destination_id),
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"A destination named {new_name!r} already exists.")
    if clean_secret is not None:
        store_secret(log_dir, master_key, destination_id, clean_secret)
    fleet.audit(db_path, actor=actor, action="backup_destination_update",
                target=new_name,
                detail={"id": destination_id, "credentials_changed": clean_secret is not None})
    return get_destination(db_path, destination_id, log_dir)


def delete_destination(db_path, log_dir, destination_id, actor="system"):
    current = get_destination(db_path, destination_id)
    if current is None:
        raise KeyError(destination_id)
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM backup_destinations WHERE id = ?", (destination_id,))
    delete_secret(log_dir, destination_id)
    # Run history deliberately survives: it is the record of what was uploaded where, and
    # deleting a destination must not erase the evidence that a backup ever ran. The rows
    # keep the id, which list_runs() resolves to "(deleted destination)".
    fleet.audit(db_path, actor=actor, action="backup_destination_delete",
                target=current["name"], detail={"id": destination_id})


def open_client(db_path, log_dir, destination_id):
    """Resolve a destination id to a ready-to-use client, or raise ValueError."""
    record = get_destination(db_path, destination_id)
    if record is None:
        raise ValueError("That backup destination no longer exists.")
    master_key = load_master_key()
    if master_key is None:
        raise ValueError("No backup master key is configured on this hub.")
    return build_client(record, load_secret(log_dir, master_key, destination_id)), record


def probe_destination(db_path, log_dir, destination_id, actor="system"):
    """Write, read back, and delete a small object. Returns a human-readable summary.

    Deliberately a full round trip rather than a HEAD on the bucket: the failure that
    matters is "the credential can list but not write", and only a write finds it. The
    probe object is named with a random suffix so two operators testing at once cannot
    collide, and is deleted in a finally so a failed read still cleans up.
    """
    client, record = open_client(db_path, log_dir, destination_id)
    prefix = _normalize_prefix(record["config"].get("prefix"))
    key = object_key(prefix, BACKUP_HUB_DB, f".probe-{uuid.uuid4().hex[:12]}")
    payload = b"fleethub backup destination probe"
    digest = hashlib.sha256(payload).hexdigest()

    try:
        client.put(key, io.BytesIO(payload), len(payload), digest)
        response = client.open(key)
        try:
            echoed = response.content
        finally:
            response.close()
        if echoed != payload:
            raise BackupError("The destination accepted the upload but returned "
                              "different bytes on read-back.")
    finally:
        try:
            client.delete(key)
        except BackupError:
            pass
    fleet.audit(db_path, actor=actor, action="backup_destination_test",
                target=record["name"], detail={"id": destination_id})
    return f"Wrote, read back and deleted {key} successfully."


# ================================
# PER-MACHINE FILE BACKUP CONFIG
# ================================
# A machine's row is entirely OPTIONAL -- absent means "follow the fleet defaults", which
# is why every override column is nullable and why nothing here creates rows eagerly. A
# fleet of 400 machines with no per-machine tweaks has an empty table, and the effective
# config is computed rather than materialised.
#
# The two list columns are ADDITIVE, not overriding: `include_json` is EXTRA paths on top
# of the fleet list. Making them replace would mean an operator adding one folder for one
# PC silently drops that PC out of the fleet-wide policy -- the roadmap asks for "per-PC
# extra paths", and additive is what makes that phrase true.
# Passed as `enabled` to set_machine_config to CLEAR the override rather than set it.
# A distinct object rather than a string or -1, so it can never arrive from JSON by
# accident -- the web layer has to translate an explicit null into it deliberately.
FOLLOW_FLEET = object()


def _machine_config_row(row, machine):
    if row is None:
        return {
            "machine": machine,
            "enabled": None,
            "destination_id": None,
            "include": [],
            "exclude": [],
            "profiles": None,
            "reported_at": None,
            "updated_at": None,
            "updated_by": None,
            "run_requested_at": None,
            "run_requested_by": None,
        }
    return {
        "machine": row["machine"],
        "enabled": None if row["enabled"] is None else bool(row["enabled"]),
        "destination_id": row["destination_id"] or None,
        "include": json.loads(row["include_json"] or "[]"),
        "exclude": json.loads(row["exclude_json"] or "[]"),
        "profiles": json.loads(row["profiles_json"]) if row["profiles_json"] else None,
        "reported_at": row["reported_at"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "run_requested_at": row["run_requested_at"],
        "run_requested_by": row["run_requested_by"],
    }


def get_machine_config(db_path, machine):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM backup_machine_config WHERE machine = ?",
                           (machine,)).fetchone()
    return _machine_config_row(row, machine)


def has_overrides(config):
    """Has an operator actually configured this machine, or is the row incidental?

    A row exists for any machine that has merely REPORTED its profiles (see
    record_profiles), which is most of the fleet. Those are not exceptions to the fleet
    policy and listing them as such would bury the handful of machines someone really did
    opt out among hundreds that simply checked in.

    `run_requested_at` is deliberately NOT counted either. A pending "Back up now" is a
    one-shot action, not a policy difference, and an operator who pressed the button on
    thirty offline laptops should not find all thirty listed as exceptions to the fleet
    settings for as long as they stay offline.
    """
    return bool(config["enabled"] is not None or config["destination_id"]
                or config["include"] or config["exclude"])


def list_machine_configs(db_path, overrides_only=True):
    """Machines with a config row. `overrides_only` drops the profile-only rows -- see
    has_overrides for why that is the useful default."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM backup_machine_config ORDER BY machine COLLATE NOCASE"
        ).fetchall()
    configs = [_machine_config_row(row, row["machine"]) for row in rows]
    return [c for c in configs if has_overrides(c)] if overrides_only else configs


def set_machine_config(db_path, machine, *, enabled=None, destination_id=None,
                       include=None, exclude=None, actor="system"):
    """Upsert one machine's overrides. Returns the stored row.

    `None` means "leave alone" for every field, so a caller can toggle `enabled` without
    resending the path lists. Clearing an override back to "follow the fleet" is done by
    passing the sentinel `""` for destination_id or an empty list for the path lists --
    both distinguishable from None.

    `enabled` has no such natural empty value: False is a real setting ("never back this
    machine up"), so FOLLOW_FLEET is the sentinel that clears it. Without one, the
    console's "Follow the fleet policy" option was unreachable -- it sends JSON null,
    which arrived here as "leave alone", so a machine that had once been opted out could
    never be opted back in from the UI.
    """
    machine = str(machine or "").strip()
    if not machine:
        raise ValueError("A machine name is required.")

    current = get_machine_config(db_path, machine)
    if include is not None:
        include = backup_paths.validate_patterns(include, kind="include")
    else:
        include = current["include"]
    if exclude is not None:
        exclude = backup_paths.validate_patterns(exclude, kind="exclude")
    else:
        exclude = current["exclude"]
    if enabled is FOLLOW_FLEET:
        enabled = None
    elif enabled is None:
        enabled = current["enabled"]
    if destination_id is None:
        destination_id = current["destination_id"]
    destination_id = (destination_id or "").strip() or None
    if destination_id and get_destination(db_path, destination_id) is None:
        raise ValueError("That backup destination no longer exists.")

    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO backup_machine_config(machine, enabled, destination_id, "
            "include_json, exclude_json, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET enabled = excluded.enabled, "
            "destination_id = excluded.destination_id, "
            "include_json = excluded.include_json, exclude_json = excluded.exclude_json, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (machine, None if enabled is None else int(enabled), destination_id,
             json.dumps(include), json.dumps(exclude), now, actor),
        )
    fleet.audit(db_path, actor=actor, action="backup_machine_config", target=machine,
                detail={"enabled": enabled, "destination_id": destination_id,
                        "include": include, "exclude": exclude})
    return get_machine_config(db_path, machine)


def request_file_run(db_path, machine, actor="system", now=None):
    """Queue a manual "Back up now" for one machine. Returns the stored epoch.

    This does NOT talk to the machine. It records the intent, and the next dispatch pass
    that finds the machine online turns it into a command. That indirection is the whole
    point: an operator pressing the button on a laptop that is shut in a bag gets a
    backup when the laptop next appears, rather than an error or a command that expires
    unseen fifteen minutes later.

    Re-requesting while one is already pending simply refreshes the timestamp -- there is
    one flag per machine, so the button is idempotent no matter how many times an anxious
    operator presses it.

    Like record_profiles, this touches a row without creating an override: a machine that
    has been asked to back up now has not been configured differently from the fleet.
    """
    machine = str(machine or "").strip()
    if not machine:
        raise ValueError("A machine name is required.")
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO backup_machine_config(machine, run_requested_at, "
            "run_requested_by) VALUES (?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET "
            "run_requested_at = excluded.run_requested_at, "
            "run_requested_by = excluded.run_requested_by",
            (machine, now, actor),
        )
    return now


def clear_file_run_request(db_path, machine):
    """Drop a pending manual request. Called once it has become a real command."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE backup_machine_config SET run_requested_at = NULL, "
            "run_requested_by = NULL WHERE machine = ?", (machine,))


def running_file_runs(db_path):
    """How many machine file backups are in flight right now.

    The dispatcher's throttle reads this. Counting rows rather than tracking a counter
    means a hub restart mid-pass cannot leak capacity, and expire_stale_file_runs is
    already responsible for retiring rows whose agent vanished -- so the number cannot
    drift upward forever.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM backup_runs WHERE kind = ? AND status = ?",
            (BACKUP_MACHINE_FILES, RUN_RUNNING)).fetchone()
    return int(row["n"] if row else 0)


def record_profiles(db_path, machine, profiles):
    """Store what the agent says its user profiles and known folders are.

    Written from the heartbeat, so it is the one thing in this table that is not operator
    input. Its only job is to make the console's path preview honest -- without it the UI
    can only show the pattern back, never what it resolves to on that box.

    Deliberately does NOT create the row's override columns: a machine that has merely
    reported its profiles has not been configured, and must keep following fleet defaults.
    """
    if not isinstance(profiles, dict) or not profiles.get("users"):
        return False
    # Cap what we keep: this is agent-supplied and lands in the database. A machine with
    # a genuinely huge profile list is a machine with a problem, not a machine that needs
    # all of it recorded.
    users = list(profiles.get("users") or [])[:64]
    trimmed = {
        "profile_root": str(profiles.get("profile_root") or "")[:260],
        "env": {str(k)[:64]: str(v)[:260]
                for k, v in list((profiles.get("env") or {}).items())[:32]},
        "users": [{
            "name": str(u.get("name") or "")[:128],
            "sid": str(u.get("sid") or "")[:128],
            "path": str(u.get("path") or "")[:260],
            "folders": {str(k)[:32].lower(): str(v)[:260]
                        for k, v in list((u.get("folders") or {}).items())[:16]},
        } for u in users],
    }
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO backup_machine_config(machine, profiles_json, reported_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET profiles_json = excluded.profiles_json, "
            "reported_at = excluded.reported_at",
            (machine, json.dumps(trimmed), now),
        )
    return True


def effective_file_config(db_path, machine, *, fleet_enabled, fleet_destination,
                          fleet_include, fleet_exclude):
    """What this machine will actually back up, fleet defaults merged with its overrides.

    The fleet-level values are passed in rather than read from settings.py, keeping this
    module settings-free like the rest of it (and testable by handing it four values).
    """
    config = get_machine_config(db_path, machine)
    return {
        "machine": machine,
        "enabled": fleet_enabled if config["enabled"] is None else config["enabled"],
        "destination_id": config["destination_id"] or fleet_destination,
        # Additive, and de-duplicated: a per-machine entry that repeats a fleet one is a
        # no-op rather than a doubled walk of the same tree.
        "include": backup_paths.validate_patterns(
            list(fleet_include or []) + config["include"], kind="include"),
        "exclude": backup_paths.validate_patterns(
            list(fleet_exclude or []) + config["exclude"], kind="exclude"),
        "profiles": config["profiles"],
        "overridden": {
            "enabled": config["enabled"] is not None,
            "destination_id": bool(config["destination_id"]),
        },
    }


def forget_machine(db_path, machine):
    """Drop a deleted machine's backup configuration.

    Its RUN HISTORY and manifest are deliberately left alone -- see delete_destination for
    the same reasoning. A machine record being removed from the console does not mean the
    archives it produced stopped existing, and those are exactly what someone will want
    when they discover the deletion was a mistake.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM backup_machine_config WHERE machine = ?", (machine,))


def rename_machine(db_path, old_name, new_name):
    """Carry configuration across a duplicate-serial merge.

    Mirrors packages.rename_machine and permissions.rename_machine. Note the archives
    themselves stay under the OLD name's folder and derived key -- the envelope header
    records which machine it was sealed for, so they remain restorable; only future runs
    land under the new name.
    """
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM backup_machine_config WHERE machine = ?", (new_name,)
        ).fetchone()
        if existing:
            # The survivor already has its own configuration; the merged-away machine's
            # is dropped rather than silently overwriting a row an operator chose.
            conn.execute("DELETE FROM backup_machine_config WHERE machine = ?",
                         (old_name,))
        else:
            conn.execute("UPDATE backup_machine_config SET machine = ? WHERE machine = ?",
                         (new_name, old_name))
        conn.execute("UPDATE backup_runs SET machine = ? WHERE machine = ?",
                     (new_name, old_name))


# ================================
# RUNS
# ================================
def _run_row(row, names=None):
    record = dict(row)
    record["destination_name"] = (names or {}).get(row["destination_id"])
    return record


def list_runs(db_path, limit=50, kind=None, machine=None):
    query = "SELECT * FROM backup_runs"
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if machine:
        clauses.append("machine = ?")
        params.append(machine)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        names = {r["id"]: r["name"] for r in
                 conn.execute("SELECT id, name FROM backup_destinations")}
    return [_run_row(row, names) for row in rows]


def get_run(db_path, run_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM backup_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        names = {r["id"]: r["name"] for r in
                 conn.execute("SELECT id, name FROM backup_destinations")}
    return _run_row(row, names)


def _start_run(db_path, kind, destination_id, trigger, actor, now, machine=None):
    run_id = uuid.uuid4().hex
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO backup_runs(id, kind, machine, destination_id, status, "
            "trigger, actor, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, machine, destination_id, RUN_RUNNING, trigger, actor, now),
        )
    return run_id


def _finish_run(db_path, run_id, status, **fields):
    columns = ["status = ?", "finished_at = ?"]
    params = [status, int(time.time())]
    for name in ("object_key", "source_bytes", "stored_bytes", "artifact_sha256",
                 "error"):
        if name in fields:
            value = fields[name]
            if name == "error" and value is not None:
                value = str(value)[:MAX_ERROR_CHARS]
            columns.append(f"{name} = ?")
            params.append(value)
    params.append(run_id)
    with get_conn(db_path) as conn:
        conn.execute(f"UPDATE backup_runs SET {', '.join(columns)} WHERE id = ?", params)


def prune_runs(db_path, keep=500):
    """Cap the run history. Unlike readings this grows a row a day, so the limit is a
    tidiness measure rather than a retention policy -- hence a count, not a window."""
    with get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM backup_runs WHERE id NOT IN "
            "(SELECT id FROM backup_runs ORDER BY started_at DESC LIMIT ?)",
            (int(keep),),
        )


# ================================
# HUB DATABASE BACKUP
# ================================
def snapshot_database(db_path, target_path):
    """A consistent copy of the live database at `target_path`, via `VACUUM INTO`.

    Not a file copy: see the module docstring. SQLite writes the snapshot inside a read
    transaction, so ingest keeps running throughout and the result carries no `-wal`
    sidecar -- it is a single self-contained file that opens cleanly.

    The target must not exist; VACUUM INTO refuses to overwrite, which is a guard worth
    keeping rather than working around.
    """
    if os.path.exists(target_path):
        os.remove(target_path)
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute("VACUUM INTO ?", (target_path,))
    finally:
        conn.close()
    return os.path.getsize(target_path)


def artifact_name(kind, now, source_name="temp_v2.db"):
    """`<stamp>-<source>.gz.fhb`, with the stamp first so a lexicographic sort over the
    remote listing is a chronological one. Rotation depends on that -- see rotate()."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return f"{stamp}-{source_name}.gz{FILE_EXTENSION}"


def rotate(client, prefix, kind, keep, machine=None):
    """Delete all but the newest `keep` artifacts in one folder. Returns the keys deleted.

    Ordering comes from the object key, not remote mtime: S3 and WebDAV report modified
    times in different formats and with different clocks, whereas the key carries the
    hub's own UTC stamp. `keep < 1` is refused rather than treated as "delete everything"
    -- a rotation policy that empties the bucket is never what was meant.
    """
    keep = int(keep)
    if keep < 1:
        raise ValueError("Keep at least one backup generation.")
    folder = folder_key(prefix, kind, machine=machine)
    objects = client.list(folder + "/")
    artifacts = sorted((o["key"] for o in objects if o["key"].endswith(FILE_EXTENSION)),
                       reverse=True)
    doomed = artifacts[keep:]
    for key in doomed:
        client.delete(key)
    return doomed


# One hub database backup at a time, process-wide. The scheduler thread and an operator
# pressing "Back up now" are genuinely concurrent, and two passes would mean two full
# snapshots on disk, two uploads competing for the link, and -- worst -- two rotations
# reading the same listing and each deleting what the other just wrote. A non-blocking
# acquire rather than a queue: the second caller wants to be told "already running", not
# to have its request block for four minutes.
_RUN_LOCK = threading.Lock()


def backup_in_progress():
    return _RUN_LOCK.locked()


def backup_hub_database(db_path, log_dir, destination_id, *, keep=14,
                        trigger=TRIGGER_MANUAL, actor="system", now=None,
                        hub_version="", chunk_bytes=CHUNK_BYTES):
    """Snapshot -> gzip -> encrypt -> upload -> rotate. Returns the finished run row.

    Returns None without recording anything if another backup is already running -- see
    _RUN_LOCK. That is a "come back later", not a failure, so it deliberately does not
    leave a red run row behind.

    Never raises for an expected failure: an unreachable endpoint, a bad credential or a
    full disk all land as a `failed` run row carrying the message, because the scheduler
    calls this on a background thread where an exception is just a log line nobody reads.
    Programming errors still propagate.

    The artifact is built to a temp file before any upload starts. Streaming
    snapshot->gzip->encrypt straight into the socket would avoid the temp space, but it
    would also mean a mid-stream failure leaves a partial object in the bucket that looks
    exactly like a good one to the next rotation pass. Local temp is cheap; a rotation
    that keeps a truncated generation and deletes a good one is not.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        return None
    try:
        return _backup_hub_database(db_path, log_dir, destination_id, keep=keep,
                                    trigger=trigger, actor=actor, now=now,
                                    hub_version=hub_version, chunk_bytes=chunk_bytes)
    finally:
        _RUN_LOCK.release()


def _backup_hub_database(db_path, log_dir, destination_id, *, keep, trigger, actor,
                         now, hub_version, chunk_bytes):
    now = int(time.time() if now is None else now)
    run_id = _start_run(db_path, BACKUP_HUB_DB, destination_id, trigger, actor, now)
    set_state(db_path, LAST_ATTEMPT_STATE_KEY, now)

    workdir = os.path.join(log_dir, "backup-work")
    snapshot = os.path.join(workdir, f"snapshot-{run_id}.db")
    artifact = os.path.join(workdir, f"artifact-{run_id}{FILE_EXTENSION}")
    key = None
    try:
        master_key = load_master_key()
        if master_key is None:
            raise ValueError("No backup master key is configured on this hub.")
        client, record = open_client(db_path, log_dir, destination_id)
        os.makedirs(workdir, exist_ok=True)

        source_bytes = snapshot_database(db_path, snapshot)
        with open(snapshot, "rb") as src, open(artifact, "wb") as dst:
            _, stored_bytes, digest = write_envelope(
                iter_gzip(iter_file(src, chunk_bytes)), dst, master_key,
                header_extra={
                    "kind": BACKUP_HUB_DB,
                    "source": os.path.basename(db_path),
                    "source_bytes": source_bytes,
                    "hub_version": hub_version,
                },
                chunk_bytes=chunk_bytes,
            )
        # The snapshot is a full plaintext copy of the database. Remove it as soon as the
        # ciphertext exists, rather than at the end of the run: it is the one file in this
        # process that would be worth stealing.
        os.remove(snapshot)

        key = object_key(record["config"].get("prefix"), BACKUP_HUB_DB,
                         artifact_name(BACKUP_HUB_DB, now, os.path.basename(db_path)))
        with open(artifact, "rb") as body:
            client.put(key, body, stored_bytes, digest)

        removed = rotate(client, record["config"].get("prefix"), BACKUP_HUB_DB, keep)

        _finish_run(db_path, run_id, RUN_SUCCEEDED, object_key=key,
                    source_bytes=source_bytes, stored_bytes=stored_bytes,
                    artifact_sha256=digest, error=None)
        set_state(db_path, LAST_SUCCESS_STATE_KEY, int(time.time()))
        fleet.audit(db_path, actor=actor, action="backup_hub_db", target=key,
                    detail={"run_id": run_id, "destination": record["name"],
                            "stored_bytes": stored_bytes, "rotated_out": len(removed),
                            "trigger": trigger})
    except (BackupError, ValueError, OSError, sqlite3.Error) as e:
        _finish_run(db_path, run_id, RUN_FAILED, object_key=key, error=str(e))
        fleet.audit(db_path, actor=actor, action="backup_hub_db_failed", target=key,
                    detail={"run_id": run_id, "error": str(e)[:MAX_ERROR_CHARS]})
    finally:
        for path in (snapshot, artifact):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
    return get_run(db_path, run_id)


# ================================
# PER-PC FILE BACKUPS: CHAINS, MANIFEST, SCHEDULER
# ================================
# A CHAIN is one full backup plus the incrementals that follow it. Every archive is a set
# row; `sequence` 0 is the full. This shape exists because user folders are large and
# re-uploading Documents every night is not viable, but it buys that with the one genuinely
# dangerous property in this feature: an incremental is USELESS without its full.
#
# So two rules are enforced here rather than left to the caller:
#
#   * rotation deletes WHOLE CHAINS, never an archive within one (rotate_chains), and
#   * the agent decides full-vs-incremental, but the hub refuses to record an incremental
#     whose chain has no full (record_file_set), because a manifest that references a set
#     that was never uploaded restores to a hole.
#
# The manifest is one row per file VERSION. A machine's current state is the newest row
# per path across its live chains, minus deletions -- which is what lets a restore fetch
# only the archives it actually needs instead of unpacking every generation.
COMMAND_BACKUP_FILES = "backup_files"
COMMAND_RESTORE_FILES = "restore_files"

# How long a minted upload URL is good for. Long enough for a slow link to finish a
# multi-gigabyte archive, short enough that a URL scraped from a log is not a standing
# grant. The agent requests it at dispatch and uploads immediately.
UPLOAD_URL_TTL_SECONDS = 6 * 60 * 60

# How many machine backups may be in flight at once, fleet-wide. This exists because of
# catch-up: forty laptops that were shut all weekend come online within a few minutes of
# each other on Monday, and without a throttle every one of them starts pushing a full
# backup up the same office uplink at 09:00. Dispatch is per-tick, so the queue drains
# steadily instead -- at a 60s tick, a cap of 3 still clears forty machines in about
# fifteen minutes. 0 means unlimited.
DEFAULT_MAX_CONCURRENT_FILE_RUNS = 3

MAX_MANIFEST_ROWS = 200_000


def new_chain_id():
    return uuid.uuid4().hex


def machine_chains(db_path, machine):
    """This machine's chains, newest first: [{chain_id, sets, started_at, complete}].

    Ordering is (newest archive time, then insertion order). The rowid tiebreak is
    load-bearing rather than tidiness: `created_at` has one-second granularity, and
    rotation deletes whichever chains sort last. Two chains written in the same second --
    a manual backup right after a scheduled one, or any test -- would otherwise order
    arbitrarily, and rotation would be free to delete the NEWER of the two. rowid is
    monotonic per insert, so it breaks the tie the way wall-clock time meant to.
    """
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT *, rowid AS _rowid FROM backup_file_sets WHERE machine = ? "
            "ORDER BY created_at DESC, rowid DESC", (machine,))]
    chains = {}
    for row in rows:
        chain = chains.setdefault(row["chain_id"], {
            "chain_id": row["chain_id"], "sets": [], "started_at": row["created_at"],
        })
        chain["sets"].append(row)
        chain["started_at"] = min(chain["started_at"], row["created_at"])
    out = []
    for chain in chains.values():
        chain["sets"].sort(key=lambda s: s["sequence"])
        # A chain missing its sequence-0 full cannot be restored from. record_file_set
        # refuses to create that state, so this is a consistency check rather than an
        # expected case -- but rotation must know about it either way, since deleting the
        # "newest N" chains while one of them is unusable would keep a chain that restores
        # to nothing.
        chain["complete"] = bool(chain["sets"]) and chain["sets"][0]["sequence"] == 0
        chain["latest_at"] = max(s["created_at"] for s in chain["sets"])
        chain["_order"] = max(s["_rowid"] for s in chain["sets"])
        out.append(chain)
    out.sort(key=lambda c: (c["latest_at"], c["_order"]), reverse=True)
    return out


def latest_chain(db_path, machine):
    """The chain a new incremental would extend, or None if a full is needed."""
    chains = machine_chains(db_path, machine)
    return chains[0] if chains and chains[0]["complete"] else None


def plan_next_run(db_path, machine, full_every):
    """Decide whether the next run is a full or an incremental.

    Returns {chain_id, sequence, full}. A full is forced when there is no usable chain,
    or when the current one has reached `full_every` archives -- a long chain restores
    slowly and is more exposed to a single damaged archive, so the cap is a reliability
    knob rather than a bandwidth one.
    """
    full_every = max(1, int(full_every))
    chain = latest_chain(db_path, machine)
    if chain is None or len(chain["sets"]) >= full_every:
        return {"chain_id": new_chain_id(), "sequence": 0, "full": True}
    return {
        "chain_id": chain["chain_id"],
        "sequence": max(s["sequence"] for s in chain["sets"]) + 1,
        "full": False,
    }


def record_file_set(db_path, *, run_id, machine, chain_id, sequence, object_key,
                    stored_bytes, files):
    """Record one uploaded archive and the file versions inside it.

    `files` is the agent's manifest: [{path, size, mtime, sha256, deleted}]. Written in
    one transaction with the set row, so there is never a set the manifest does not
    describe or a manifest row pointing at a set that was not recorded.

    Refuses an incremental whose chain has no full -- see the section comment. That is a
    "the agent and the hub disagree about state" condition, and recording it would produce
    a manifest that restores to a hole.
    """
    sequence = int(sequence)
    if sequence > 0:
        with get_conn(db_path) as conn:
            base = conn.execute(
                "SELECT 1 FROM backup_file_sets WHERE chain_id = ? AND sequence = 0",
                (chain_id,)).fetchone()
        if base is None:
            raise ValueError(
                f"Refusing to record incremental {sequence} for chain {chain_id}: its "
                f"full backup was never recorded, so nothing in it could be restored.")

    set_id = uuid.uuid4().hex
    now = int(time.time())
    rows = []
    for entry in (files or [])[:MAX_MANIFEST_ROWS]:
        path = backup_paths.normalize(entry.get("path"))
        if not path:
            continue
        rows.append((set_id, machine, path,
                     int(entry.get("size") or 0), int(entry.get("mtime") or 0),
                     str(entry.get("sha256") or "")[:64],
                     1 if entry.get("deleted") else 0))

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO backup_file_sets(id, machine, run_id, chain_id, sequence, "
            "object_key, stored_bytes, file_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (set_id, machine, run_id, chain_id, sequence, object_key,
             int(stored_bytes or 0), len(rows), now),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO backup_files(set_id, machine, path, size, mtime, "
            "sha256, deleted) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return set_id


# The newest surviving version of every path a machine has backed up.
#
# Done in SQL rather than by loading every row and de-duplicating in Python, because the
# restore browser calls it per folder click on a manifest that is legitimately hundreds of
# thousands of rows -- one row per file VERSION, not per file.
#
# "Newest" is (created_at, sequence, rowid) DESCENDING. The rowid tiebreak is the same
# load-bearing one machine_chains explains: created_at has one-second granularity, so two
# sets written in the same second would otherwise resolve arbitrarily and a file could
# read back its older version.
_LATEST_VERSIONS_SQL = """
    SELECT path, size, mtime, sha256, deleted, set_id, chain_id, sequence,
           created_at, object_key
    FROM (
        SELECT f.path, f.size, f.mtime, f.sha256, f.deleted, f.set_id,
               s.chain_id, s.sequence, s.created_at, s.object_key,
               ROW_NUMBER() OVER (
                   PARTITION BY LOWER(f.path)
                   ORDER BY s.created_at DESC, s.sequence DESC, s.rowid DESC
               ) AS rank
        FROM backup_files f JOIN backup_file_sets s ON s.id = f.set_id
        WHERE f.machine = ? {extra}
    )
    WHERE rank = 1 AND deleted = 0
"""


def _like_prefix(prefix):
    """A LIKE pattern matching everything UNDER a folder, with the wildcards escaped.

    `%` and `_` are legal in Windows filenames (`%TEMP%.log`, `my_notes`), so a raw
    concatenation would turn a folder name into a wildcard and hand back files from
    somewhere else. `!` is the escape character rather than the usual `\\`, which is the
    path separator here and would need escaping itself on every single segment.
    """
    escaped = (str(prefix).replace("!", "!!").replace("%", "!%").replace("_", "!_"))
    return escaped + "\\%"


def _latest_versions(db_path, machine, under=None, contains=None):
    """Newest surviving version of each path, optionally filtered.

    Both filters are applied INSIDE the window query rather than to its output. That is
    not just speed: filtering afterwards would be identical here (a path's versions all
    share the path), and doing it in SQL keeps the row set that reaches Python bounded by
    what was asked for rather than by the size of the whole manifest.
    """
    params = [machine]
    clauses = []
    if under:
        # The folder ITSELF is never a manifest row (only files are), so this is a pure
        # "starts with <folder>\" test -- no need to also match the bare prefix.
        clauses.append("AND f.path LIKE ? ESCAPE '!'")
        params.append(_like_prefix(under))
    if contains:
        clauses.append("AND LOWER(f.path) LIKE ? ESCAPE '!'")
        params.append("%" + str(contains).replace("!", "!!").replace("%", "!%")
                      .replace("_", "!_") + "%")
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(
            _LATEST_VERSIONS_SQL.format(extra=" ".join(clauses)), params)]


def current_manifest(db_path, machine):
    """The machine's current state: newest version of each path, deletions removed."""
    return _latest_versions(db_path, machine)


def manifest_summary(db_path, machine):
    """Totals for the restore browser's header: how much is actually recoverable."""
    rows = _latest_versions(db_path, machine)
    chains = machine_chains(db_path, machine)
    return {
        "file_count": len(rows),
        "total_bytes": sum(int(r["size"] or 0) for r in rows),
        "latest_at": max((int(r["created_at"] or 0) for r in rows), default=None),
        "chains": len(chains),
        "archives": sum(len(c["sets"]) for c in chains),
    }


def manifest_listing(db_path, machine, prefix="", limit=2000):
    """One folder of the manifest: its subfolders and its files.

    A folder at a time rather than the whole manifest, because a profile is 100k-500k
    files and no browser wants that in one response -- and an operator restoring a
    Documents folder does not want to scroll it either.

    Folders are DERIVED, not stored: only files have manifest rows, so a directory exists
    exactly when something under it does. That is the honest definition here -- an empty
    folder was never backed up (tar carries no directory entries from this feature), so
    offering it as restorable would be a lie.
    """
    prefix = backup_paths.normalize(prefix)
    rows = _latest_versions(db_path, machine, under=prefix or None)

    dirs, files = {}, []
    head_len = len(prefix) + 1 if prefix else 0
    for row in rows:
        remainder = row["path"][head_len:] if prefix else row["path"]
        name, sep, _ = remainder.partition("\\")
        if not name:
            continue
        if sep:
            folder = dirs.setdefault(name.lower(), {
                "name": name,
                "path": f"{prefix}\\{name}" if prefix else name,
                "file_count": 0,
                "total_bytes": 0,
            })
            folder["file_count"] += 1
            folder["total_bytes"] += int(row["size"] or 0)
        else:
            files.append({
                "name": name,
                "path": row["path"],
                "size": int(row["size"] or 0),
                "mtime": row["mtime"],
                "sha256": row["sha256"],
                "created_at": row["created_at"],
                "chain_id": row["chain_id"],
                "sequence": row["sequence"],
            })

    files.sort(key=lambda f: f["name"].lower())
    return {
        "path": prefix,
        "parents": _parents_of(prefix),
        "dirs": sorted(dirs.values(), key=lambda d: d["name"].lower()),
        "files": files[:limit],
        "truncated": len(files) > limit,
        "file_count": len(files),
    }


def _parents_of(prefix):
    """Breadcrumbs for a folder: every ancestor, outermost first, excluding itself."""
    if not prefix:
        return []
    parts = prefix.split("\\")
    out, walked = [], ""
    for part in parts[:-1]:
        walked = f"{walked}\\{part}" if walked else part
        out.append({"name": part, "path": walked})
    return out


def manifest_search(db_path, machine, query, limit=500):
    """Files whose path contains `query`. Capped -- this is a finder, not a dump.

    Sorted before the cap is applied, so "the first 500" is a stable answer rather than
    whichever 500 the query planner happened to emit first -- an operator who searches
    twice and gets two different lists stops trusting the browser.
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return {"query": "", "files": [], "truncated": False}
    hits = [{
        "name": row["path"].rsplit("\\", 1)[-1],
        "path": row["path"],
        "size": int(row["size"] or 0),
        "mtime": row["mtime"],
        "sha256": row["sha256"],
        "created_at": row["created_at"],
    } for row in _latest_versions(db_path, machine, contains=needle)]
    hits.sort(key=lambda f: f["path"].lower())
    return {"query": needle, "files": hits[:limit], "truncated": len(hits) > limit}


def rotate_chains(client, prefix, machine, keep_chains, db_path):
    """Delete the oldest chains beyond `keep_chains`. Returns the object keys removed.

    THE sharp edge of the whole feature. The hub-database rotation counts objects, which
    would here happily delete a chain's full backup and leave four incrementals that can
    never be restored -- worse than deleting all five, because the console would still
    list them. So this works in units of chains: whole chains go, or nothing does.

    Reads the DATABASE rather than the remote listing (unlike the hub-DB rotate) because
    chain membership is hub-side knowledge -- an object key alone does not say which full
    an incremental belongs to. The remote is still the source of truth for what EXISTS; a
    key already gone deletes as a no-op.

    A partial delete is survivable BY CONSTRUCTION, in two steps:

      * a chain's archives are deleted **newest sequence first, the full LAST**, so a
        failure partway through leaves a PREFIX of the chain -- full plus incrementals
        0..k -- which is still a valid, restorable chain describing an older moment. The
        obvious order (full first) leaves the exact state this whole module exists to
        prevent: orphaned incrementals whose base is gone.
      * the manifest rows are dropped **per archive, immediately after that archive is
        actually gone**, so the database always describes what storage really holds. The
        old code deleted every object and then every row, and an exception in between left
        the console offering a restore that 404s halfway.

    A chain that only partly deleted is simply still over the limit, so the next pass
    retries the rest -- no bookkeeping of pending deletions is needed. The last error is
    re-raised once every chain has been reconciled, so the caller still logs the failure
    (ingest_file_result does) rather than a rotation silently degrading.
    """
    keep_chains = int(keep_chains)
    if keep_chains < 1:
        raise ValueError("Keep at least one backup chain.")
    chains = machine_chains(db_path, machine)
    doomed = chains[keep_chains:]
    if not doomed:
        return []

    removed = []
    failure = None
    for chain in doomed:
        for file_set in sorted(chain["sets"], key=lambda s: s["sequence"], reverse=True):
            try:
                client.delete(file_set["object_key"])
            except BackupError as e:
                # Stop THIS chain here: everything below this archive is its base, and
                # deleting a base while this one survives is the orphan state above.
                failure = e
                break
            removed.append(file_set["object_key"])
            # The manifest rows go with the archive: a path whose only surviving version
            # lived in a deleted set is genuinely no longer restorable, and leaving it
            # listed would offer the operator a restore that 404s.
            with get_conn(db_path) as conn:
                conn.execute("DELETE FROM backup_files WHERE set_id = ?",
                             (file_set["id"],))
                conn.execute("DELETE FROM backup_file_sets WHERE id = ?",
                             (file_set["id"],))
    if failure is not None:
        raise failure
    return removed


def build_file_command_params(*, machine, run_id, plan, config, destination, machine_key,
                              object_key, upload, limits):
    """The `backup_files` command params: everything the agent needs, and nothing else.

    A SNAPSHOT of the policy at dispatch, not a pointer to it -- the same reasoning as
    packages.build_command_params. An operator editing the include list mid-run must not
    give one machine a half-old, half-new definition of what was backed up, because the
    manifest recorded afterwards would then describe neither.

    `run_id` is what the agent POSTs its manifest back against. It is carried explicitly
    rather than left to be parsed out of the upload URL: that only works for the WebDAV
    (hub-proxied) shape, and an S3 pre-signed URL contains no run id at all.

    `machine_key` is this machine's DERIVED key, never the master. See derive_machine_key.
    """
    return {
        "machine": machine,
        "run_id": run_id,
        "chain_id": plan["chain_id"],
        "sequence": plan["sequence"],
        "full": plan["full"],
        "include": list(config["include"]),
        "exclude": list(config["exclude"]),
        "object_key": object_key,
        "upload": upload,           # {"kind": "s3"|"hub", "url": ...}
        "encryption": {
            "algorithm": "AES-256-GCM",
            "key": base64.b64encode(machine_key).decode("ascii"),
            "key_id": key_id(machine_key),
        },
        "destination_kind": destination["kind"],
        "limits": limits,
    }


def files_due_at(db_path, machine, interval_hours):
    """When this machine's next file backup is due, as an epoch.

    Anchored on the last ATTEMPT for the same reason the hub-DB schedule is: a machine
    that has been failing for a week should not be retried every tick. A machine that has
    never run is due immediately.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(started_at) AS last FROM backup_runs "
            "WHERE kind = ? AND machine = ?",
            (BACKUP_MACHINE_FILES, machine)).fetchone()
    last = row["last"] if row else None
    return 0 if not last else int(last) + int(interval_hours) * 3600


def start_file_run(db_path, machine, destination_id, plan, trigger, actor, now,
                   object_key=None, command_id=None):
    """Open a `running` row for a machine backup, before the command is queued.

    Written BEFORE dispatch, like packages' claim-then-queue: a crash between the two
    then costs one visible failed run rather than a second backup nobody expected.

    The `object_key` is stored HERE, at dispatch, because it is the key the hub minted an
    upload URL for. ingest_file_result reads it back from this row rather than believing
    the agent's report -- otherwise a compromised agent could have its archive recorded
    under another machine's key and quietly poison that machine's manifest.
    """
    run_id = uuid.uuid4().hex
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO backup_runs(id, kind, machine, destination_id, status, trigger, "
            "actor, started_at, chain_id, sequence, object_key, command_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, BACKUP_MACHINE_FILES, machine, destination_id, RUN_RUNNING, trigger,
             actor, now, plan["chain_id"], plan["sequence"], object_key, command_id),
        )
    return run_id


def attach_command(db_path, run_id, command_id):
    with get_conn(db_path) as conn:
        conn.execute("UPDATE backup_runs SET command_id = ? WHERE id = ?",
                     (command_id, run_id))


def complete_file_run(db_path, run_id, *, object_key=None, stored_bytes=None,
                      file_count=None, error=None):
    """Close a machine run. `error` set means failed, absent means succeeded."""
    status = RUN_FAILED if error else RUN_SUCCEEDED
    _finish_run(db_path, run_id, status, object_key=object_key,
                stored_bytes=stored_bytes, error=error)
    if file_count is not None:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE backup_runs SET file_count = ? WHERE id = ?",
                         (int(file_count), run_id))
    return get_run(db_path, run_id)


def expire_stale_file_runs(db_path, now=None, max_seconds=24 * 60 * 60):
    """Fail runs whose agent never reported back.

    Without this a machine that goes offline mid-backup stays `running` forever, and
    because files_due_at anchors on the last ATTEMPT it would also never be retried --
    the machine would silently stop being backed up, which is precisely the failure this
    feature must not have. Returns how many were retired.
    """
    now = int(time.time() if now is None else now)
    cutoff = now - int(max_seconds)
    with get_conn(db_path) as conn:
        stale = [r["id"] for r in conn.execute(
            "SELECT id FROM backup_runs WHERE kind = ? AND status = ? AND started_at < ?",
            (BACKUP_MACHINE_FILES, RUN_RUNNING, cutoff))]
    for run_id in stale:
        _finish_run(db_path, run_id, RUN_FAILED,
                    error="The machine never reported a result for this backup.")
    return len(stale)


# ================================
# SCHEDULER
# ================================
def next_due_at(db_path, interval_hours):
    """When the next scheduled hub backup is due, as an epoch.

    Anchored on the last ATTEMPT, not the last success. Anchoring on success would mean a
    destination that has been down for a week gets retried on every single tick, hammering
    an endpoint that is already unhappy and filling the run list with noise; the operator
    is told about the failure by the run row either way.
    """
    last = get_state(db_path, LAST_ATTEMPT_STATE_KEY)
    if last is None:
        return 0        # never run: due immediately
    return int(last) + int(interval_hours) * 3600


def mint_upload(db_path, log_dir, destination_id, object_key, hub_url="", run_id=""):
    """Where the agent should PUT its archive, without ever holding the shared credential.

    Two shapes, because the two destination kinds genuinely differ:

      * **S3** -- a pre-signed PUT URL scoped to this exact object key. The agent can
        upload and do nothing else: it cannot list the bucket, cannot read another
        machine's archive, and cannot write outside its own folder. This is the whole
        reason the SigV4 signer was written out by hand in #1a.

      * **WebDAV** -- there is no pre-signed-URL concept, and minting a scoped credential
        needs provider-specific admin APIs (Nextcloud app passwords and friends) that do
        not generalise. So the agent PUTs to the HUB, authenticated with the bearer token
        it already has, and the hub streams it onward. Slower and it costs hub bandwidth,
        but the alternative -- handing every agent the share's real password -- is exactly
        the thing this design exists to avoid.
    """
    record = get_destination(db_path, destination_id)
    if record is None:
        raise ValueError("That backup destination no longer exists.")
    if record["kind"] == KIND_S3:
        master_key = load_master_key()
        if master_key is None:
            raise ValueError("No backup master key is configured on this hub.")
        client = build_client(record, load_secret(log_dir, master_key, destination_id))
        return {"kind": "s3",
                "url": client.presigned_url(object_key, method="PUT",
                                            expires_seconds=UPLOAD_URL_TTL_SECONDS),
                "expires_in": UPLOAD_URL_TTL_SECONDS}
    return {"kind": "hub",
            "url": f"{hub_url.rstrip('/')}/api/agent/backups/upload/{run_id}",
            "expires_in": UPLOAD_URL_TTL_SECONDS}


def roster_entry(entry):
    """Normalise one roster element to (machine, online).

    The roster may be a list of names or of {"machine", "online"} dicts. A bare name
    counts as online, which keeps every existing caller and test working and means the
    degenerate case is "behave as before" rather than "silently back nothing up".
    """
    if isinstance(entry, dict):
        return str(entry.get("machine") or "").strip(), bool(entry.get("online", True))
    return str(entry or "").strip(), True


def files_dispatch_once(db_path, log_dir, *, fleet_enabled, fleet_destination,
                        fleet_include, fleet_exclude, interval_hours, full_every,
                        limits, machines, now=None, hub_url="",
                        max_concurrent=DEFAULT_MAX_CONCURRENT_FILE_RUNS,
                        ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS):
    """Queue a `backup_files` command for every ONLINE machine that is due or has been
    asked to back up now. Returns the count.

    `machines` is the roster, passed in -- this module does not know how to enumerate the
    fleet (that is machine_info, which app.py owns) and should not learn. Entries carry
    an `online` flag; see roster_entry.

    **Offline machines are skipped, and skipping them is the catch-up mechanism.**
    Dispatching to a machine that cannot answer used to be actively harmful, not merely
    useless: start_file_run stamps `started_at = now`, files_due_at anchors on the newest
    attempt, so queuing a command into the void reset the machine's clock and pushed the
    next real attempt out by a full interval. A laptop that was closed at 03:00 therefore
    missed that night AND the following one. It also burned the six-hour pre-signed
    upload URL minted alongside it, so even an agent that reconnected later got an
    archive it could no longer upload. Because due-ness and the manual-request flag are
    both persistent state, doing nothing while a machine is unreachable leaves it due --
    and the first pass after it reappears (within one tick, so a minute) dispatches it.

    Manual requests are served before scheduled ones. During a Monday-morning catch-up
    the throttle below can hold a queue for several minutes, and an operator who just
    pressed "Back up now" on the machine in front of them should not wait behind thirty
    laptops that are merely due.

    Per machine, in order: resolve the effective policy, skip if disabled/not due/not
    online, open the run row, mint an upload, then queue the command. Run-row-before-
    command is the same claim-then-queue discipline packages.dispatch_once uses -- a
    crash between the two leaves one visible failed run rather than a backup that ran
    with nothing recording it.

    One machine's failure never stops the pass: a bad path pattern on PC-3 must not mean
    PC-4 goes unbacked-up tonight.
    """
    now = int(time.time() if now is None else now)

    # Phase 1 -- decide who wants to run. Cheap reads only; nothing here mints a URL or
    # writes a run row, so a machine that is skipped by the throttle below is left in
    # exactly the state it started in and will be picked up by a later pass.
    candidates = []
    for entry in machines or []:
        machine, online = roster_entry(entry)
        if not machine:
            continue
        try:
            config = effective_file_config(
                db_path, machine, fleet_enabled=fleet_enabled,
                fleet_destination=fleet_destination, fleet_include=fleet_include,
                fleet_exclude=fleet_exclude)
            if not config["enabled"] or not config["destination_id"]:
                continue
            if not config["include"]:
                continue        # nothing selected: not a failure, just nothing to do
            stored = get_machine_config(db_path, machine)
            requested = stored["run_requested_at"]
            if not requested and now < files_due_at(db_path, machine, interval_hours):
                continue
            if not online:
                continue        # stays due / stays requested -- see the docstring
            candidates.append((0 if requested else 1, machine, config, stored))
        except Exception as e:
            print(f"[backup] Could not evaluate a file backup for {machine}: {e}")
    candidates.sort(key=lambda c: c[0])

    # Phase 2 -- dispatch as far as the throttle allows.
    dispatched = 0
    in_flight = running_file_runs(db_path)
    for _, machine, config, stored in candidates:
        if max_concurrent and in_flight >= max_concurrent:
            break
        try:
            destination = get_destination(db_path, config["destination_id"])
            if destination is None:
                continue
            machine_key = machine_key_for(machine)
            if machine_key is None:
                continue        # no master key yet; the console already says so loudly

            manual = bool(stored["run_requested_at"])
            plan = plan_next_run(db_path, machine, full_every)
            object_key = object_key_for_machine(destination, machine, plan, now)
            run_id = start_file_run(
                db_path, machine, config["destination_id"], plan,
                TRIGGER_MANUAL if manual else TRIGGER_SCHEDULE,
                (stored["run_requested_by"] or "operator") if manual else "scheduler",
                now, object_key=object_key)
            upload = mint_upload(db_path, log_dir, config["destination_id"], object_key,
                                 hub_url=hub_url, run_id=run_id)
            params = build_file_command_params(
                machine=machine, run_id=run_id, plan=plan, config=config,
                destination=destination, machine_key=machine_key,
                object_key=object_key, upload=upload, limits=limits)
            command_id = fleet.create_command(
                db_path, machine=machine, command_type=COMMAND_BACKUP_FILES,
                params=params, issued_by="scheduler", ttl_seconds=ttl_seconds)
            attach_command(db_path, run_id, command_id)
            # Cleared only now that the request has become a real, recorded command. If
            # anything above raised, the flag survives and the next pass tries again --
            # which is what an operator who pressed the button expects.
            if manual:
                clear_file_run_request(db_path, machine)
            in_flight += 1
            dispatched += 1
        except Exception as e:
            print(f"[backup] Could not schedule a file backup for {machine}: {e}")
    return dispatched


def object_key_for_machine(destination, machine, plan, now):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    kind = "full" if plan["full"] else "inc"
    name = f"{stamp}-{plan['chain_id'][:12]}-{plan['sequence']:03d}-{kind}{FILE_EXTENSION}"
    return object_key(destination["config"].get("prefix"), BACKUP_MACHINE_FILES, name,
                      machine=machine)


def files_tick(db_path, log_dir, *, fleet_enabled, fleet_destination, fleet_include,
               fleet_exclude, interval_hours, full_every, keep_chains, limits, machines,
               now=None, hub_url="",
               max_concurrent=DEFAULT_MAX_CONCURRENT_FILE_RUNS,
               ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS):
    """One per-PC scheduler pass: retire abandoned runs, then dispatch due ones.

    Retire FIRST, for the same reason packages.tick reconciles first: a machine whose
    last run is stuck `running` is never due (due-ness anchors on the last attempt), so
    it would never be retried until something expired it. Abandoned RESTORES are retired
    on the same pass -- they have no schedule of their own, and a restore that hangs
    forever is exactly as misleading as a backup that does.

    Retiring first also releases throttle capacity: `running` rows are what
    files_dispatch_once counts against max_concurrent, so a machine that died mid-backup
    must stop occupying a slot before the pass decides who else may start.
    """
    expired = expire_stale_file_runs(db_path, now=now)
    expired += expire_stale_restores(db_path, now=now)
    dispatched = files_dispatch_once(
        db_path, log_dir, fleet_enabled=fleet_enabled,
        fleet_destination=fleet_destination, fleet_include=fleet_include,
        fleet_exclude=fleet_exclude, interval_hours=interval_hours,
        full_every=full_every, limits=limits, machines=machines, now=now,
        hub_url=hub_url, max_concurrent=max_concurrent, ttl_seconds=ttl_seconds)
    return expired, dispatched


def ingest_file_result(db_path, log_dir, run_id, result, *, keep_chains=4):
    """Record what an agent reported for one `backup_files` command.

    Called from the agent-facing endpoint. Everything in `result` is agent-supplied, so
    the manifest is size-capped and every path normalised; the object key is NOT taken
    from the agent -- it is the one the hub minted the upload for, so a compromised agent
    cannot make the hub record its archive under another machine's key.

    Rotation runs here rather than on the scheduler tick: it needs the chain that was
    just added, and doing it at dispatch would delete an old chain before the new one
    landed -- briefly leaving fewer generations than the operator asked for.
    """
    run = get_run(db_path, run_id)
    if run is None:
        raise ValueError("unknown backup run")
    if run["status"] != RUN_RUNNING:
        return run          # already reported; a retry of a POST that landed

    error = (result or {}).get("error")
    if error:
        return complete_file_run(db_path, run_id, error=str(error))

    files = (result or {}).get("files") or []
    stored_bytes = int((result or {}).get("stored_bytes") or 0)
    try:
        record_file_set(db_path, run_id=run_id, machine=run["machine"],
                        chain_id=run["chain_id"], sequence=run["sequence"],
                        object_key=run["object_key"],
                        stored_bytes=stored_bytes, files=files)
    except ValueError as e:
        return complete_file_run(db_path, run_id, error=str(e))

    finished = complete_file_run(db_path, run_id, stored_bytes=stored_bytes,
                                 file_count=len(files))
    try:
        client, record = open_client(db_path, log_dir, run["destination_id"])
        rotate_chains(client, record["config"].get("prefix"), run["machine"],
                      keep_chains, db_path)
    except (BackupError, ValueError) as e:
        # A rotation failure must not turn a successful backup red -- the archive IS
        # uploaded and IS restorable. Logged, and the next run tries again.
        print(f"[backup] Rotation for {run['machine']} failed: {e}")
    fleet.audit(db_path, actor="agent", action="backup_files", target=run["machine"],
                detail={"run_id": run_id, "files": len(files),
                        "stored_bytes": stored_bytes, "chain": run["chain_id"],
                        "sequence": run["sequence"]})
    return finished


# ================================
# RESTORE
# ================================
# Getting the data back, which is the only reason any of the above exists.
#
# Three ideas shape this half, and each is the opposite of what the backup path does:
#
#   * **The plan lives in the database, not in the command.** A `backup_files` command
#     carries its whole policy in `params`, which is right for a few dozen patterns. A
#     restore names FILES -- tens of thousands of them -- and fleet.create_command audits
#     its params verbatim, so the same shape would write a multi-megabyte audit row (into
#     the very database that then gets backed up) for every restore. The command carries
#     only a restore id; the agent fetches the plan from an authenticated endpoint.
#
#   * **The decrypt key travels with the plan, not with the command,** for the same
#     reason: the command's params are audit-logged, and a key in the audit log is a key
#     in every subsequent hub-database backup.
#
#   * **The hub names every object, always.** The agent is told "archive 0, archive 1",
#     and download URLs are resolved from the STORED plan. An agent can no more choose
#     which archive it reads than it can choose where its backup is written -- which
#     matters more here, because a read of another machine's archive is a data breach
#     rather than a corrupted manifest.
#
# Cross-machine restore (replacing dead hardware) is the case the whole design bends
# around: the archives were sealed with the SOURCE machine's derived key, so the target
# machine is handed a key that is not its own. That is a deliberate, audited widening of
# blast radius -- and it is the entire point of "restore to a different machine".
MAX_RESTORE_FILES = 200_000
MAX_RESTORE_SELECTIONS = 500
MAX_TARGET_DIR_CHARS = 200

# How long a minted download URL is good for. Shorter than the upload TTL: a restore is
# started by a human who is watching, so the agent picks the command up within a poll
# interval, whereas a backup can be dispatched to a machine that is asleep.
DOWNLOAD_URL_TTL_SECONDS = 2 * 60 * 60

# How long a queued `restore_files` command stays valid -- much longer than the fleet
# default of 15 minutes, because the machine an operator wants to restore onto is very
# often the machine that is currently being rebuilt, and expiring the command while it
# boots would mean the restore has to be started again from the browser.
#
# Deliberately shorter than expire_stale_restores' 24 hours: the command dies first, so a
# machine that never came back leaves a restore row that is expired by the scheduler with
# a real explanation rather than one that is picked up a day later and restores files onto
# a PC nobody is expecting it on.
RESTORE_COMMAND_TTL_SECONDS = 4 * 60 * 60


def validate_target_dir(target_dir):
    """Where a restore writes. "" means "back where the files came from".

    An absolute local path or nothing -- no relative paths, because "relative to what" on
    a service running as SYSTEM is `C:\\Windows\\System32`, and no UNC, because writing a
    restore to a network share means the agent's SYSTEM account authenticating to it,
    which it generally cannot.
    """
    text = backup_paths.normalize(target_dir)
    if not text:
        return ""
    if len(text) > MAX_TARGET_DIR_CHARS:
        raise ValueError(f"The restore folder is limited to {MAX_TARGET_DIR_CHARS} "
                         f"characters.")
    if not re.match(r"^[A-Za-z]:\\", text):
        raise ValueError("The restore folder must be an absolute local path, like "
                         "C:\\Restored.")
    if ".." in text.split("\\"):
        raise ValueError("The restore folder may not contain '..'.")
    return text


def _ancestors(path_lower):
    """`c:\\a\\b\\c.txt` -> `c:\\a\\b`, `c:\\a`, `c:`. Used to test folder selection."""
    parts = path_lower.split("\\")
    for cut in range(len(parts) - 1, 0, -1):
        yield "\\".join(parts[:cut])


def plan_restore(db_path, machine, paths, *, max_files=MAX_RESTORE_FILES):
    """Work out which archives hold the selected files, and what to pull from each.

    `paths` are what the operator ticked: files, folders, or both -- a folder means
    everything under it, resolved HERE rather than by the agent, because the agent has no
    manifest and the folder may no longer exist on disk (which is usually why someone is
    restoring it).

    Matching walks each manifest row's ANCESTORS against the selection set rather than
    testing every selection against every row. With a 400k-row manifest and a few hundred
    selections the second shape is tens of billions of comparisons; this one is a handful
    of set lookups per row, because a Windows path is not deep.

    Returns {archives, file_count, total_bytes, missing}. `missing` names selections that
    matched nothing -- a restore that silently drops a folder the operator asked for is
    the failure mode this feature cannot have.
    """
    selections = []
    for raw in (paths or [])[:MAX_RESTORE_SELECTIONS]:
        # rstrip on top of normalize, which deliberately KEEPS the separator on a bare
        # drive ("C:\" is a root, "C:" is a drive-relative path). Selections are matched
        # against the ancestors of a path, and the outermost ancestor of
        # `C:\Users\bob\a.txt` is `C:` -- so a selection of `C:\` would otherwise match
        # nothing at all, silently, which for "restore this whole drive" is the worst
        # possible way to be wrong.
        clean = backup_paths.normalize(raw).rstrip("\\")
        if clean and clean not in selections:
            selections.append(clean)
    if not selections:
        raise ValueError("Choose at least one file or folder to restore.")

    wanted = {s.lower() for s in selections}
    matched = set()
    chosen = []
    for row in _latest_versions(db_path, machine):
        lowered = row["path"].lower()
        hit = lowered if lowered in wanted else next(
            (a for a in _ancestors(lowered) if a in wanted), None)
        if hit is None:
            continue
        matched.add(hit)
        chosen.append(row)
        if len(chosen) > max_files:
            raise ValueError(
                f"That selection covers more than {max_files:,} files. Restore a folder "
                f"at a time, or narrow it.")

    if not chosen:
        raise ValueError("Nothing in this machine's backups matches that selection.")

    # Grouped by archive, oldest first: the agent opens each archive exactly once, and a
    # progress line that walks forward in time reads the way an operator expects.
    archives = {}
    for row in sorted(chosen, key=lambda r: (r["created_at"], r["sequence"])):
        archive = archives.setdefault(row["object_key"], {
            "object_key": row["object_key"],
            "chain_id": row["chain_id"],
            "sequence": row["sequence"],
            "created_at": row["created_at"],
            "files": [],
        })
        archive["files"].append({
            "path": row["path"],
            "member": backup_paths.archive_member(row["path"]),
            "size": int(row["size"] or 0),
            "sha256": row["sha256"] or "",
        })
    ordered = list(archives.values())
    for index, archive in enumerate(ordered):
        archive["index"] = index

    return {
        "archives": ordered,
        "file_count": len(chosen),
        "total_bytes": sum(int(r["size"] or 0) for r in chosen),
        "missing": [s for s in selections if s.lower() not in matched],
    }


def _restore_row(row):
    record = dict(row)
    record["plan"] = json.loads(record.pop("plan_json") or "{}")
    record["overwrite"] = bool(record["overwrite"])
    record["archive_count"] = len(record["plan"].get("archives") or [])
    return record


def get_restore(db_path, restore_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM backup_restores WHERE id = ?",
                           (restore_id,)).fetchone()
    return _restore_row(row) if row else None


def list_restores(db_path, machine=None, limit=20):
    """Restore history. `machine` matches EITHER end of a cross-machine restore, so a
    machine's page shows both "restored onto this box" and "this box's data was pulled
    back onto another" -- both are things an operator looking at PC-3 needs to know."""
    query = "SELECT * FROM backup_restores"
    params = []
    if machine:
        query += " WHERE machine = ? OR source_machine = ?"
        params += [machine, machine]
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    # The plan is megabytes and the list view shows counts, so it is dropped here rather
    # than shipped to a browser that would only measure its length.
    out = []
    for row in rows:
        record = _restore_row(row)
        record.pop("plan", None)
        out.append(record)
    return out


def create_restore(db_path, *, machine, source_machine, destination_id, plan,
                   target_dir="", overwrite=False, actor="system", now=None):
    """Open a restore row. Written BEFORE the command is queued, like every other
    dispatch here -- a crash between the two costs one visible failed restore rather than
    an agent writing files nobody recorded asking for."""
    now = int(time.time() if now is None else now)
    restore_id = uuid.uuid4().hex
    target_dir = validate_target_dir(target_dir)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO backup_restores(id, machine, source_machine, destination_id, "
            "target_dir, overwrite, status, plan_json, file_count, actor, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (restore_id, machine, source_machine, destination_id,
             target_dir, 1 if overwrite else 0, RUN_RUNNING,
             json.dumps(plan), int(plan.get("file_count") or 0), actor, now),
        )
    # Audited with the SHAPE of the restore, never the file list: the point of the record
    # is "who pulled whose data onto which machine", and a 40,000-path audit row would
    # bury exactly that.
    fleet.audit(db_path, actor=actor, action="backup_restore_start", target=machine,
                detail={"restore_id": restore_id, "source_machine": source_machine,
                        "files": plan.get("file_count"),
                        "archives": len(plan.get("archives") or []),
                        "target_dir": target_dir or "(original locations)",
                        "overwrite": bool(overwrite)})
    return restore_id


def attach_restore_command(db_path, restore_id, command_id):
    with get_conn(db_path) as conn:
        conn.execute("UPDATE backup_restores SET command_id = ? WHERE id = ?",
                     (command_id, restore_id))


def build_restore_command_params(*, restore_id, source_machine, plan):
    """The `restore_files` command params: an id and the size of the job, nothing more.

    Deliberately tiny. The file list and the decryption key are fetched by the agent from
    the plan endpoint -- see the section comment for why neither belongs in something
    fleet.create_command writes verbatim into the audit log.

    The counts ARE here so the agent can refuse an obviously wrong job (and so an operator
    reading the command list sees the size of what they started) without a second request.
    """
    return {
        "restore_id": restore_id,
        "source_machine": source_machine,
        "file_count": int(plan.get("file_count") or 0),
        "total_bytes": int(plan.get("total_bytes") or 0),
        "archive_count": len(plan.get("archives") or []),
    }


def mint_download(db_path, log_dir, destination_id, object_key, *, hub_url="",
                  restore_id="", index=0):
    """Where the agent should GET one archive from -- the mirror of mint_upload.

    S3 gets a pre-signed GET scoped to this exact object; WebDAV, which has no such
    concept, is proxied by the hub. Same split, same reason: the shared credential never
    reaches a machine.
    """
    record = get_destination(db_path, destination_id)
    if record is None:
        raise ValueError("That backup destination no longer exists.")
    if record["kind"] == KIND_S3:
        master_key = load_master_key()
        if master_key is None:
            raise ValueError("No backup master key is configured on this hub.")
        client = build_client(record, load_secret(log_dir, master_key, destination_id))
        return {"kind": "s3",
                "url": client.presigned_url(object_key, method="GET",
                                            expires_seconds=DOWNLOAD_URL_TTL_SECONDS),
                "expires_in": DOWNLOAD_URL_TTL_SECONDS}
    return {"kind": "hub",
            "url": f"{hub_url.rstrip('/')}/api/agent/backups/restore/{restore_id}"
                   f"/archive/{int(index)}",
            "expires_in": DOWNLOAD_URL_TTL_SECONDS}


def restore_plan_payload(db_path, log_dir, restore_id, *, hub_url=""):
    """The full plan, with a download URL per archive and the decryption key.

    Built fresh on every fetch rather than stored: pre-signed URLs expire, and a command
    that sat in the queue while a laptop was shut for the weekend would otherwise wake up
    holding a set of dead links. The plan itself -- which archives, which members -- comes
    from the row and never from the request.
    """
    restore = get_restore(db_path, restore_id)
    if restore is None:
        raise ValueError("unknown restore")
    machine_key = machine_key_for(restore["source_machine"])
    if machine_key is None:
        raise ValueError("No backup master key is configured on this hub.")

    archives = []
    for archive in restore["plan"].get("archives") or []:
        archives.append({
            "index": archive["index"],
            "object_key": archive["object_key"],
            "download": mint_download(db_path, log_dir, restore["destination_id"],
                                      archive["object_key"], hub_url=hub_url,
                                      restore_id=restore_id, index=archive["index"]),
            "files": archive["files"],
        })
    return {
        "restore_id": restore_id,
        "source_machine": restore["source_machine"],
        "machine": restore["machine"],
        "target_dir": restore["target_dir"] or "",
        "overwrite": bool(restore["overwrite"]),
        "file_count": restore["file_count"],
        "total_bytes": int(restore["plan"].get("total_bytes") or 0),
        "encryption": {
            "algorithm": "AES-256-GCM",
            "key": base64.b64encode(machine_key).decode("ascii"),
            "key_id": key_id(machine_key),
        },
        "archives": archives,
    }


def restore_archive_key(db_path, restore_id, index):
    """The object key for one archive of a restore, or None.

    The WebDAV proxy's whole safety property: the agent asks for "archive 2 of restore X"
    and the hub decides what that means. An agent-supplied object key would let any
    enrolled machine read any archive in the bucket.
    """
    restore = get_restore(db_path, restore_id)
    if restore is None:
        return None
    for archive in restore["plan"].get("archives") or []:
        if int(archive["index"]) == int(index):
            return archive["object_key"]
    return None


def complete_restore(db_path, restore_id, *, restored=None, bytes_restored=None,
                     error=None, actor="agent"):
    """Close a restore row. `error` set means failed, absent means succeeded."""
    status = RUN_FAILED if error else RUN_SUCCEEDED
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE backup_restores SET status = ?, restored_count = ?, "
            "bytes_restored = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, None if restored is None else int(restored),
             None if bytes_restored is None else int(bytes_restored),
             str(error)[:MAX_ERROR_CHARS] if error else None,
             int(time.time()), restore_id),
        )
    restore = get_restore(db_path, restore_id)
    if restore is not None:
        fleet.audit(db_path, actor=actor,
                    action="backup_restore_failed" if error else "backup_restore",
                    target=restore["machine"],
                    detail={"restore_id": restore_id,
                            "source_machine": restore["source_machine"],
                            "restored": restored, "requested": restore["file_count"],
                            "error": str(error)[:MAX_ERROR_CHARS] if error else None})
    return restore


def ingest_restore_result(db_path, restore_id, result):
    """Record what an agent reported for one `restore_files` command.

    A restore that wrote SOME files is reported as a failure carrying the count, not as a
    success: "restored 900 of 1000 files" needs a human to look at which 100, and a green
    row would mean nobody ever does.
    """
    restore = get_restore(db_path, restore_id)
    if restore is None:
        raise ValueError("unknown restore")
    if restore["status"] != RUN_RUNNING:
        return restore          # already reported; a retry of a POST that landed

    result = result or {}
    restored = int(result.get("restored") or 0)
    bytes_restored = int(result.get("bytes_restored") or 0)
    error = result.get("error")
    if not error and restored < restore["file_count"]:
        failures = [str(f)[:200] for f in (result.get("failures") or [])][:5]
        error = (f"Restored {restored:,} of {restore['file_count']:,} files."
                 + (" First problems: " + "; ".join(failures) if failures else ""))
    return complete_restore(db_path, restore_id, restored=restored,
                            bytes_restored=bytes_restored, error=error)


def expire_stale_restores(db_path, now=None, max_seconds=24 * 60 * 60):
    """Fail restores whose agent never reported back -- the mirror of
    expire_stale_file_runs, and needed for the same reason: a restore stuck `running`
    forever tells an operator neither that it worked nor that it did not."""
    now = int(time.time() if now is None else now)
    cutoff = now - int(max_seconds)
    with get_conn(db_path) as conn:
        stale = [r["id"] for r in conn.execute(
            "SELECT id FROM backup_restores WHERE status = ? AND started_at < ?",
            (RUN_RUNNING, cutoff))]
    for restore_id in stale:
        complete_restore(db_path, restore_id, actor="scheduler",
                         error="The machine never reported a result for this restore.")
    return len(stale)


def tick(db_path, log_dir, *, enabled, destination_id, interval_hours, keep,
         now=None, hub_version=""):
    """One scheduler pass. Returns the run row if a backup ran, else None.

    Every knob is a parameter rather than a settings.get_int() call, so the whole
    scheduler is testable by passing a clock -- the same contract packages.tick() uses.
    """
    now = int(time.time() if now is None else now)
    if not enabled or not destination_id:
        return None
    if now < next_due_at(db_path, interval_hours):
        return None
    run = backup_hub_database(db_path, log_dir, destination_id, keep=keep,
                              trigger=TRIGGER_SCHEDULE, actor="scheduler", now=now,
                              hub_version=hub_version)
    if run is not None:
        # Trimmed here rather than on every run so a burst of manual tests doesn't spend
        # a DELETE each time. Once a day against a table gaining a row a day is plenty.
        prune_runs(db_path)
    return run
