"""Package deployment -- define an installer once, push it to many machines.

Roadmap #5. The shape of the problem is PDQ's: an operator should describe a piece of
software ONCE (what the payload is, how to run it silently, what counts as success) and
then aim it at machines, rather than hand-typing an `install_app` command per box and
eyeballing the terminal to see whether it worked.

Four ideas carry the design, and each exists because the naive version is wrong:

  * **Recipe and payload are separate tables.** `packages` is the recipe (command line,
    timeout, success criteria); `package_sources` is the payload (an uploaded blob, or a
    reference to winget/URL/UNC). Splitting them means re-uploading a newer installer
    doesn't disturb the recipe or orphan its deploy history, and it is the seam where
    per-architecture payloads land later without a migration. v1 keeps one source row per
    package (UNIQUE on package_id) -- the table shape, not the row count, is the point.

  * **Uploaded payloads are content-addressed by sha256 and refcounted.** The blob lives
    at `<blob root>/<first two hex>/<sha256>`, so two packages built from the same
    installer share one file, and a blob is only unlinked once no source row references
    it. The hash is computed by the hub AT UPLOAD, never accepted from a client, and the
    agent re-verifies it before executing. That is the whole trust story for hub-hosted
    payloads: the authenticated HTTPS channel plus a hash the hub itself computed. There
    is deliberately no new offline signing key here -- see fleet.py's docstring for why
    that model was removed, and note that the agent's own self-update trust root is
    SEPARATE and still fully signed.

  * **Success = exit code AND detection.** An installer exiting 0 is evidence, not proof:
    silent installers routinely return 0 after doing nothing. So a package carries both a
    `success_exit_codes` set (0 and 3010 -- "reboot required" -- by default) and a
    post-install detection rule the agent evaluates afterward. The rule grammar is
    deliberately three kinds and a `none` escape hatch, not a DSL: every kind added here
    is a kind the agent must implement, and an expression language would put arbitrary
    evaluation back on the endpoint.

  * **Scheduling layers on the existing command queue, it does not replace it.** A
    deployment is a set of per-machine target rows; the scheduler tick turns an eligible
    target into an ordinary `deploy_package` command with the usual TTL, then reads that
    command's terminal status back. An offline machine therefore costs one expired
    command and one backoff, using the exact same expiry the queue already enforces --
    rather than a second, parallel notion of delivery that could disagree with it.

Authorization lives entirely upstream, at the `deploy_packages` capability plus machine
scope (see packages_web.py). Nothing here checks a session, exactly like fleet.py.

Kept free of Flask so it can be unit-tested in isolation; packages_web.py wires thin
HTTP endpoints on top.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid

import fleet

# ================================
# VOCABULARY
# ================================
# Where a payload comes from. `upload` is the only kind the hub stores bytes for; the
# other three are references the agent resolves itself at install time.
SOURCE_UPLOAD = "upload"    # a file uploaded to the hub, addressed by sha256
SOURCE_WINGET = "winget"    # a winget package id
SOURCE_URL = "url"          # an http(s) URL the agent downloads
SOURCE_UNC = "unc"          # a \\server\share path the agent reads
SOURCE_KINDS = (SOURCE_UPLOAD, SOURCE_WINGET, SOURCE_URL, SOURCE_UNC)

# Kinds of payload that produce a local FILE the command line has to point at. winget is
# the odd one out: it resolves and runs its own payload, so there is nothing to substitute.
FILE_SOURCE_KINDS = frozenset({SOURCE_UPLOAD, SOURCE_URL, SOURCE_UNC})

# The placeholder the agent replaces with the resolved local payload path. It must appear
# somewhere in the command line of a file-backed package -- a package that downloads an
# installer and then never references it is always a mistake, never an intent, so
# validation refuses it rather than shipping a deploy that silently no-ops.
FILE_PLACEHOLDER = "{file}"

# Post-install detection. Three kinds plus an explicit opt-out, held to that deliberately:
# each one is code the C# agent must implement and keep working across Windows versions.
DETECT_NONE = "none"
DETECT_FILE_EXISTS = "file_exists"
DETECT_REGISTRY_VALUE = "registry_value"
DETECT_INSTALLED_VERSION = "installed_version"
DETECTION_KINDS = (DETECT_NONE, DETECT_FILE_EXISTS, DETECT_REGISTRY_VALUE,
                   DETECT_INSTALLED_VERSION)

# Shown in the package form. Kept here rather than in the template for the same reason
# permissions.CAPABILITY_LABELS is: the API describes itself, so adding a kind is one edit.
DETECTION_LABELS = {
    DETECT_NONE: ("No detection check",
                  "Trust the exit code alone. Only sensible for installers you know "
                  "report failure honestly."),
    DETECT_FILE_EXISTS: ("File exists",
                         "Succeed only if a path is present after the install, e.g. "
                         "C:\\Program Files\\7-Zip\\7z.exe."),
    DETECT_REGISTRY_VALUE: ("Registry value",
                            "Succeed only if a registry value exists -- optionally "
                            "matching an exact string."),
    DETECT_INSTALLED_VERSION: ("Installed version",
                               "Look the product up in Windows' installed-programs "
                               "registry and require at least a given version."),
}

REGISTRY_ROOTS = ("HKLM", "HKCU", "HKCR", "HKU")

# Deployment lifecycle. `scheduled` means nothing has been attempted yet (a future
# window, or a tick away); `running` means at least one target has been attempted and at
# least one is still unresolved; `complete` means every target reached a terminal state.
DEPLOY_SCHEDULED = "scheduled"
DEPLOY_RUNNING = "running"
DEPLOY_COMPLETE = "complete"
DEPLOY_CANCELLED = "cancelled"
DEPLOY_STATUSES = (DEPLOY_SCHEDULED, DEPLOY_RUNNING, DEPLOY_COMPLETE, DEPLOY_CANCELLED)

# Per-machine target lifecycle. `failed` is "attempts exhausted", not "one attempt
# failed" -- a target with retries left goes back to `pending` with a backoff, which is
# what keeps the retry policy visible in the row rather than hidden in the scheduler.
TARGET_PENDING = "pending"
TARGET_IN_FLIGHT = "in_flight"
TARGET_SUCCEEDED = "succeeded"
TARGET_FAILED = "failed"
TARGET_EXPIRED = "expired"      # the deploy window closed before this one ran
TARGET_CANCELLED = "cancelled"
TARGET_STATUSES = (TARGET_PENDING, TARGET_IN_FLIGHT, TARGET_SUCCEEDED, TARGET_FAILED,
                   TARGET_EXPIRED, TARGET_CANCELLED)
TARGET_TERMINAL = frozenset({TARGET_SUCCEEDED, TARGET_FAILED, TARGET_EXPIRED,
                             TARGET_CANCELLED})

# 3010 is Windows' "success, but a reboot is required" -- treating it as failure would
# mark half a fleet's MSI installs red. 0 and 3010 is the standard PDQ-style default.
DEFAULT_SUCCESS_EXIT_CODES = (0, 3010)

MAX_NAME_CHARS = 120
MAX_COMMAND_CHARS = 2000
# How much of a failing command's output to keep on the target row. The full text stays
# in command_results; this is the at-a-glance reason shown next to the machine.
MAX_ERROR_CHARS = 2000

# The command type the scheduler queues. Registered in fleet.ALL_COMMANDS so
# create_command accepts it and the agent's dispatcher can route it.
COMMAND_TYPE = "deploy_package"


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_packages_db(db_path):
    """Create the package/deployment tables if absent. Idempotent -- safe to call next
    to the other init_*_db() functions on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # The RECIPE. Deliberately free of any payload detail -- see the module docstring.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS packages (
                id                  TEXT PRIMARY KEY,
                name                TEXT NOT NULL,
                description         TEXT,
                version             TEXT,
                install_command     TEXT NOT NULL,
                install_args        TEXT,
                timeout_seconds     INTEGER NOT NULL,
                success_exit_codes  TEXT NOT NULL,   -- JSON array of ints
                detection_json      TEXT NOT NULL,   -- JSON object, always has "kind"
                created_at          INTEGER NOT NULL,
                updated_at          INTEGER NOT NULL,
                created_by          TEXT,
                updated_by          TEXT
            )
            """
        )
        # Case-insensitive, like permission group names: two packages differing only in
        # case is a configuration accident every time.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_packages_name "
            "ON packages(name COLLATE NOCASE)"
        )
        # The PAYLOAD. `sha256` is set for uploads (the content address of the stored
        # blob) and MAY be set for url/unc, where it is an integrity pin the agent
        # enforces after fetching. NULL for winget, which has its own trust chain.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS package_sources (
                id          TEXT PRIMARY KEY,
                package_id  TEXT NOT NULL,
                kind        TEXT NOT NULL,   -- SOURCE_KINDS
                ref         TEXT,            -- winget id / URL / UNC path
                sha256      TEXT,            -- content address (upload) or pin (url/unc)
                file_name   TEXT,            -- original upload filename, for display
                file_size   INTEGER,
                created_at  INTEGER NOT NULL
            )
            """
        )
        # v1 is one source per package. The constraint is what makes that explicit rather
        # than accidental; lifting it later (per-architecture payloads) is a one-line
        # index change, not a data migration.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_package_sources_package "
            "ON package_sources(package_id)"
        )
        # Blob refcounting reads this -- see blob_is_referenced().
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_package_sources_sha ON package_sources(sha256)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deployments (
                id                    TEXT PRIMARY KEY,
                package_id            TEXT NOT NULL,
                note                  TEXT,
                status                TEXT NOT NULL,
                window_start          INTEGER,        -- NULL = start immediately
                window_end            INTEGER,        -- NULL = no deadline
                max_attempts          INTEGER NOT NULL,
                retry_backoff_seconds INTEGER NOT NULL,
                created_at            INTEGER NOT NULL,
                created_by            TEXT NOT NULL,
                updated_at            INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deployments_status ON deployments(status)"
        )
        # One row per machine per deployment: the unit the scheduler advances and the
        # unit the progress UI renders.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deployment_targets (
                deployment_id   TEXT NOT NULL,
                machine         TEXT NOT NULL,
                status          TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER,      -- earliest time the scheduler may retry
                command_id      TEXT,         -- the current/last queued command
                last_error      TEXT,
                updated_at      INTEGER NOT NULL,
                PRIMARY KEY (deployment_id, machine)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deployment_targets_machine "
            "ON deployment_targets(machine)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deployment_targets_status "
            "ON deployment_targets(deployment_id, status)"
        )


