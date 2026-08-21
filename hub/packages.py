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

  * **A package is a list of STEPS, and one command line is just the short list.** Real
    software rarely installs in one call: a driver pack is download, unpack, `pnputil`.
    Expressing that as a single chained command line hides which part failed and mixes
    three programs' output into one blob. So `packages.steps_json` holds an ordered list,
    each step carrying its own timeout and success codes, and `package_sources` holds
    NAMED payloads that steps refer to by name. A package with no steps is the original
    one-command recipe, still stored and still dispatched exactly as it was -- that is what
    lets an agent that predates this keep running every package written before it.

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

# ================================
# STEPS
# ================================
# A package may carry an ordered list of STEPS instead of one command line. The motivating
# shape is a driver pack: download a zip, unpack it, hand the directory to pnputil. Doing
# that with the single-command recipe means shelling out to a one-liner that chains three
# things with `&&`, where a failure in the middle is invisible and the operator's only
# record is a wall of mixed output.
#
# Five kinds, and the same discipline as the detection grammar: every kind here is code the
# C# agent must implement and keep working, so the list is short on purpose and there is no
# expression language. `powershell` is the deliberate escape hatch -- copying a file,
# poking the registry, restarting a service -- so the other four can stay narrow instead of
# growing options to cover every adjacent case.
STEP_RUN = "run"                # an executable with arguments
STEP_POWERSHELL = "powershell"  # an inline script, the general-purpose escape hatch
STEP_WINGET = "winget"          # winget install <id>
STEP_EXTRACT = "extract"        # unpack a .zip into a directory
STEP_PNPUTIL = "pnputil"        # stage/install driver packages
STEP_KINDS = (STEP_RUN, STEP_POWERSHELL, STEP_WINGET, STEP_EXTRACT, STEP_PNPUTIL)

# Labels/descriptions live in the catalogs under `packages.step.<kind>.<label|description>`,
# exactly like DETECTION_TEXT_KEY -- so the step palette in the editor is served by the API
# in the caller's language, and a kind added without catalog entries fails tests/test_i18n.py
# rather than captioning a button with its own key.
STEP_TEXT_KEY = "packages.step"

# pnputil's exit codes are their own dialect. 0 is success and 3010 is the usual "reboot
# required", but a driver pack that installed cleanly also routinely reports 259
# (ERROR_NO_MORE_ITEMS) once it has walked the last INF. Treating that as failure paints a
# perfectly good driver rollout red on most of a fleet -- the same trap as 3010, one layer
# down -- so it is in the default set, and the editor says so rather than hiding it here.
DEFAULT_PNPUTIL_EXIT_CODES = (0, 259, 3010)

# Payload slots. Steps refer to payloads by NAME, so a package can carry more than one file
# (a driver zip and a config, say) and each step says which one it means.
DEFAULT_SOURCE_NAME = "payload"

# The working directory the agent creates for one attempt, and deletes afterward. Bound as
# a variable so a step can put things there without hardcoding a path.
WORK_VAR = "work"

# Every step-based package binds `{file}` to its single payload when it has exactly one.
# Purely for continuity: `{file}` is what every existing package and every operator already
# types, and having it keep working is what makes adding a step to an existing package a
# one-click change rather than a rewrite.
LEGACY_FILE_VAR = "file"

