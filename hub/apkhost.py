"""The Android agent APK this hub hosts for provisioning -- roadmap #23 phase A.

**The hub serves the file a factory-reset device installs as its device owner.** Until this
existed, an operator hosted the APK somewhere else, ran `apksigner verify --print-certs` against
it, converted the hex digest by hand and typed both into settings. Three manual steps in front
of the one action whose mistakes are only discovered after a wipe. Now: upload the signed APK,
and the hub stores it, derives the checksum from its own signing block (see
provisioning.signing_certificate_checksum) and puts both into the QR.

**One row, enforced by the schema.** `CHECK (id = 1)` is a design statement rather than a
constraint that happened to fit: a fleet has one agent APK for the same reason it has one
signing key, and settings.py already says a fleet with two of them is a fleet with two
identities. Uploading replaces. There is no version history and no rollback list, because
either would let two APKs be hosted at once and the only thing that could then decide between
them is whichever row a query happened to sort first.

**The download URL carries a random token, and the token is what makes it revocable.** A
content-addressed path would have been simpler and is not a secret -- the digest is a function
of a binary that is handed to devices, so anyone holding the APK can compute the URL. Worse, it
cannot be rotated: re-uploading identical bytes reproduces the same path. A token means a code
photographed off a bench can be killed by uploading again or removing.

**It is anti-enumeration and a kill switch, not a security boundary**, and nothing in this
product should imply otherwise. The file it protects is a signed release binary carrying no
fleet data, no hub address and no credentials -- the enrollment secret rides in the QR's admin
extras, never in the APK. See SECURITY.MD.

**Two silent failures shape the rest of this file.** The first is a checksum derived from the
wrong certificate, which is 43 valid characters, a code that scans, and a device that fails
provisioning after a factory reset -- so the checksum is derived BEFORE the row is written and
an APK whose certificate cannot be read is never recorded at all. The second is a row that
outlives its blob, which happens when a database is restored from backup and the blob directory
is not: the console would say an APK is hosted and the device would get a 404 after being
wiped. The download route answers that case with a 410 and a reason rather than a 404.

Kept free of Flask so it can be unit-tested in isolation.
"""
import hmac
import os
import re
import secrets
import sqlite3
import time

import packages
import provisioning

#: The token is `secrets.token_urlsafe(32)`, which is 43 characters of the same URL-safe
#: alphabet a signature checksum uses. Checked by shape before anything touches the database,
#: so a malformed path segment costs one regex rather than a query.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

#: The name the download is served under. Fixed rather than the uploaded file's own name: the
#: setup wizard does not care, and echoing an operator-supplied filename into a
#: Content-Disposition header is a header-injection surface for no benefit.
DOWNLOAD_NAME = "fleethub-agent.apk"

#: What the download route answers with. The platform ignores it, but a browser used to test the
#: URL by hand should offer to save the file rather than render it.
CONTENT_TYPE = "application/vnd.android.package-archive"