# ================================
# VALIDATION
# ================================
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _clean(value, limit=None):
    text = str(value or "").strip()
    if limit is not None and len(text) > limit:
        raise ValueError(f"value must be {limit} characters or fewer")
    return text


def normalize_sha256(value):
    """Lowercase a hex digest, or raise. Returns None for an absent value -- callers
    that require one check for that themselves, so the error names the field."""
    if value is None or str(value).strip() == "":
        return None
    digest = str(value).strip().lower()
    if not _SHA256_RE.match(digest):
        raise ValueError("sha256 must be 64 hexadecimal characters")
    return digest


def validate_exit_codes(codes):
    """Normalize a success-exit-code set to a sorted list of ints.

    Accepts a list or a comma-separated string, because the admin form posts whichever
    is easier and both mean the same thing. An empty set is refused: it would make every
    install fail regardless of what the installer did, which is never what an operator
    means -- if they truly don't care about the exit code they want a wide set, not none.
    """
    if codes is None:
        codes = list(DEFAULT_SUCCESS_EXIT_CODES)
    if isinstance(codes, str):
        codes = [part for part in re.split(r"[,\s]+", codes) if part]
    if not isinstance(codes, (list, tuple)):
        raise ValueError("success_exit_codes must be a list of integers")
    parsed = set()
    for code in codes:
        try:
            parsed.add(int(code))
        except (TypeError, ValueError):
            raise ValueError(f"success exit code {code!r} is not an integer")
    if not parsed:
        raise ValueError("at least one success exit code is required")
    return sorted(parsed)