# What counts as a variable inside `{...}`. Narrow ON PURPOSE: msiexec command lines are
# full of literal product codes like {90160000-008C-0000-1000-0000000FF1CE}, and a rule
# that treated every brace pair as a variable would reject them -- or worse, substitute
# them. Hyphens and uppercase are not in this grammar, so a GUID is never mistaken for a
# variable, and an unknown lowercase {word} is a typo worth failing on.
_VARIABLE_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Which string fields of each kind are variable-substituted (and therefore validated for
# unknown variables). `winget.id` is absent deliberately: a package id is a literal.
_STEP_SUBSTITUTED = {
    STEP_RUN: ("command", "args"),
    STEP_POWERSHELL: ("script",),
    STEP_WINGET: ("args",),
    STEP_EXTRACT: ("archive", "dest"),
    STEP_PNPUTIL: ("path",),
}

MAX_STEPS = 25
MAX_SCRIPT_CHARS = 10000

# Post-install detection. Three kinds plus an explicit opt-out, held to that deliberately:
# each one is code the C# agent must implement and keep working across Windows versions.
DETECT_NONE = "none"
DETECT_FILE_EXISTS = "file_exists"
DETECT_REGISTRY_VALUE = "registry_value"
DETECT_INSTALLED_VERSION = "installed_version"
DETECTION_KINDS = (DETECT_NONE, DETECT_FILE_EXISTS, DETECT_REGISTRY_VALUE,
                   DETECT_INSTALLED_VERSION)

# The label and description each detection kind is shown with live in the translation
# catalogs, under `packages.detection.<kind>.label` / `.description`; `packages_web`
# resolves them in the caller's language when it serves the vocabulary. Same move, and
# the same reasoning, as permissions.CAPABILITY_TEXT_KEY: the API stays self-describing,
# but one string has one home, and `en.json` is that home. A kind added without entries
# fails tests/test_i18n.py rather than captioning the package form with its own key.
DETECTION_TEXT_KEY = "packages.detection"

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
        # The ordered step list, as JSON. A table would buy nothing here: steps are always
        # read and written as a whole, never queried across packages, and are snapshotted
        # verbatim into the command params -- so a second table would add an ordering
        # column and a join to store one document. NULL means "no steps": the original
        # install_command recipe, which is still how most packages are written.
        package_columns = {row["name"] for row in conn.execute("PRAGMA table_info(packages)")}
        if "steps_json" not in package_columns:
            conn.execute("ALTER TABLE packages ADD COLUMN steps_json TEXT")
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
        # `name` is the slot a step refers to. It arrived with steps: one command line only
        # ever needed one payload, but "unpack the driver zip, then run the vendor's
        # config tool from the other download" needs to say which file it means.
        source_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(package_sources)")}
        if "name" not in source_columns:
            conn.execute("ALTER TABLE package_sources ADD COLUMN name TEXT")
            # Existing rows are the one payload their package has, and every existing
            # command line calls it {file} -- which is bound to the single source by name
            # below. Naming them explicitly here (rather than tolerating NULL forever)
            # keeps one code path for reading sources.
            conn.execute("UPDATE package_sources SET name = ? WHERE name IS NULL",
                         (DEFAULT_SOURCE_NAME,))
        # The old index was UNIQUE(package_id) -- one payload per package, which the
        # original docstring flagged as the seam extra payloads would land on. This is that
        # change: uniqueness is now per SLOT. Dropped by name rather than left in place,
        # because leaving it would silently refuse the second payload.
        conn.execute("DROP INDEX IF EXISTS idx_package_sources_package")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_package_sources_slot "
            "ON package_sources(package_id, name)"
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


def validate_name(value, field):
    """A variable-safe identifier: lowercase, starts with a letter, then letters, digits
    and underscores.

    Lowercase specifically, rather than case-insensitive-unique: `{Drivers}` and
    `{drivers}` in two steps of the same package would otherwise be a bug an operator
    could stare straight through. One spelling, no fold to get wrong.
    """
    name = _clean(value).lower()
    if not _NAME_RE.match(name):
        raise ValueError(
            f"{field} must start with a letter and contain only lowercase letters, "
            f"digits and underscores (up to 32 characters)")
    return name


def validate_source(source, default_name=DEFAULT_SOURCE_NAME):
    """Normalize + validate a payload descriptor.

    `source` is {name, kind, ref, sha256, file_name, file_size}. For an upload the caller
    has already stored the blob and passes the hash the HUB computed -- this function never
    treats a client-supplied digest as authoritative for an upload, it just checks shape.
    """
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    name = validate_name(source.get("name") or default_name, "a payload name")
    if name == WORK_VAR:
        # {work} is the attempt's own directory, bound by the agent. A payload claiming
        # that name would shadow it, and every step referring to {work} would silently
        # mean the file instead of the folder.
        raise ValueError(f"{WORK_VAR!r} is reserved -- it is the working directory")
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
        "name": name,
        "kind": kind,
        "ref": ref or None,
        "sha256": sha256,
        "file_name": _clean(source.get("file_name"), MAX_NAME_CHARS) or None,
        "file_size": int(size) if size not in (None, "") else None,
    }