MAX_FILE_NAME_CHARS = 200


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_apkhost_db(db_path):
    """Idempotent, like every other init_*_db."""
    with get_conn(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provisioning_apk (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                sha256        TEXT NOT NULL,
                size_bytes    INTEGER NOT NULL,
                file_name     TEXT NOT NULL DEFAULT '',
                checksum      TEXT NOT NULL,
                token         TEXT NOT NULL,
                uploaded_by   TEXT NOT NULL DEFAULT '',
                uploaded_at   INTEGER NOT NULL
            )
        """)


# ================================
# THE BLOB
# ================================
def blob_root(log_dir):
    """Where the hosted APK lives: a `provisioning` directory beside the database.

    Beside the DB rather than in the source tree for the reason packages.blob_root gives -- the
    hub's own updater replaces `hub/` wholesale, and `hub/static/` with it.

    **Its own directory rather than sharing packages'**, which would otherwise be free. The
    reason is packages.delete_blob_if_orphaned: it decides whether a blob is unreferenced by
    looking only at `package_sources`, so an APK sitting in that tree is a file no packages row
    points at. Nothing sweeps today; the day something does, sharing the tree would make it a
    fleet-wide deletion of the file every new device downloads.
    """
    return os.path.join(log_dir, "provisioning")


def blob_path(root, sha256):
    """Content-addressed, with the same two-hex shard packages uses. One file lives here today,
    but the sharding costs nothing and keeps the two stores readable side by side."""
    return packages.blob_path(root, sha256)


def new_token():
    """A fresh download token. Minted on every upload and on every removal, so a previously
    printed QR stops resolving the moment the hosted APK changes."""
    return secrets.token_urlsafe(32)


def _clean(value, limit):
    return str(value if value is not None else "").strip()[:limit]


# ================================
# WRITING
# ================================
def store_upload(db_path, root, stream, max_bytes, *, file_name="", actor="", now=None):
    """Store an uploaded APK and make it the hosted one. Returns the new record.

    **The checksum is derived before anything is recorded**, and the blob is removed again if it
    cannot be. An APK whose signing certificate this hub cannot read is not one it can hand to a
    device, and the operator is standing at the console right now -- which is the last moment
    the mistake is cheap.

    Replaces rather than appends: the previous blob is unlinked when the digest differs, and a
    new token is minted either way.
    """
    sha256, size = packages.store_blob(root, stream, max_bytes)
    path = blob_path(root, sha256)

    try:
        with open(path, "rb") as handle:
            checksum = provisioning.signing_certificate_checksum(handle.read())
    except Exception:
        # Including provisioning.ProvisioningIncomplete, which the route renders as the reason.
        # The blob goes with it: a file nothing references is one that would sit in the state
        # directory forever with nothing to say what it was.
        _unlink(path)
        raise

    previous = get_hosted(db_path)
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO provisioning_apk(id, sha256, size_bytes, file_name, checksum, token, "
            "                             uploaded_by, uploaded_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "sha256 = excluded.sha256, size_bytes = excluded.size_bytes, "
            "file_name = excluded.file_name, checksum = excluded.checksum, "
            "token = excluded.token, uploaded_by = excluded.uploaded_by, "
            "uploaded_at = excluded.uploaded_at",
            (sha256, size, _clean(file_name, MAX_FILE_NAME_CHARS), checksum, new_token(),
             _clean(actor, 200), now))

    if previous and previous["sha256"] != sha256:
        _unlink(blob_path(root, previous["sha256"]))
    return get_hosted(db_path)


def remove_hosted(db_path, root):
    """Stop hosting. Returns True if there was something to stop.

    This is the off switch, and it is the only one. A `serve_apk` toggle beside it would create
    a state where a record exists, the console says an APK is hosted, and the QR points at a
    route that answers nothing.
    """
    record = get_hosted(db_path)
    if record is None:
        return False
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM provisioning_apk WHERE id = 1")
    _unlink(blob_path(root, record["sha256"]))
    return True


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        # A blob that is already gone, or one the process cannot remove. Neither is worth
        # failing a request that has otherwise succeeded -- the row is what decides what is
        # served, and it is already correct.
        pass


# ================================
# READING
# ================================
def get_hosted(db_path):
    """The hosted APK's record, or None. The token is included: the only caller is the web
    layer, which needs it to build the download URL."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM provisioning_apk WHERE id = 1").fetchone()
    return dict(row) if row is not None else None


def hosted_by_token(db_path, token):
    """The record this token addresses, or None.

    **The token is looked UP, never joined to a path.** That is what keeps the download route
    from becoming a read primitive over the state directory: the only file it can ever serve is
    the one digest recorded here, and a caller supplies a token rather than a location.

    Compared with `hmac.compare_digest` for the usual reason, and checked by shape first so a
    malformed segment never reaches the database.
    """
    text = str(token or "").strip()
    if not _TOKEN_RE.match(text):
        return None
    record = get_hosted(db_path)
    if record is None:
        return None
    return record if hmac.compare_digest(record["token"], text) else None


def download_path(token):
    """The path the QR carries. One stable prefix, deliberately: a reverse proxy that wants to
    rate-limit or block the only unauthenticated route in this hub should be able to do it in
    one rule."""
    return f"/provisioning/apk/{token}/{DOWNLOAD_NAME}"


def download_url(hub_url, token):
    """The absolute URL for the QR. Empty when the hub has no public address -- build_payload
    turns that into the refusal naming HUB_URL, rather than producing a relative URL a
    factory-reset device could do nothing with."""
    base = str(hub_url or "").strip().rstrip("/")
    return f"{base}{download_path(token)}" if base else ""