def validate_detection(rule):
    """Normalize + validate a detection rule, returning a dict that always has "kind".

    Unknown keys are DROPPED rather than preserved. This object is handed to the agent
    and evaluated there, so letting arbitrary fields ride along would make the rule's
    effective grammar whatever the agent happens to read, not what the hub validated.
    """
    if rule is None or rule == "":
        return {"kind": DETECT_NONE}
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except (TypeError, ValueError):
            raise ValueError("detection rule must be a JSON object")
    if not isinstance(rule, dict):
        raise ValueError("detection rule must be an object")

    kind = _clean(rule.get("kind") or DETECT_NONE)
    if kind not in DETECTION_KINDS:
        raise ValueError(f"unknown detection kind: {kind!r}")

    if kind == DETECT_NONE:
        return {"kind": DETECT_NONE}

    if kind == DETECT_FILE_EXISTS:
        path = _clean(rule.get("path"), MAX_COMMAND_CHARS)
        if not path:
            raise ValueError("a 'file exists' detection rule requires a path")
        return {"kind": kind, "path": path}

    if kind == DETECT_REGISTRY_VALUE:
        root = _clean(rule.get("root")).upper()
        if root not in REGISTRY_ROOTS:
            raise ValueError(f"registry root must be one of {', '.join(REGISTRY_ROOTS)}")
        key = _clean(rule.get("key"), MAX_COMMAND_CHARS)
        name = _clean(rule.get("name"), MAX_NAME_CHARS)
        if not key or not name:
            raise ValueError("a registry detection rule requires a key and a value name")
        normalized = {"kind": kind, "root": root, "key": key, "name": name}
        # Absent `equals` means "the value merely has to exist". Distinguished from an
        # empty string, which is a legitimate value to require an exact match on.
        if rule.get("equals") is not None:
            normalized["equals"] = _clean(rule.get("equals"), MAX_COMMAND_CHARS)
        return normalized

    # DETECT_INSTALLED_VERSION
    name = _clean(rule.get("name"), MAX_NAME_CHARS)
    if not name:
        raise ValueError("an installed-version detection rule requires a product name")
    normalized = {"kind": kind, "name": name}
    min_version = _clean(rule.get("min_version"), 60)
    if min_version:
        if not re.match(r"^[0-9]+(\.[0-9]+)*$", min_version):
            raise ValueError("min_version must be dotted numbers, e.g. 24.09 or 1.2.3.4")
        normalized["min_version"] = min_version
    return normalized


def validate_source(source):
    """Normalize + validate a payload descriptor.

    `source` is {kind, ref, sha256, file_name, file_size}. For an upload the caller has
    already stored the blob and passes the hash the HUB computed -- this function never
    treats a client-supplied digest as authoritative for an upload, it just checks shape.
    """
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    kind = _clean(source.get("kind"))
    if kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source kind: {kind!r}")

    ref = _clean(source.get("ref"), MAX_COMMAND_CHARS)
    sha256 = normalize_sha256(source.get("sha256"))

    if kind == SOURCE_UPLOAD:
        if not sha256:
            raise ValueError("an uploaded package needs its stored file's sha256")
        ref = ""
    elif kind == SOURCE_WINGET:
        if not ref:
            raise ValueError("a winget package needs a package id")
        # winget resolves and verifies its own payload; a hash here would be meaningless.
        sha256 = None
    elif kind == SOURCE_URL:
        if not re.match(r"^https?://", ref, re.IGNORECASE):
            raise ValueError("a URL package needs an http:// or https:// address")
    else:  # SOURCE_UNC
        if not ref.startswith("\\\\"):
            raise ValueError(r"a UNC package needs a path starting with \\")

    size = source.get("file_size")
    return {
        "kind": kind,
        "ref": ref or None,
        "sha256": sha256,
        "file_name": _clean(source.get("file_name"), MAX_NAME_CHARS) or None,
        "file_size": int(size) if size not in (None, "") else None,
    }


def _validate_recipe(install_command, install_args, source_kind, timeout_seconds):
    """The command line and its timeout. Split out because create and update share it."""
    command = _clean(install_command, MAX_COMMAND_CHARS)
    args = _clean(install_args, MAX_COMMAND_CHARS)

    if source_kind == SOURCE_WINGET:
        # The agent builds the winget command line from the package id; a custom command
        # here would silently win over it, so refuse rather than quietly ignore. Extra
        # args ARE allowed -- they're appended to winget's own.
        if command:
            raise ValueError(
                "a winget package has no install command -- the agent runs winget itself; "
                "put any extra switches in the arguments field")
    else:
        if not command:
            raise ValueError("an install command is required")
        if FILE_PLACEHOLDER not in (command + " " + args):
            raise ValueError(
                f"the command or arguments must reference the payload with "
                f"{FILE_PLACEHOLDER} -- otherwise the downloaded file is never used")

    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be an integer")
    if not (30 <= timeout <= 24 * 3600):
        raise ValueError("timeout_seconds must be between 30 and 86400")
    return command, args, timeout


# ================================
# BLOB STORE (uploaded payloads)
# ================================
def blob_root(log_dir):
    """Where uploaded payloads live: a `packages` directory beside the database.

    Next to the DB rather than inside the source tree deliberately -- the hub's own
    updater replaces the source tree wholesale (see app.perform_hub_update), and a
    hundred megabytes of installers sitting in there would be destroyed by an update or,
    worse, committed.
    """
    return os.path.join(log_dir, "packages")


def blob_path(root, sha256):
    """Content-addressed path. The two-hex-character shard keeps any one directory from
    growing to tens of thousands of entries, which is where Windows directory
    enumeration starts to hurt."""
    digest = normalize_sha256(sha256)
    if not digest:
        raise ValueError("sha256 is required")
    return os.path.join(root, digest[:2], digest)