def validate_sources(sources):
    """Normalize a package's whole payload list. Returns a list of source dicts.

    Accepts a single source dict as well as a list, because a one-payload package is still
    the common case and the console posts whichever it has. An empty list is legal -- a
    package can be pure winget, or pure powershell, with nothing to download.
    """
    if sources is None:
        return []
    if isinstance(sources, dict):
        sources = [sources]
    if not isinstance(sources, (list, tuple)):
        raise ValueError("sources must be a list of payload objects")

    validated = []
    seen = set()
    for index, source in enumerate(sources):
        # The default name is only unambiguous for the first slot; later ones must say what
        # they are, because "payload2" chosen by the hub is a name no step author expects.
        item = validate_source(
            source, default_name=DEFAULT_SOURCE_NAME if index == 0 else "")
        if item["name"] in seen:
            raise ValueError(f"two payloads are both named {item['name']!r}")
        seen.add(item["name"])
        validated.append(item)
    return validated


def _referenced_variables(text):
    """Every `{name}` in a string that is shaped like a variable. See _VARIABLE_RE for why
    a literal MSI product code is deliberately not one."""
    return set(_VARIABLE_RE.findall(str(text or "")))


def _step_int(step, field, low, high, default=None):
    value = step.get(field)
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer")
    if not (low <= parsed <= high):
        raise ValueError(f"{field} must be between {low} and {high}")
    return parsed


def validate_steps(steps, source_names=()):
    """Normalize + validate an ordered step list against the payloads it may refer to.

    Returns [] for a package with no steps -- the original single-command recipe, which
    every caller still handles.

    Two properties are worth more than the field checking:

      * **Every `{variable}` must already be bound when the step runs.** Payload names are
        bound before step 1; a step that produces a path (`extract`) binds its `save_as`
        for the steps AFTER it. So a typo, or a step ordered before the thing it needs, is
        an error at save time rather than a path called literally "{drivers}" appearing in
        an installer's arguments on 200 machines at 2am.
      * **Unknown keys are dropped**, exactly as in validate_detection: these objects are
        handed to the agent and executed there, so preserving arbitrary fields would make
        the effective grammar whatever the agent happens to read.
    """
    if steps is None or steps == "":
        return []
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except (TypeError, ValueError):
            raise ValueError("steps must be a JSON array")
    if not isinstance(steps, (list, tuple)):
        raise ValueError("steps must be a list")
    if not steps:
        return []
    if len(steps) > MAX_STEPS:
        raise ValueError(f"a package may have at most {MAX_STEPS} steps")

    bound = {WORK_VAR} | set(source_names)
    if len(source_names) == 1:
        # One payload, so `{file}` is unambiguous -- and it is what every package written
        # before steps existed already says. See LEGACY_FILE_VAR.
        bound.add(LEGACY_FILE_VAR)

    validated = []
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"step {index} must be an object")
        kind = _clean(raw.get("kind"))
        if kind not in STEP_KINDS:
            raise ValueError(f"step {index}: unknown step kind {kind!r}")

        step = {"kind": kind}
        label = _clean(raw.get("name"), MAX_NAME_CHARS)
        if label:
            step["name"] = label

        if kind == STEP_RUN:
            command = _clean(raw.get("command"), MAX_COMMAND_CHARS)
            if not command:
                raise ValueError(f"step {index}: a run step needs a command")
            step["command"] = command
            args = _clean(raw.get("args"), MAX_COMMAND_CHARS)
            if args:
                step["args"] = args

        elif kind == STEP_POWERSHELL:
            script = str(raw.get("script") or "").strip()
            if not script:
                raise ValueError(f"step {index}: a PowerShell step needs a script")
            if len(script) > MAX_SCRIPT_CHARS:
                raise ValueError(
                    f"step {index}: the script must be {MAX_SCRIPT_CHARS} characters or fewer")
            step["script"] = script

        elif kind == STEP_WINGET:
            package_id = _clean(raw.get("id"), MAX_NAME_CHARS)
            if not package_id:
                raise ValueError(f"step {index}: a winget step needs a package id")
            step["id"] = package_id
            args = _clean(raw.get("args"), MAX_COMMAND_CHARS)
            if args:
                step["args"] = args

        elif kind == STEP_EXTRACT:
            archive = _clean(raw.get("archive"), MAX_COMMAND_CHARS)
            if not archive:
                raise ValueError(
                    f"step {index}: an extract step needs the archive to unpack, "
                    f"e.g. {{{source_names[0] if source_names else DEFAULT_SOURCE_NAME}}}")
            step["archive"] = archive
            dest = _clean(raw.get("dest"), MAX_COMMAND_CHARS)
            if dest:
                step["dest"] = dest

        else:  # STEP_PNPUTIL
            path = _clean(raw.get("path"), MAX_COMMAND_CHARS)
            if not path:
                raise ValueError(
                    f"step {index}: a pnputil step needs the folder or .inf to install")
            step["path"] = path
            # Driver packs nest by model and chipset far more often than not, so recursing
            # is the default and turning it off is the deliberate act.
            step["subdirs"] = bool(raw.get("subdirs", True))

        # ---- shared options ----
        timeout = _step_int(raw, "timeout_seconds", 30, 24 * 3600)
        if timeout is not None:
            step["timeout_seconds"] = timeout
        if raw.get("success_exit_codes") not in (None, ""):
            step["success_exit_codes"] = validate_exit_codes(raw["success_exit_codes"])
        elif kind == STEP_PNPUTIL:
            # Resolved HERE rather than defaulted in the agent, so the editor can show the
            # operator the set their driver step will actually be judged by. See
            # DEFAULT_PNPUTIL_EXIT_CODES for why 259 is in it.
            step["success_exit_codes"] = list(DEFAULT_PNPUTIL_EXIT_CODES)
        if raw.get("continue_on_error"):
            step["continue_on_error"] = True

        # ---- variables ----
        unknown = set()
        for field in _STEP_SUBSTITUTED[kind]:
            unknown |= _referenced_variables(step.get(field)) - bound
        if unknown:
            raise ValueError(
                f"step {index} refers to {'{'}{sorted(unknown)[0]}{'}'}, which nothing "
                f"before it provides. Available here: "
                f"{', '.join('{' + name + '}' for name in sorted(bound))}")

        if kind == STEP_EXTRACT:
            # The unpacked directory is the whole point of the step, so it always binds a
            # name. Auto-numbered when the operator does not care, which is the common case.
            save_as = raw.get("save_as")
            if save_as in (None, ""):
                save_as = "extracted"
                suffix = 2
                while save_as in bound:
                    save_as = f"extracted{suffix}"
                    suffix += 1
            else:
                save_as = validate_name(save_as, "save_as")
            if save_as in bound:
                raise ValueError(
                    f"step {index}: {{{save_as}}} is already taken by an earlier step or a "
                    f"payload -- give this one a different name")
            step["save_as"] = save_as
            bound.add(save_as)

        validated.append(step)
    return validated