def store_blob(root, stream, max_bytes, chunk_size=1024 * 1024):
    """Stream an upload to disk, hashing as it goes. Returns (sha256, size).

    The hash is computed HERE, from the bytes actually written -- never taken from the
    request -- because it is the only thing the agent checks before executing the file.
    Accepting a client's digest would reduce that check to "the uploader and the
    downloader agree", which is not integrity.

    Writes to a temp file first and renames into place, so a connection dropped mid-
    upload cannot leave a truncated blob sitting at a valid content address. A blob that
    already exists is left alone: identical content, identical hash, nothing to do.
    """
    os.makedirs(root, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    tmp_path = os.path.join(root, f".upload-{uuid.uuid4().hex}.part")
    try:
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(
                        f"package file exceeds the {max_bytes // (1024 * 1024)} MB limit")
                digest.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise ValueError("package file is empty")

        sha256 = digest.hexdigest()
        final_path = blob_path(root, sha256)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        if os.path.exists(final_path):
            os.remove(tmp_path)
        else:
            os.replace(tmp_path, final_path)
        return sha256, size
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def blob_is_referenced(db_path, sha256, exclude_source_id=None):
    """Is any source row still pointing at this blob? The refcount behind cleanup."""
    digest = normalize_sha256(sha256)
    if not digest:
        return False
    sql = "SELECT 1 FROM package_sources WHERE sha256 = ?"
    params = [digest]
    if exclude_source_id:
        sql += " AND id != ?"
        params.append(str(exclude_source_id))
    with get_conn(db_path) as conn:
        return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def delete_blob_if_orphaned(db_path, root, sha256, exclude_source_id=None):
    """Unlink a stored payload once nothing references it. Returns True if removed.

    Called after a package is deleted or has its payload replaced. Failing to delete is
    not an error worth propagating -- an orphaned blob wastes disk, a raised exception
    would abort the package deletion the operator actually asked for.
    """
    digest = normalize_sha256(sha256)
    if not digest or blob_is_referenced(db_path, digest, exclude_source_id):
        return False
    try:
        os.remove(blob_path(root, digest))
        return True
    except OSError:
        return False


# ================================
# PACKAGES
# ================================
def _package_row(row, source_row=None):
    pkg = dict(row)
    pkg["success_exit_codes"] = json.loads(pkg.pop("success_exit_codes"))
    pkg["detection"] = json.loads(pkg.pop("detection_json"))
    pkg["source"] = None
    if source_row is not None:
        source = dict(source_row)
        source.pop("package_id", None)
        pkg["source"] = source
    return pkg


def list_packages(db_path):
    """Every package with its payload, newest first. Small table by nature -- a fleet has
    tens of packages, not thousands -- so there is no pagination here on purpose."""
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM packages ORDER BY name COLLATE NOCASE").fetchall()
        sources = {
            r["package_id"]: r
            for r in conn.execute("SELECT * FROM package_sources").fetchall()
        }
    return [_package_row(row, sources.get(row["id"])) for row in rows]


def get_package(db_path, package_id):
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM packages WHERE id = ?", (str(package_id),)).fetchone()
        if row is None:
            return None
        source = conn.execute(
            "SELECT * FROM package_sources WHERE package_id = ?", (str(package_id),)
        ).fetchone()
    return _package_row(row, source)


def create_package(db_path, *, name, source, install_command=None, install_args=None,
                   description=None, version=None, timeout_seconds=900,
                   success_exit_codes=None, detection=None, actor="system"):
    """Define a package. Returns its id.

    Everything is validated before anything is written, so a rejected definition never
    leaves a half-created package (or, worse, a source row pointing at a blob nobody
    will ever clean up) behind.
    """
    name = _clean(name, MAX_NAME_CHARS)
    if not name:
        raise ValueError("a package name is required")
    source = validate_source(source)
    command, args, timeout = _validate_recipe(
        install_command, install_args, source["kind"], timeout_seconds)
    codes = validate_exit_codes(success_exit_codes)
    rule = validate_detection(detection)

    package_id = uuid.uuid4().hex
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO packages(id, name, description, version, install_command, "
                "install_args, timeout_seconds, success_exit_codes, detection_json, "
                "created_at, updated_at, created_by, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (package_id, name, _clean(description, 2000) or None,
                 _clean(version, 60) or None, command, args or None, timeout,
                 json.dumps(codes), json.dumps(rule, sort_keys=True),
                 now, now, str(actor), str(actor)),
            )
            _write_source(conn, package_id, source, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"a package named {name!r} already exists")

    fleet.audit(db_path, actor=actor, action="create_package", target=name,
                detail={"package_id": package_id, "source": source["kind"]})
    return package_id


def _write_source(conn, package_id, source, now):
    """Replace a package's payload row. One row per package in v1, so this deletes
    before inserting rather than relying on an upsert -- the kind can change (an upload
    becoming a winget reference), and a partial update would leave a stale sha256."""
    conn.execute("DELETE FROM package_sources WHERE package_id = ?", (package_id,))
    conn.execute(
        "INSERT INTO package_sources(id, package_id, kind, ref, sha256, file_name, "
        "file_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, package_id, source["kind"], source["ref"], source["sha256"],
         source["file_name"], source["file_size"], now),
    )