def _validate_recipe(install_command, install_args, sources, steps, timeout_seconds):
    """The command line (or the step list) and the timeout. Create and update share it.

    A package is written one of two ways and never both: an ordered `steps` list, or the
    original single `install_command`. Accepting both would leave the question of which one
    runs to be answered by whichever agent picked the command up.
    """
    command = _clean(install_command, MAX_COMMAND_CHARS)
    args = _clean(install_args, MAX_COMMAND_CHARS)

    if steps:
        if command or args:
            raise ValueError(
                "a package with steps has no top-level install command -- the steps are "
                "the recipe; move that command line into a run step")
        winget_sources = [s["name"] for s in sources if s["kind"] == SOURCE_WINGET]
        if winget_sources:
            # A winget "payload" is not a file, so there is nothing for a step to point at.
            # In the step grammar winget is an ACTION, which is also the only shape that
            # lets a package install two winget apps, or one alongside an MSI.
            raise ValueError(
                f"payload {winget_sources[0]!r} is a winget reference, which a step-based "
                f"package cannot use as a payload -- add a winget step instead")
        # Same rule as {file} below, generalized: a payload the hub downloads onto every
        # target and no step ever opens is always a mistake, never an intent.
        used = set()
        for step in steps:
            for field in _STEP_SUBSTITUTED[step["kind"]]:
                used |= _referenced_variables(step.get(field))
        if len(sources) == 1 and LEGACY_FILE_VAR in used:
            used.add(sources[0]["name"])
        unused = [s["name"] for s in sources if s["name"] not in used]
        if unused:
            raise ValueError(
                f"no step uses the payload {{{unused[0]}}} -- it would be downloaded to "
                f"every machine and never opened")

    elif not sources:
        raise ValueError("a package needs a payload, or a list of steps")

    elif sources[0]["kind"] == SOURCE_WINGET:
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

    if not steps and len(sources) > 1:
        # One command line can only name one file. Extra payloads would be downloaded and
        # unreachable, so this is the same "never opened" mistake as above.
        raise ValueError(
            "a package with more than one payload needs steps to say what to do with them")

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
def _package_row(row, source_rows=()):
    pkg = dict(row)
    pkg["success_exit_codes"] = json.loads(pkg.pop("success_exit_codes"))
    pkg["detection"] = json.loads(pkg.pop("detection_json"))
    pkg["steps"] = json.loads(pkg.pop("steps_json", None) or "[]")
    sources = []
    for source_row in source_rows:
        source = dict(source_row)
        source.pop("package_id", None)
        # A row written before payloads had names is that package's single payload.
        source["name"] = source.get("name") or DEFAULT_SOURCE_NAME
        sources.append(source)
    pkg["sources"] = sources
    # `source` (singular) is the first payload, kept because every existing caller -- the
    # console's package list, build_command_params' legacy projection, the blob collector --
    # was written when a package had exactly one. It is the same object, not a copy of it.
    pkg["source"] = sources[0] if sources else None
    return pkg


def _sources_by_package(conn, package_ids=None):
    """Payload rows grouped by package, in slot order."""
    sql = "SELECT * FROM package_sources"
    params = []
    if package_ids is not None:
        if not package_ids:
            return {}
        sql += f" WHERE package_id IN ({','.join('?' for _ in package_ids)})"
        params = list(package_ids)
    grouped = {}
    for row in conn.execute(sql + " ORDER BY created_at, rowid", params).fetchall():
        grouped.setdefault(row["package_id"], []).append(row)
    return grouped


def list_packages(db_path):
    """Every package with its payloads, newest first. Small table by nature -- a fleet has
    tens of packages, not thousands -- so there is no pagination here on purpose."""
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM packages ORDER BY name COLLATE NOCASE").fetchall()
        sources = _sources_by_package(conn)
    return [_package_row(row, sources.get(row["id"], ())) for row in rows]


def get_package(db_path, package_id):
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM packages WHERE id = ?", (str(package_id),)).fetchone()
        if row is None:
            return None
        sources = _sources_by_package(conn, [str(package_id)])
    return _package_row(row, sources.get(str(package_id), ()))