def update_package(db_path, package_id, *, name=None, source=None, install_command=None,
                   install_args=None, description=None, version=None,
                   timeout_seconds=None, success_exit_codes=None, detection=None,
                   actor="system", blob_root_dir=None):
    """Patch a package. Every argument is optional; None means "leave alone".

    When the payload changes, the OLD blob is offered to the orphan collector -- but only
    after the new row is committed, so a crash between the two loses disk space rather
    than the file a package still points at. Pass `blob_root_dir` to enable that; without
    it the old blob is simply left on disk (which is what the unit tests do).

    Note that changing the recipe does NOT retroactively alter deployments already in
    flight: their command params were snapshotted at dispatch (see build_command_params),
    so a machine can't get half of one recipe and half of another.
    """
    existing = get_package(db_path, package_id)
    if existing is None:
        raise KeyError("unknown package")

    old_source = existing.get("source") or {}
    new_source = validate_source(source) if source is not None else None
    source_kind = (new_source or old_source).get("kind")

    # The command line and the source kind are validated together (winget takes no
    # command; a file-backed package must reference {file}), so a change to either
    # re-checks the pair rather than just the field that moved.
    command = existing["install_command"] if install_command is None else install_command
    args = existing["install_args"] if install_args is None else install_args
    timeout = existing["timeout_seconds"] if timeout_seconds is None else timeout_seconds
    command, args, timeout = _validate_recipe(command, args, source_kind, timeout)

    codes = (existing["success_exit_codes"] if success_exit_codes is None
             else validate_exit_codes(success_exit_codes))
    rule = existing["detection"] if detection is None else validate_detection(detection)

    new_name = existing["name"] if name is None else _clean(name, MAX_NAME_CHARS)
    if not new_name:
        raise ValueError("a package name is required")

    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE packages SET name = ?, description = ?, version = ?, "
                "install_command = ?, install_args = ?, timeout_seconds = ?, "
                "success_exit_codes = ?, detection_json = ?, updated_at = ?, "
                "updated_by = ? WHERE id = ?",
                (new_name,
                 existing["description"] if description is None
                 else (_clean(description, 2000) or None),
                 existing["version"] if version is None else (_clean(version, 60) or None),
                 command, args or None, timeout, json.dumps(codes),
                 json.dumps(rule, sort_keys=True), now, str(actor), str(package_id)),
            )
            if new_source is not None:
                _write_source(conn, str(package_id), new_source, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"a package named {new_name!r} already exists")

    if new_source is not None and blob_root_dir and old_source.get("sha256"):
        if old_source.get("sha256") != new_source.get("sha256"):
            delete_blob_if_orphaned(db_path, blob_root_dir, old_source["sha256"])

    fleet.audit(db_path, actor=actor, action="update_package", target=new_name,
                detail={"package_id": package_id})
    return get_package(db_path, package_id)


def delete_package(db_path, package_id, *, actor="system", blob_root_dir=None):
    """Remove a package and its payload row, and orphan-collect its blob.

    Deployment history is deliberately NOT deleted. "Who pushed what, where, and did it
    work" has to survive the package definition being tidied up -- that record is the
    reason the feature is auditable at all -- so deployments keep the package_id and the
    UI renders a name-less row rather than losing the history.
    """
    existing = get_package(db_path, package_id)
    if existing is None:
        raise KeyError("unknown package")

    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM package_sources WHERE package_id = ?", (str(package_id),))
        conn.execute("DELETE FROM packages WHERE id = ?", (str(package_id),))

    source = existing.get("source") or {}
    if blob_root_dir and source.get("sha256"):
        delete_blob_if_orphaned(db_path, blob_root_dir, source["sha256"])

    fleet.audit(db_path, actor=actor, action="delete_package", target=existing["name"],
                detail={"package_id": package_id})


def package_id_for_blob(db_path, sha256):
    """Which package (if any) owns this blob. The agent download endpoint's gate: a
    digest that no package references is a 404, so the blob store is not a general-
    purpose file host that happens to sit behind agent auth."""
    digest = normalize_sha256(sha256)
    if not digest:
        return None
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT package_id FROM package_sources WHERE sha256 = ? LIMIT 1", (digest,)
        ).fetchone()
    return row["package_id"] if row else None


# ================================
# DEPLOYMENTS
# ================================
def create_deployment(db_path, *, package_id, machines, created_by, note=None,
                      window_start=None, window_end=None, max_attempts=3,
                      retry_backoff_seconds=900):
    """Schedule a package onto a set of machines. Returns the deployment id.

    Machine scope was already enforced by the caller (packages_web checks every target
    against the operator's permission groups BEFORE calling this), exactly as
    fleet.create_command relies on its endpoint having done so.

    Nothing is dispatched here. The scheduler tick owns dispatch, so an immediate
    deployment and a windowed one travel the same code path -- one of them just has a
    window that is already open. Two mechanisms for "send it" is how the immediate case
    ends up with a bug the scheduled case doesn't.
    """
    package = get_package(db_path, package_id)
    if package is None:
        raise KeyError("unknown package")

    targets = []
    seen = set()
    for machine in machines or []:
        name = _clean(machine)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            targets.append(name)
    if not targets:
        raise ValueError("a deployment needs at least one target machine")

    try:
        max_attempts = int(max_attempts)
        retry_backoff_seconds = int(retry_backoff_seconds)
    except (TypeError, ValueError):
        raise ValueError("max_attempts and retry_backoff_seconds must be integers")
    if not (1 <= max_attempts <= 10):
        raise ValueError("max_attempts must be between 1 and 10")
    if not (60 <= retry_backoff_seconds <= 86400):
        raise ValueError("retry_backoff_seconds must be between 60 and 86400")

    window_start = _epoch_or_none(window_start, "window_start")
    window_end = _epoch_or_none(window_end, "window_end")
    if window_start and window_end and window_end <= window_start:
        raise ValueError("the deployment window must end after it starts")

    deployment_id = uuid.uuid4().hex
    now = int(time.time())
    if window_end and window_end <= now:
        raise ValueError("the deployment window has already closed")

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO deployments(id, package_id, note, status, window_start, "
            "window_end, max_attempts, retry_backoff_seconds, created_at, created_by, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (deployment_id, str(package_id), _clean(note, 500) or None, DEPLOY_SCHEDULED,
             window_start, window_end, max_attempts, retry_backoff_seconds, now,
             str(created_by), now),
        )
        conn.executemany(
            "INSERT INTO deployment_targets(deployment_id, machine, status, attempts, "
            "next_attempt_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
            [(deployment_id, machine, TARGET_PENDING, window_start or now, now)
             for machine in targets],
        )

    fleet.audit(db_path, actor=created_by, action="create_deployment",
                target=package["name"],
                detail={"deployment_id": deployment_id, "package_id": package_id,
                        "machines": targets, "window_start": window_start,
                        "window_end": window_end, "max_attempts": max_attempts})
    return deployment_id


def _epoch_or_none(value, field):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a unix timestamp")