def create_package(db_path, *, name, source=None, sources=None, steps=None,
                   install_command=None, install_args=None,
                   description=None, version=None, timeout_seconds=900,
                   success_exit_codes=None, detection=None, actor="system"):
    """Define a package. Returns its id.

    `source` (one payload) and `sources` (a list) are the same argument at different
    arities; `source` is kept because every caller that predates steps passes it.

    Everything is validated before anything is written, so a rejected definition never
    leaves a half-created package (or, worse, a source row pointing at a blob nobody
    will ever clean up) behind.
    """
    name = _clean(name, MAX_NAME_CHARS)
    if not name:
        raise ValueError("a package name is required")
    payloads = validate_sources(sources if sources is not None else source)
    plan = validate_steps(steps, [s["name"] for s in payloads])
    command, args, timeout = _validate_recipe(
        install_command, install_args, payloads, plan, timeout_seconds)
    codes = validate_exit_codes(success_exit_codes)
    rule = validate_detection(detection)

    package_id = uuid.uuid4().hex
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO packages(id, name, description, version, install_command, "
                "install_args, timeout_seconds, success_exit_codes, detection_json, "
                "steps_json, created_at, updated_at, created_by, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (package_id, name, _clean(description, 2000) or None,
                 _clean(version, 60) or None, command, args or None, timeout,
                 json.dumps(codes), json.dumps(rule, sort_keys=True),
                 json.dumps(plan) if plan else None,
                 now, now, str(actor), str(actor)),
            )
            _write_sources(conn, package_id, payloads, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"a package named {name!r} already exists")

    fleet.audit(db_path, actor=actor, action="create_package",
                level=fleet.LEVEL_NOTICE, target=name,
                detail={"package_id": package_id,
                        "sources": [s["kind"] for s in payloads],
                        "steps": [s["kind"] for s in plan]})
    return package_id


def _write_sources(conn, package_id, payloads, now):
    """Replace a package's payload rows wholesale.

    Delete-then-insert rather than an upsert per slot: a slot's KIND can change (an upload
    becoming a URL reference), slots can be removed entirely, and a partial update would
    leave a stale sha256 behind pointing at a blob the recipe no longer uses.
    """
    conn.execute("DELETE FROM package_sources WHERE package_id = ?", (package_id,))
    conn.executemany(
        "INSERT INTO package_sources(id, package_id, name, kind, ref, sha256, file_name, "
        "file_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(uuid.uuid4().hex, package_id, source["name"], source["kind"], source["ref"],
          source["sha256"], source["file_name"], source["file_size"], now)
         for source in payloads],
    )


def update_package(db_path, package_id, *, name=None, source=None, sources=None,
                   steps=None, install_command=None,
                   install_args=None, description=None, version=None,
                   timeout_seconds=None, success_exit_codes=None, detection=None,
                   actor="system", blob_root_dir=None):
    """Patch a package. Every argument is optional; None means "leave alone".

    When a payload changes, the OLD blob is offered to the orphan collector -- but only
    after the new rows are committed, so a crash between the two loses disk space rather
    than the file a package still points at. Pass `blob_root_dir` to enable that; without
    it the old blob is simply left on disk (which is what the unit tests do).

    Note that changing the recipe does NOT retroactively alter deployments already in
    flight: their command params were snapshotted at dispatch (see build_command_params),
    so a machine can't get half of one recipe and half of another.
    """
    existing = get_package(db_path, package_id)
    if existing is None:
        raise KeyError("unknown package")

    old_sources = existing.get("sources") or []
    incoming = sources if sources is not None else source
    new_sources = validate_sources(incoming) if incoming is not None else None
    payloads = new_sources if new_sources is not None else old_sources

    # Steps and payloads are validated together (a step may only name a payload that
    # exists), and so are the steps and the command line (a package has one or the other).
    # So a change to any of them re-checks the whole recipe rather than the field that
    # moved -- otherwise deleting a payload would leave the step that used it dangling.
    plan = (existing["steps"] if steps is None
            else validate_steps(steps, [s["name"] for s in payloads]))
    if steps is None and new_sources is not None:
        # Re-validate the kept steps against the new payload set for exactly that reason.
        plan = validate_steps(plan, [s["name"] for s in payloads])

    command = existing["install_command"] if install_command is None else install_command
    args = existing["install_args"] if install_args is None else install_args
    timeout = existing["timeout_seconds"] if timeout_seconds is None else timeout_seconds
    command, args, timeout = _validate_recipe(command, args, payloads, plan, timeout)

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
                "success_exit_codes = ?, detection_json = ?, steps_json = ?, "
                "updated_at = ?, updated_by = ? WHERE id = ?",
                (new_name,
                 existing["description"] if description is None
                 else (_clean(description, 2000) or None),
                 existing["version"] if version is None else (_clean(version, 60) or None),
                 command, args or None, timeout, json.dumps(codes),
                 json.dumps(rule, sort_keys=True), json.dumps(plan) if plan else None,
                 now, str(actor), str(package_id)),
            )
            if new_sources is not None:
                _write_sources(conn, str(package_id), new_sources, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"a package named {new_name!r} already exists")

    if new_sources is not None and blob_root_dir:
        kept = {s.get("sha256") for s in new_sources}
        for old in old_sources:
            if old.get("sha256") and old["sha256"] not in kept:
                delete_blob_if_orphaned(db_path, blob_root_dir, old["sha256"])

    fleet.audit(db_path, actor=actor, action="update_package",
                level=fleet.LEVEL_NOTICE, target=new_name,
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

    if blob_root_dir:
        for source in existing.get("sources") or []:
            if source.get("sha256"):
                delete_blob_if_orphaned(db_path, blob_root_dir, source["sha256"])

    fleet.audit(db_path, actor=actor, action="delete_package",
                level=fleet.LEVEL_NOTICE, target=existing["name"],
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
                level=fleet.LEVEL_SECURITY,
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



# ---------------------------------------------------------------- Dashboard tallies
#
# Counted in SQL rather than by loading rows and length-ing them. The Dashboard asks all of
# these on one poll, from every open console, so a helper that returned a hundred rows for a
# number would be the most expensive thing on the page by a wide margin.
#
# `machines` is an optional iterable used as the scope filter, applied HERE rather than by
# the caller for the same reason: dropping out-of-scope rows in Python means reading them
# first, and an operator scoped to three machines would still pay for the whole fleet.


def count_deployment_states(db_path, machines=None, since=None):
    """How many deployment targets are in flight, and how many have failed.

    Two different questions with two different time frames, which is why `since` applies to
    the failures only. "Running" is a state a target is IN -- it is either unresolved now or
    it is not -- while "failed" is an event, and a failure from three weeks ago on a
    Dashboard reading "1 failed" would be a permanent, meaningless red mark.
    """
    running_clauses = ["t.status IN (?, ?)"]
    running_params = [TARGET_PENDING, TARGET_IN_FLIGHT]
    failed_clauses = ["t.status = ?"]
    failed_params = [TARGET_FAILED]

    scope = [_clean(m) for m in machines] if machines is not None else None
    if scope is not None:
        if not scope:
            return {"running": 0, "failed": 0}
        placeholders = ",".join("?" for _ in scope)
        running_clauses.append(f"t.machine IN ({placeholders})")
        running_params.extend(scope)
        failed_clauses.append(f"t.machine IN ({placeholders})")
        failed_params.extend(scope)
    if since is not None:
        # The TARGET's updated_at, which is when it went to `failed`, not the deployment's
        # created_at: a push scheduled last month whose target failed an hour ago is news,
        # and a push created this morning that failed at 09:00 stops being news tomorrow.
        failed_clauses.append("t.updated_at >= ?")
        failed_params.append(int(since))

    with get_conn(db_path) as conn:
        running = conn.execute(
            "SELECT COUNT(*) FROM deployment_targets t WHERE " + " AND ".join(running_clauses),
            running_params).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM deployment_targets t WHERE "
            + " AND ".join(failed_clauses), failed_params).fetchone()[0]
    return {"running": int(running), "failed": int(failed)}


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
    fleet.audit(db_path, actor=actor, action="cancel_deployment",
                level=fleet.LEVEL_NOTICE, target=deployment_id)


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
        fleet.audit(db_path, actor=actor, action="retry_deployment",
                    level=fleet.LEVEL_SECURITY, target=deployment_id,
                    detail={"requeued": requeued})
    return requeued


# ================================
# COMMAND PAYLOAD
# ================================
def _wire_source(source, hub_url=""):
    """One payload as the agent receives it."""
    kind = source.get("kind")
    payload = {"name": source.get("name") or DEFAULT_SOURCE_NAME, "kind": kind}
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
    return payload


def build_command_params(package, deployment, hub_url=""):
    """The params the agent receives for one `deploy_package` command.

    A full SNAPSHOT of the recipe, not a pointer to it. The agent could in principle be
    handed a package id and told to fetch the definition, but then editing a package
    while a deployment is in flight would silently change what half the fleet installs.
    Snapshotting means a target always runs the recipe that was current when its attempt
    was dispatched, and the audit log records exactly that.

    `download_url` is relative when no hub URL is configured -- the agent resolves it
    against its own configured hub base, which is the address it already trusts.

    THE LEGACY PROJECTION is the interesting part. Agents older than steps read `source`,
    `install_command` and `install_args` and know nothing about `steps` or `sources`. So:

      * A package with NO steps is emitted exactly as it always was, and an old agent runs
        it correctly. Every package written before this feature keeps working on every
        agent in the field, which is the whole point -- the fleet updates itself in about
        fifteen minutes, but not all at once and not while the operator is watching.
      * A package WITH steps has no honest single-command projection, so it deliberately
        emits none: `install_command` is empty and `source.kind` is "multi", which no old
        agent resolves. Such an agent fails the deploy with "no install command" and the
        target retries until the update reaches it. Failing closed is the requirement --
        the alternative is an old agent silently running step 1 and reporting success.
    """
    sources = package.get("sources") or ([package["source"]] if package.get("source") else [])
    steps = package.get("steps") or []

    if steps:
        legacy_source = {"kind": "multi", "count": len(sources)}
        legacy_command, legacy_args = "", ""
    else:
        legacy_source = _wire_source(sources[0], hub_url) if sources else {"kind": "none"}
        legacy_command = package["install_command"] or ""
        legacy_args = package["install_args"] or ""

    return {
        "deployment_id": deployment["id"],
        "package_id": package["id"],
        "package_name": package["name"],
        "package_version": package.get("version"),
        "source": legacy_source,
        "sources": [_wire_source(s, hub_url) for s in sources],
        "steps": steps,
        "install_command": legacy_command,
        "install_args": legacy_args,
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


def pending_target_machines(db_path, now=None):
    """The machines this scheduler is about to dispatch to, if they are reachable.

    Exists for Wake-on-LAN (roadmap #10): a maintenance window that dispatches into a dark
    office installs nothing, so the wake feature asks which machines a window is waiting on
    and switches the offline ones on. Deliberately a READ that answers a question rather
    than a hook that does something -- `wake` does not belong inside this dispatch path, and
    this module has no business knowing that waking exists.

    Same due-ness test as dispatch_once, minus the per-target retry clock: a target waiting
    out its backoff is still a machine the window needs awake shortly.
    """
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT t.machine FROM deployment_targets t "
            "JOIN deployments d ON d.id = t.deployment_id "
            "WHERE t.status = ? AND d.status IN (?, ?) "
            "  AND (d.window_start IS NULL OR d.window_start <= ?) "
            "  AND (d.window_end IS NULL OR d.window_end > ?)",
            (TARGET_PENDING, DEPLOY_SCHEDULED, DEPLOY_RUNNING, now, now)).fetchall()
    return [row["machine"] for row in rows]


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