def list_deployments(db_path, limit=100, machine=None):
    """Recent deployments with a per-status target tally, newest first.

    The tally is computed in SQL rather than by loading every target row: a fleet-wide
    deployment has one row per machine, and the list page only needs the counts.
    `machine` narrows to deployments that touch one machine, which is what the machine
    page's Packages panel asks for.
    """
    sql = (
        "SELECT d.*, p.name AS package_name, p.version AS package_version "
        "FROM deployments d LEFT JOIN packages p ON p.id = d.package_id"
    )
    params = []
    if machine:
        sql += (" WHERE EXISTS (SELECT 1 FROM deployment_targets t "
                "WHERE t.deployment_id = d.id AND t.machine = ?)")
        params.append(_clean(machine))
    sql += " ORDER BY d.created_at DESC LIMIT ?"
    params.append(int(limit))

    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        ids = [row["id"] for row in rows]
        counts = {}
        if ids:
            placeholders = ",".join("?" for _ in ids)
            for tally in conn.execute(
                f"SELECT deployment_id, status, COUNT(*) AS n FROM deployment_targets "
                f"WHERE deployment_id IN ({placeholders}) GROUP BY deployment_id, status",
                ids,
            ).fetchall():
                counts.setdefault(tally["deployment_id"], {})[tally["status"]] = tally["n"]

    deployments = []
    for row in rows:
        item = dict(row)
        by_status = counts.get(item["id"], {})
        item["target_counts"] = by_status
        item["target_total"] = sum(by_status.values())
        deployments.append(item)
    return deployments


def get_deployment(db_path, deployment_id):
    """One deployment with every target row -- the progress view."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT d.*, p.name AS package_name, p.version AS package_version "
            "FROM deployments d LEFT JOIN packages p ON p.id = d.package_id "
            "WHERE d.id = ?", (str(deployment_id),)
        ).fetchone()
        if row is None:
            return None
        targets = conn.execute(
            "SELECT * FROM deployment_targets WHERE deployment_id = ? "
            "ORDER BY machine COLLATE NOCASE", (str(deployment_id),)
        ).fetchall()
    deployment = dict(row)
    deployment["targets"] = [dict(t) for t in targets]
    counts = {}
    for target in deployment["targets"]:
        counts[target["status"]] = counts.get(target["status"], 0) + 1
    deployment["target_counts"] = counts
    deployment["target_total"] = len(deployment["targets"])
    return deployment


def cancel_deployment(db_path, deployment_id, actor="system"):
    """Stop a deployment. Targets that haven't reached a terminal state become
    `cancelled`; ones already in flight are left alone.

    In-flight targets are deliberately NOT clawed back. The command is already on its
    way to (or running on) the machine, and marking it cancelled here would produce a
    row claiming nothing happened while an installer runs -- a record that lies is worse
    than one that says "this one got out before you hit stop". The reconcile pass still
    records its real outcome.
    """
    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM deployments WHERE id = ?", (str(deployment_id),)).fetchone()
        if row is None:
            raise KeyError("unknown deployment")
        if row["status"] == DEPLOY_COMPLETE:
            raise ValueError("that deployment has already finished")
        conn.execute(
            "UPDATE deployment_targets SET status = ?, updated_at = ? "
            "WHERE deployment_id = ? AND status = ?",
            (TARGET_CANCELLED, now, str(deployment_id), TARGET_PENDING),
        )
        conn.execute(
            "UPDATE deployments SET status = ?, updated_at = ? WHERE id = ?",
            (DEPLOY_CANCELLED, now, str(deployment_id)),
        )
    fleet.audit(db_path, actor=actor, action="cancel_deployment", target=deployment_id)


def retry_deployment_failures(db_path, deployment_id, actor="system"):
    """Put every failed/expired target back in the queue with a fresh attempt budget.

    Separate from creating a new deployment because the operator's intent is different:
    "try these seven again", not "start a new push". Keeping it on the same deployment
    row is what lets the history show one deploy that eventually reached 100% rather
    than a chain of partial ones nobody can line up.
    """
    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM deployments WHERE id = ?", (str(deployment_id),)).fetchone()
        if row is None:
            raise KeyError("unknown deployment")
        cur = conn.execute(
            "UPDATE deployment_targets SET status = ?, attempts = 0, next_attempt_at = ?, "
            "last_error = NULL, updated_at = ? "
            "WHERE deployment_id = ? AND status IN (?, ?, ?)",
            (TARGET_PENDING, now, now, str(deployment_id),
             TARGET_FAILED, TARGET_EXPIRED, TARGET_CANCELLED),
        )
        requeued = cur.rowcount or 0
        if requeued:
            conn.execute(
                "UPDATE deployments SET status = ?, updated_at = ? WHERE id = ?",
                (DEPLOY_RUNNING, now, str(deployment_id)),
            )
    if requeued:
        fleet.audit(db_path, actor=actor, action="retry_deployment", target=deployment_id,
                    detail={"requeued": requeued})
    return requeued


# ================================
# COMMAND PAYLOAD
# ================================
def build_command_params(package, deployment, hub_url=""):
    """The params the agent receives for one `deploy_package` command.

    A full SNAPSHOT of the recipe, not a pointer to it. The agent could in principle be
    handed a package id and told to fetch the definition, but then editing a package
    while a deployment is in flight would silently change what half the fleet installs.
    Snapshotting means a target always runs the recipe that was current when its attempt
    was dispatched, and the audit log records exactly that.

    `download_url` is relative when no hub URL is configured -- the agent resolves it
    against its own configured hub base, which is the address it already trusts.
    """
    source = package.get("source") or {}
    kind = source.get("kind")
    payload = {"kind": kind}
    if kind == SOURCE_UPLOAD:
        payload["sha256"] = source.get("sha256")
        payload["file_name"] = source.get("file_name")
        payload["download_url"] = (
            f"{hub_url.rstrip('/')}/api/agent/packages/{source.get('sha256')}"
            if hub_url else f"/api/agent/packages/{source.get('sha256')}")
    elif kind == SOURCE_WINGET:
        payload["id"] = source.get("ref")
    else:  # url / unc
        payload["ref"] = source.get("ref")
        # Optional for these kinds; when present the agent MUST enforce it.
        if source.get("sha256"):
            payload["sha256"] = source["sha256"]

    return {
        "deployment_id": deployment["id"],
        "package_id": package["id"],
        "package_name": package["name"],
        "package_version": package.get("version"),
        "source": payload,
        "install_command": package["install_command"] or "",
        "install_args": package["install_args"] or "",
        "timeout_seconds": package["timeout_seconds"],
        "success_exit_codes": package["success_exit_codes"],
        "detection": package["detection"],
    }


# ================================
# SCHEDULER
# ================================
# NEVER call fleet.create_command (or anything else that writes on its own connection)
# from inside one of this module's `with get_conn(...)` blocks. Both write to the same
# SQLite file on separate connections, so the outer transaction's write lock blocks the
# inner one until it times out -- "database is locked", not a deadlock the timeout
# reveals quickly. Every function below therefore reads, closes, decides, and only then
# writes in short transactions.
def _terminal_outcome(command):
    """Map a command row's status onto (finished, succeeded, error_text)."""
    if command is None:
        # The command row is gone -- fleet.delete_machine cascades commands away when a
        # machine is hard-deleted. Nothing left to wait for.
        return True, False, "the command record no longer exists"
    status = command["status"]
    if status in (fleet.STATUS_PENDING, fleet.STATUS_CLAIMED):
        return False, False, None
    if status == fleet.STATUS_DONE:
        return True, True, None
    if status == fleet.STATUS_EXPIRED:
        return True, False, "the machine did not pick the command up before it expired"
    result = command.get("result") or {}
    output = (result.get("output") or "").strip()
    return True, False, output[:MAX_ERROR_CHARS] or "the agent reported a failure"


def _refresh_deployment_status(conn, deployment_id, now):
    """Roll per-target states up to the deployment. Cancelled is sticky -- an operator
    who stopped a deploy should not see it flip back to running because the one in-flight
    target finished."""
    row = conn.execute(
        "SELECT status FROM deployments WHERE id = ?", (deployment_id,)).fetchone()
    if row is None or row["status"] == DEPLOY_CANCELLED:
        return
    states = [r["status"] for r in conn.execute(
        "SELECT status FROM deployment_targets WHERE deployment_id = ?", (deployment_id,))]
    if states and all(s in TARGET_TERMINAL for s in states):
        status = DEPLOY_COMPLETE
    elif any(s != TARGET_PENDING for s in states):
        status = DEPLOY_RUNNING
    else:
        status = DEPLOY_SCHEDULED
    if status != row["status"]:
        conn.execute("UPDATE deployments SET status = ?, updated_at = ? WHERE id = ?",
                     (status, now, deployment_id))


def _retire(db_path, updates, now):
    """Apply a batch of terminal/backoff target updates and roll their deployments up.

    `updates` is a list of (deployment_id, machine, status, next_attempt_at, error).
    One short transaction for the whole batch, taken only after every decision is made.
    """
    if not updates:
        return 0
    with get_conn(db_path) as conn:
        for deployment_id, machine, status, next_at, error in updates:
            conn.execute(
                "UPDATE deployment_targets SET status = ?, next_attempt_at = ?, "
                "last_error = ?, updated_at = ? WHERE deployment_id = ? AND machine = ?",
                (status, next_at, error, now, deployment_id, machine),
            )
        for deployment_id in {u[0] for u in updates}:
            _refresh_deployment_status(conn, deployment_id, now)
    return len(updates)


def reconcile_once(db_path, now=None):
    """Read the outcome of every in-flight attempt back off the command queue.

    This is the half of the scheduler that does NOT issue anything. Splitting it from
    dispatch keeps the rule simple: a target can only be dispatched from `pending`, and
    only reconcile moves it out of `in_flight`, so there is no window where two ticks
    could each queue a command for the same machine.
    """
    if now is None:
        now = int(time.time())

    # Retire timed-out commands first. The queue only expires commands lazily, when an
    # agent for that machine polls -- and the machine a deploy is stuck on is precisely
    # the one that isn't polling. Without this sweep an offline target would sit
    # in_flight forever: never failing, so never retried and never given up on.
    fleet.expire_stale_commands(db_path, now)

    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.*, d.max_attempts, d.retry_backoff_seconds, d.window_end "
            "FROM deployment_targets t JOIN deployments d ON d.id = t.deployment_id "
            "WHERE t.status = ?", (TARGET_IN_FLIGHT,)
        )]

    updates = []
    for target in rows:
        if target["command_id"]:
            finished, succeeded, error = _terminal_outcome(
                fleet.get_command(db_path, target["command_id"]))
        else:
            # In flight with no command id: dispatch claimed the attempt and then died
            # before (or while) queueing the command -- see dispatch_once, which claims
            # first on purpose. Spend the attempt rather than leaving the row stranded;
            # a lost attempt is recoverable, a target nothing ever moves again is not.
            finished, succeeded, error = (
                True, False, "the attempt was interrupted before the command was queued")
        if not finished:
            continue

        if succeeded:
            status, next_at = TARGET_SUCCEEDED, None
            error = None
        elif target["attempts"] >= target["max_attempts"]:
            status, next_at = TARGET_FAILED, None
        elif target["window_end"] and target["window_end"] <= now:
            status, next_at = TARGET_EXPIRED, None
        else:
            # Exponential backoff on the attempt number: 1x, 2x, 4x... A machine that is
            # off for the weekend shouldn't be retried at a fixed 15-minute cadence for
            # 48 hours, and a genuinely broken installer shouldn't be hammered either.
            status = TARGET_PENDING
            next_at = now + target["retry_backoff_seconds"] * (2 ** (target["attempts"] - 1))

        updates.append((target["deployment_id"], target["machine"], status, next_at, error))

    return _retire(db_path, updates, now)


def _claim_target(db_path, deployment_id, machine, now):
    """Atomically move one target from `pending` to `in_flight`, spending an attempt.

    Returns True if this caller won the row. The UPDATE ... WHERE status = 'pending' is
    the claim: whoever's UPDATE changes a row owns the attempt, so two schedulers (or a
    tick overlapping a slow one) can never both queue an install for the same machine.

    The claim happens BEFORE the command is created, deliberately. If the process dies
    between the two, the target sits in_flight with a NULL command_id and reconcile
    charges it one failed attempt -- costing a retry. The other order would leave a
    queued command with a target still `pending`, and the next tick would install the
    package a second time. A wasted attempt is recoverable; a double install is not.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE deployment_targets SET status = ?, attempts = attempts + 1, "
            "command_id = NULL, next_attempt_at = NULL, updated_at = ? "
            "WHERE deployment_id = ? AND machine = ? AND status = ?",
            (TARGET_IN_FLIGHT, now, deployment_id, machine, TARGET_PENDING),
        )
        return (cur.rowcount or 0) == 1


def dispatch_once(db_path, now=None, ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS,
                  hub_url=""):
    """Queue a `deploy_package` command for every target that is due.

    Due means: the deployment is open, its window has started, and the target is
    `pending` with `next_attempt_at` in the past. Targets whose window has closed are
    retired as `expired` in the same pass, so a deploy that nobody was online for still
    reaches a terminal state instead of sitting `pending` forever.
    """
    if now is None:
        now = int(time.time())

    # ---- read: candidates and the rows they need, then close the connection ----
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.*, d.window_start, d.window_end, d.max_attempts "
            "FROM deployment_targets t JOIN deployments d ON d.id = t.deployment_id "
            "WHERE t.status = ? AND d.status IN (?, ?) "
            "ORDER BY t.next_attempt_at ASC",
            (TARGET_PENDING, DEPLOY_SCHEDULED, DEPLOY_RUNNING),
        )]
        deployments = {}
        for deployment_id in {r["deployment_id"] for r in rows}:
            row = conn.execute("SELECT * FROM deployments WHERE id = ?",
                               (deployment_id,)).fetchone()
            if row is not None:
                deployments[deployment_id] = dict(row)

    # ---- decide: retire what can't run, collect what can ----
    retire = []
    ready = []
    package_cache = {}
    for target in rows:
        deployment = deployments.get(target["deployment_id"])
        if deployment is None:
            continue

        if target["window_end"] and target["window_end"] <= now:
            retire.append((target["deployment_id"], target["machine"], TARGET_EXPIRED,
                           None, "the deployment window closed before this machine ran"))
            continue
        if target["window_start"] and target["window_start"] > now:
            continue
        if target["next_attempt_at"] and target["next_attempt_at"] > now:
            continue
        if target["attempts"] >= target["max_attempts"]:
            # Belt and braces: reconcile normally retires these, but a target must only
            # ever be dispatched while it still has attempt budget.
            retire.append((target["deployment_id"], target["machine"], TARGET_FAILED,
                           None, target["last_error"]))
            continue

        package_id = deployment["package_id"]
        if package_id not in package_cache:
            package_cache[package_id] = get_package(db_path, package_id)
        package = package_cache[package_id]
        if package is None:
            # The package was deleted mid-deployment. Retire the target with a real
            # reason rather than retrying something that can no longer be built.
            retire.append((target["deployment_id"], target["machine"], TARGET_FAILED,
                           None, "the package definition was deleted"))
            continue

        ready.append((target, deployment, package))

    _retire(db_path, retire, now)

    # ---- act: claim, queue, record. Each step its own short transaction. ----
    dispatched = 0
    touched = set()
    for target, deployment, package in ready:
        if not _claim_target(db_path, target["deployment_id"], target["machine"], now):
            continue  # someone else took it
        params = build_command_params(package, deployment, hub_url=hub_url)
        command_id = fleet.create_command(
            db_path, machine=target["machine"], command_type=COMMAND_TYPE,
            params=params, issued_by=deployment["created_by"], ttl_seconds=ttl_seconds,
        )
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE deployment_targets SET command_id = ?, updated_at = ? "
                "WHERE deployment_id = ? AND machine = ?",
                (command_id, now, target["deployment_id"], target["machine"]),
            )
        touched.add(target["deployment_id"])
        dispatched += 1

    if touched:
        with get_conn(db_path) as conn:
            for deployment_id in touched:
                _refresh_deployment_status(conn, deployment_id, now)
    return dispatched


def tick(db_path, now=None, ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS, hub_url=""):
    """One scheduler pass: reconcile finished attempts, then dispatch due ones.

    Reconcile FIRST so a target whose retry backoff has just elapsed can be dispatched in
    the same tick that observed its failure, rather than waiting a full tick interval.
    Returns (reconciled, dispatched) for the caller's log line.
    """
    reconciled = reconcile_once(db_path, now=now)
    dispatched = dispatch_once(db_path, now=now, ttl_seconds=ttl_seconds, hub_url=hub_url)
    return reconciled, dispatched


def forget_machine(db_path, machine):
    """Drop a deleted machine's target rows and roll its deployments up again.

    Mirrors permissions.forget_machine and fleet.delete_machine: a machine record going
    away must not leave a deployment permanently stuck at 9/10 because the tenth target
    points at a hostname that no longer exists.
    """
    machine = _clean(machine)
    if not machine:
        return
    now = int(time.time())
    with get_conn(db_path) as conn:
        affected = [r["deployment_id"] for r in conn.execute(
            "SELECT DISTINCT deployment_id FROM deployment_targets WHERE machine = ?",
            (machine,))]
        conn.execute("DELETE FROM deployment_targets WHERE machine = ?", (machine,))
        for deployment_id in affected:
            _refresh_deployment_status(conn, deployment_id, now)


def rename_machine(db_path, old_name, new_name):
    """Follow a machine through a duplicate-serial merge, like permissions.rename_machine.

    An INSERT OR IGNORE-style move: if the survivor is already a target of the same
    deployment, the dropped row is simply removed rather than colliding on the primary key.
    """
    old_name = _clean(old_name)
    new_name = _clean(new_name)
    if not old_name or not new_name or old_name == new_name:
        return
    now = int(time.time())
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT deployment_id FROM deployment_targets WHERE machine = ?", (old_name,)
        ).fetchall()
        for row in rows:
            exists = conn.execute(
                "SELECT 1 FROM deployment_targets WHERE deployment_id = ? AND machine = ?",
                (row["deployment_id"], new_name)).fetchone()
            if exists:
                conn.execute(
                    "DELETE FROM deployment_targets WHERE deployment_id = ? AND machine = ?",
                    (row["deployment_id"], old_name))
            else:
                conn.execute(
                    "UPDATE deployment_targets SET machine = ?, updated_at = ? "
                    "WHERE deployment_id = ? AND machine = ?",
                    (new_name, now, row["deployment_id"], old_name))
