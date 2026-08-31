"""Patch management -- keep the fleet's operating systems and applications current.

Roadmap #14. The console could push an installer an operator wrote (`packages.py`) and
flash a BIOS image an operator uploaded (`firmware.py`), but it could not answer the one
question every helpdesk is actually asked about a fleet: *is it patched?* This module
answers it, and then closes the gap.

The insight that sizes the feature is that **patching is the package engine with a
vendor-supplied catalogue**. `packages.py` already has payload sources, detection, a
dispatch/reconcile loop over per-machine target rows, retries with backoff, cancel and
retry-failures. None of that is rebuilt here. What patching adds is everything that
follows from the operator not choosing the payload:

  * **The catalogue is discovered, not authored.** An agent reports what Windows Update
    and `winget` say is available FOR THAT MACHINE, change-only on the heartbeat, exactly
    the way `BiosInventoryReporter` and `NicReader` already report. A patch therefore has
    no recipe to validate and no blob to store -- `machine_patches` is an observation, and
    the hub's job is to decide which observations are allowed to become installs.

  * **An install is confirmed by re-reading inventory, never by an exit code.** This is
    `firmware.py`'s discipline and it is here for the same reason, one layer down: a
    silent installer that exits 0 having done nothing is the normal failure mode of this
    entire problem domain, and Windows Update reports success for updates that need a
    restart before they are, in any useful sense, installed. So a target moves to
    REBOOTING when the agent says it staged what it could, and only the machine's own next
    patch inventory closes it out -- `confirm_from_inventory`, called from the heartbeat
    ingest path, so a fleet with no run in flight pays nothing. A run may sit in REBOOTING
    for a day. That is correct behaviour, not a stall.

  * **Approvals are fleet-wide per update; windows are per group.** Approving KB5060842 is
    a statement about the patch, and it does not become a different patch on a different
    desk -- so a decision is keyed on the update's identity and nothing else. *When* it may
    run is the thing that legitimately varies between a server rack and a reception PC, so
    the maintenance window carries the scope. Doing it the other way round (approve
    per-group, schedule globally) means the same KB can be approved here and not there,
    which is not a policy anybody can hold in their head.

  * **Unlike a flash, a patch may be retried; unlike a package, its content is decided at
    dispatch.** A run does not snapshot a list of updates when it is created. It carries a
    SELECTION RULE -- "everything approved", or an explicit set -- and each target resolves
    it against that machine's own inventory at the moment it is dispatched. A machine that
    was offline on Tuesday gets Wednesday's approved set when it comes back, rather than a
    Tuesday snapshot that is now missing a supersedence. `patch_run_items` records what was
    actually attempted per machine, which is why it exists as a third table.

**`patch_run_items` is also the scoring substrate, and that is deliberate.** It records the
machine's model and OS build beside each attempted update and its outcome, from the first
release. The PRD's "AI stability scoring" -- flag a patch other machines had trouble with
before it goes wider -- is then a query over history rather than a migration, and nothing
here has to guess in advance what that query will be. No scoring is implemented; the
columns cost nothing and their absence would cost a schema change.

**Emergency push is a run that ignores every window.** It is gated on `manage_patches`,
which is its own capability rather than a reuse of `deploy_packages`: forcing a restart
outside an agreed window is a different thing to hand somebody than pushing an installer
inside one, and folding the two together would have granted it silently, on the day it
shipped, to everyone who already had the other.

**Maintenance windows are evaluated in the HUB's local time.** One helpdesk, one site, one
clock -- and a window expressed in the operator's own time is the one they can reason about
at 5pm on a Friday. The day a fleet spans timezones, the fix is a per-machine offset applied
in `window_is_open`, which is why that decision lives in one function.

Authorization lives entirely upstream at `manage_patches` (or `view` for the read side)
plus machine scope -- see patches_web.py. Nothing here checks a session, exactly like
fleet.py, packages.py and firmware.py.

Kept free of Flask so it can be unit-tested in isolation.
"""
import json
import re
import sqlite3
import time
import uuid

import fleet

# ================================
# VOCABULARY
# ================================
# The command type the scheduler queues. Must be registered in fleet.ALL_COMMANDS so
# create_command accepts it and the agent's dispatcher can route it.
COMMAND_TYPE = "install_patches"

# Where an available update came from. Two sources, and the list is short for the same
# reason packages' step kinds are: each one is code the C# agent must implement and keep
# working across Windows versions.
SOURCE_WINDOWS_UPDATE = "windows_update"   # the WUApiLib COM search interface
SOURCE_WINGET = "winget"                   # `winget upgrade`
SOURCE_KINDS = (SOURCE_WINDOWS_UPDATE, SOURCE_WINGET)

# How Windows classifies an update, normalised. `security` and `critical` are the two that
# auto-approval can be switched on for, because they are the two whose absence is a finding
# rather than a preference. `unknown` is a real answer, not a parse failure: winget has no
# notion of classification at all, and saying so is better than filing every application
# update under `other` as though somebody had decided that.
CLASS_SECURITY = "security"
CLASS_CRITICAL = "critical"
CLASS_DRIVER = "driver"
CLASS_FEATURE = "feature"
CLASS_OTHER = "other"
CLASS_UNKNOWN = "unknown"
CLASSIFICATIONS = (CLASS_SECURITY, CLASS_CRITICAL, CLASS_DRIVER, CLASS_FEATURE,
                   CLASS_OTHER, CLASS_UNKNOWN)

# Which classifications `patches.auto_approve_classifications` may name. Feature updates and
# drivers are deliberately not offerable: a feature update is a Windows version migration
# and a driver is the one class of patch that reliably breaks hardware, and neither is a
# thing to hand to a scheduler because somebody ticked a box once.
AUTO_APPROVABLE = (CLASS_SECURITY, CLASS_CRITICAL)

# Labels and descriptions live in the translation catalogs under
# `patches.classification.<kind>.label` / `.description` and `patches.source.<kind>.*`,
# exactly like packages.DETECTION_TEXT_KEY and permissions.CAPABILITY_TEXT_KEY -- so the API
# stays self-describing while the console reads in the operator's language, and a kind added
# without catalog entries fails tests/test_i18n.py rather than captioning a filter with its
# own key.
CLASSIFICATION_TEXT_KEY = "patches.classification"
SOURCE_TEXT_KEY = "patches.source"

# An approval decision. The ABSENCE of a row is the third state -- undecided -- and it is
# the default for everything the fleet has never been asked about. Storing "undecided"
# explicitly would mean every newly discovered update wrote a row nobody asked for.
APPROVAL_APPROVED = "approved"
APPROVAL_DECLINED = "declined"
APPROVAL_DECISIONS = (APPROVAL_APPROVED, APPROVAL_DECLINED)

# Run lifecycle. Mirrors packages' deployment vocabulary exactly, including that
# `cancelled` is sticky -- an operator who stopped a run should not see it flip back to
# running because the one in-flight machine finished.
RUN_SCHEDULED = "scheduled"
RUN_RUNNING = "running"
RUN_COMPLETE = "complete"
RUN_CANCELLED = "cancelled"
RUN_STATUSES = (RUN_SCHEDULED, RUN_RUNNING, RUN_COMPLETE, RUN_CANCELLED)
RUN_OPEN_STATUSES = (RUN_SCHEDULED, RUN_RUNNING)

# Per-machine target lifecycle. The middle of this list is where patching differs from
# packaging: an install does not finish when the command is answered.
TARGET_PENDING = "pending"
TARGET_IN_FLIGHT = "in_flight"       # command queued, agent has not answered yet
# The agent answered: it installed what it could and the machine needs a restart before any
# of it counts. Only confirm_from_inventory leaves this state -- or expire_stale, which
# eventually calls an unanswered reboot a failure.
TARGET_REBOOTING = "rebooting"
TARGET_APPLIED = "applied"           # inventory came back and the updates are gone from it
# Some of the attempted set applied and some did not. Its own outcome rather than a failure,
# because "eleven of thirteen" is the normal result of a patch night and painting it red
# teaches operators to ignore the colour.
TARGET_PARTIAL = "partial"
TARGET_FAILED = "failed"             # attempts exhausted, or the agent refused outright
# Dispatch resolved the selection against this machine and there was nothing to do. A real
# outcome and not a silent skip: "nothing to install" and "we never got to it" look
# identical on a progress bar, and only one of them is fine.
TARGET_NOTHING_TO_DO = "nothing_to_do"
TARGET_EXPIRED = "expired"           # the window closed before this machine ran
TARGET_CANCELLED = "cancelled"
TARGET_STATUSES = (TARGET_PENDING, TARGET_IN_FLIGHT, TARGET_REBOOTING, TARGET_APPLIED,
                   TARGET_PARTIAL, TARGET_FAILED, TARGET_NOTHING_TO_DO, TARGET_EXPIRED,
                   TARGET_CANCELLED)
TARGET_TERMINAL = frozenset({TARGET_APPLIED, TARGET_PARTIAL, TARGET_FAILED,
                             TARGET_NOTHING_TO_DO, TARGET_EXPIRED, TARGET_CANCELLED})
# States a cancel may still recall a target from. Once a machine is REBOOTING the patches
# are on it; "cancelled" would be a lie about the state of that PC.
TARGET_RECALLABLE = frozenset({TARGET_PENDING, TARGET_IN_FLIGHT})

# Per-update outcome inside one target. Deliberately coarser than the target vocabulary:
# these are the three things the evidence can actually distinguish once the machine has
# come back and said what it still needs.
ITEM_APPLIED = "applied"
ITEM_FAILED = "failed"
ITEM_PENDING = "pending"     # attempted, machine not back yet -- the transient state
ITEM_STATUSES = (ITEM_APPLIED, ITEM_FAILED, ITEM_PENDING)

# What a run installs. `approved` re-resolves per machine at dispatch (see the module
# docstring); `explicit` is the operator having picked exact updates in the console, which
# is how a single KB gets pushed at a single PC.
SELECTION_APPROVED = "approved"
SELECTION_EXPLICIT = "explicit"
SELECTIONS = (SELECTION_APPROVED, SELECTION_EXPLICIT)

# Reboot policy on a window. `if_required` is the only sane default: never reboot leaves a
# fleet permanently one restart short of actually being patched, and always reboot restarts
# machines that did not need it.
REBOOT_NEVER = "never"
REBOOT_IF_REQUIRED = "if_required"
REBOOT_ALWAYS = "always"
REBOOT_POLICIES = (REBOOT_NEVER, REBOOT_IF_REQUIRED, REBOOT_ALWAYS)

# How a maintenance window picks its machines. Mirrors the machine-scope vocabulary the
# permission groups already use, rather than inventing a second way to say "these PCs".
SCOPE_ALL = "all"
SCOPE_MACHINES = "machines"      # an explicit hostname list
SCOPE_KINDS = (SCOPE_ALL, SCOPE_MACHINES)

MAX_NAME_CHARS = 120
MAX_UID_CHARS = 200
MAX_TITLE_CHARS = 400
MAX_KB_CHARS = 32
MAX_NOTE_CHARS = 500
MAX_ERROR_CHARS = 2000
MAX_UPDATES_PER_REPORT = 500
MAX_UPDATES_PER_RUN = 200
MAX_TARGETS_PER_RUN = 500
MAX_WINDOW_MACHINES = 500

# A run's default retry policy, matching packages'. A patch that failed to install is worth
# another go -- unlike a flash, which gets exactly one (see firmware.py).
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 900

# How long a target may sit in REBOOTING before it is called a failure. A day, matching
# firmware's confirm timeout and for the same reason: a machine that is off for the weekend
# has not failed, and the alternative to waiting is reporting an outcome nobody observed.
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 24 * 3600

_UID_RE = re.compile(r"^[a-z0-9][a-z0-9._:+-]{0,199}$")
_KB_RE = re.compile(r"^KB[0-9]{5,10}$", re.IGNORECASE)

# Minutes in a day, for window arithmetic.
_DAY_MINUTES = 24 * 60


class PatchError(ValueError):
    """Bad input from an operator or an agent. Carries a message meant to be shown."""


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_patches_db(db_path):
    """Create the patch tables if absent. Idempotent -- safe to call next to the other
    init_*_db() functions on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        # WHAT IS AVAILABLE. An observation, replaced wholesale each time a machine
        # reports -- see ingest_inventory for why this is a replace and not a merge.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_patches (
                machine         TEXT NOT NULL,
                uid             TEXT NOT NULL,   -- stable identity, see normalize_uid
                source          TEXT NOT NULL,   -- SOURCE_KINDS
                kb              TEXT,            -- KB number where there is one
                title           TEXT NOT NULL,
                classification  TEXT NOT NULL,   -- CLASSIFICATIONS
                reboot_required INTEGER NOT NULL DEFAULT 0,
                size_bytes      INTEGER,
                first_seen      INTEGER NOT NULL,
                last_seen       INTEGER NOT NULL,
                PRIMARY KEY (machine, uid)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_patches_uid "
                     "ON machine_patches(uid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_patches_class "
                     "ON machine_patches(classification)")

        # WHAT IS ALLOWED. Keyed on the update's identity alone -- no machine column, on
        # purpose. See the module docstring.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patch_approvals (
                uid         TEXT PRIMARY KEY,
                decision    TEXT NOT NULL,   -- APPROVAL_DECISIONS
                title       TEXT,            -- last title seen, so a decision reads as prose
                note        TEXT,
                decided_at  INTEGER NOT NULL,
                decided_by  TEXT NOT NULL,
                auto        INTEGER NOT NULL DEFAULT 0   -- set by classification, not a human
            )
            """
        )

        # WHEN IT MAY RUN.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_windows (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                enabled         INTEGER NOT NULL DEFAULT 1,
                days_mask       INTEGER NOT NULL,   -- bit 0 = Monday .. bit 6 = Sunday
                start_minute    INTEGER NOT NULL,   -- minutes past local midnight
                duration_minutes INTEGER NOT NULL,
                scope_kind      TEXT NOT NULL,      -- SCOPE_KINDS
                scope_json      TEXT NOT NULL,      -- JSON array of hostnames when scoped
                reboot_policy   TEXT NOT NULL,      -- REBOOT_POLICIES
                created_at      INTEGER NOT NULL,
                created_by      TEXT NOT NULL,
                updated_at      INTEGER NOT NULL,
                updated_by      TEXT
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_maintenance_windows_name "
                     "ON maintenance_windows(name COLLATE NOCASE)")

        # WHAT WAS DONE.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patch_runs (
                id                    TEXT PRIMARY KEY,
                note                  TEXT,
                status                TEXT NOT NULL,
                selection             TEXT NOT NULL,   -- SELECTIONS
                selection_json        TEXT NOT NULL,   -- explicit uid list, [] when approved
                window_id             TEXT,            -- NULL for an ad-hoc or emergency run
                emergency             INTEGER NOT NULL DEFAULT 0,
                reboot_policy         TEXT NOT NULL,
                window_start          INTEGER,
                window_end            INTEGER,
                max_attempts          INTEGER NOT NULL,
                retry_backoff_seconds INTEGER NOT NULL,
                confirm_timeout_seconds INTEGER NOT NULL,
                created_at            INTEGER NOT NULL,
                created_by            TEXT NOT NULL,
                updated_at            INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_patch_runs_status "
                     "ON patch_runs(status)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patch_run_targets (
                run_id          TEXT NOT NULL,
                machine         TEXT NOT NULL,
                status          TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER,
                command_id      TEXT,
                staged_at       INTEGER,      -- when the agent said "restart me"
                reboot_required INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT,
                updated_at      INTEGER NOT NULL,
                PRIMARY KEY (run_id, machine)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_patch_run_targets_machine "
                     "ON patch_run_targets(machine)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_patch_run_targets_status "
                     "ON patch_run_targets(run_id, status)")

        # WHAT WAS ATTEMPTED, PER UPDATE. The row the stability question will be asked of
        # later, which is why `model` and `os_build` are denormalised onto it: the machine
        # they describe may have been re-imaged, renamed or deleted by the time anybody
        # asks, and an outcome you cannot attribute to a configuration answers nothing.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patch_run_items (
                id           TEXT PRIMARY KEY,
                run_id       TEXT NOT NULL,
                machine      TEXT NOT NULL,
                uid          TEXT NOT NULL,
                kb           TEXT,
                title        TEXT,
                classification TEXT,
                status       TEXT NOT NULL,   -- ITEM_STATUSES
                error        TEXT,
                model        TEXT,            -- as reported when the attempt was made
                os_build     TEXT,
                attempted_at INTEGER NOT NULL,
                resolved_at  INTEGER,         -- when inventory confirmed or denied it
                UNIQUE (run_id, machine, uid)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_patch_run_items_uid "
                     "ON patch_run_items(uid, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_patch_run_items_target "
                     "ON patch_run_items(run_id, machine)")


# ================================
# VALIDATION
# ================================
def _clean(value, limit=None):
    text = str(value or "").strip()
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_list(raw):
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def normalize_uid(value):
    """The stable identity of an update, lowercased.

    Case folding is the whole point: Windows Update reports `KB5060842` and winget reports
    ids in whatever case the publisher chose, and an approval that failed to match because
    of a capital letter is the worst kind of bug here -- it looks like the patch was simply
    not offered. Raises rather than returning a falsy value, because every caller of this
    is either storing a decision or matching one.
    """
    text = _clean(value, MAX_UID_CHARS).lower()
    if not text:
        raise PatchError("An update needs an identifier.")
    if not _UID_RE.match(text):
        raise PatchError(f"Not a usable update identifier: {value!r}.")
    return text


def normalize_kb(value):
    """`KB5060842` however it was typed, or "" -- plenty of updates genuinely have no KB."""
    text = _clean(value, MAX_KB_CHARS)
    if not text:
        return ""
    if not text.upper().startswith("KB"):
        text = "KB" + text
    return text.upper() if _KB_RE.match(text) else ""


def normalize_classification(value):
    """Map whatever the agent said onto CLASSIFICATIONS, defaulting to `unknown`.

    Unknown rather than `other` on purpose -- see the vocabulary comment. This is the one
    place a new Windows classification string can appear, and it must not raise: an update
    the hub cannot categorise is still an update the operator needs to see.
    """
    text = _clean(value).lower().replace(" ", "_")
    if text in CLASSIFICATIONS:
        return text
    # The handful of aliases Windows actually emits.
    if text in ("security_updates", "security_update"):
        return CLASS_SECURITY
    if text in ("critical_updates", "critical_update"):
        return CLASS_CRITICAL
    if text in ("drivers",):
        return CLASS_DRIVER
    if text in ("feature_packs", "upgrades", "upgrade"):
        return CLASS_FEATURE
    if text in ("updates", "update_rollups", "definition_updates", "tools",
                "service_packs"):
        return CLASS_OTHER
    return CLASS_UNKNOWN


def validate_window(*, name, days_mask, start_minute, duration_minutes,
                    scope_kind, machines=(), reboot_policy=REBOOT_IF_REQUIRED):
    """Check one maintenance window, returning the cleaned field dict or raising.

    A window with no days, or a zero duration, is refused rather than stored as a window
    that silently never opens -- the failure mode being avoided is an operator believing a
    fleet patches itself on Sundays when nothing has ever run.
    """
    clean_name = _clean(name, MAX_NAME_CHARS)
    if not clean_name:
        raise PatchError("A maintenance window needs a name.")

    mask = _int_or_none(days_mask)
    if mask is None or not 0 < mask <= 0b1111111:
        raise PatchError("Pick at least one day of the week.")

    start = _int_or_none(start_minute)
    if start is None or not 0 <= start < _DAY_MINUTES:
        raise PatchError("The start time must be a time of day.")

    duration = _int_or_none(duration_minutes)
    if duration is None or not 0 < duration <= _DAY_MINUTES:
        raise PatchError("The window must be between 1 minute and 24 hours long.")

    if scope_kind not in SCOPE_KINDS:
        raise PatchError(f"Unknown scope: {scope_kind!r}.")
    hosts = []
    if scope_kind == SCOPE_MACHINES:
        seen = set()
        for raw in machines or ():
            host = _clean(raw, 63)
            key = host.lower()
            if host and key not in seen:
                seen.add(key)
                hosts.append(host)
        if not hosts:
            raise PatchError("Pick at least one machine, or scope the window to all of them.")
        if len(hosts) > MAX_WINDOW_MACHINES:
            raise PatchError(
                f"A window covers at most {MAX_WINDOW_MACHINES} machines by name; "
                f"scope it to all machines instead.")

    if reboot_policy not in REBOOT_POLICIES:
        raise PatchError(f"Unknown reboot policy: {reboot_policy!r}.")

    return {
        "name": clean_name,
        "days_mask": mask,
        "start_minute": start,
        "duration_minutes": duration,
        "scope_kind": scope_kind,
        "machines": hosts,
        "reboot_policy": reboot_policy,
    }


# ================================
# INVENTORY
# ================================
def _patch_row(row):
    return {
        "machine": row["machine"],
        "uid": row["uid"],
        "source": row["source"],
        "kb": row["kb"] or "",
        "title": row["title"],
        "classification": row["classification"],
        "reboot_required": bool(row["reboot_required"]),
        "size_bytes": row["size_bytes"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    }


def parse_report(updates):
    """Normalise an agent's available-updates list, dropping what cannot be understood.

    Dropping rather than raising: one malformed entry in a report of two hundred must not
    cost the hub the other hundred and ninety-nine, and an agent version that grows a field
    this hub has never heard of should be ingestible by an older hub. The cap is applied
    here rather than at the endpoint so both callers get it.
    """
    seen = set()
    out = []
    for raw in (updates or [])[:MAX_UPDATES_PER_REPORT]:
        if not isinstance(raw, dict):
            continue
        source = _clean(raw.get("source")).lower()
        if source not in SOURCE_KINDS:
            continue
        try:
            uid = normalize_uid(raw.get("uid"))
        except PatchError:
            continue
        if uid in seen:
            continue
        title = _clean(raw.get("title"), MAX_TITLE_CHARS)
        if not title:
            # A patch with no title is unactionable in a console -- an operator cannot
            # approve a blank row -- so the uid stands in rather than the row being dropped.
            title = uid
        seen.add(uid)
        size = _int_or_none(raw.get("size_bytes"))
        out.append({
            "uid": uid,
            "source": source,
            "kb": normalize_kb(raw.get("kb")),
            "title": title,
            "classification": normalize_classification(raw.get("classification")),
            "reboot_required": bool(raw.get("reboot_required")),
            "size_bytes": size if size and size > 0 else None,
        })
    return out


def ingest_inventory(db_path, machine, updates, now=None):
    """Replace a machine's available-update set with what it just reported.

    A REPLACE and not a merge, which is the only correct choice: the interesting event in
    this table is an update DISAPPEARING, because that is what "it installed" looks like
    from the hub. A merge would leave every patch a machine has ever needed listed forever,
    and `confirm_from_inventory` -- which reads exactly this absence -- would never fire.

    `first_seen` survives the replace, so "this box has been missing a security update for
    three weeks" stays answerable. Returns (added, removed, kept).
    """
    host = _clean(machine, 63)
    if not host:
        return (0, 0, 0)
    if now is None:
        now = int(time.time())
    parsed = parse_report(updates)
    incoming = {u["uid"]: u for u in parsed}

    with get_conn(db_path) as conn:
        existing = {r["uid"]: r["first_seen"] for r in conn.execute(
            "SELECT uid, first_seen FROM machine_patches WHERE machine = ?", (host,))}
        gone = [uid for uid in existing if uid not in incoming]
        if gone:
            conn.executemany(
                "DELETE FROM machine_patches WHERE machine = ? AND uid = ?",
                [(host, uid) for uid in gone])
        for uid, update in incoming.items():
            conn.execute(
                "INSERT INTO machine_patches (machine, uid, source, kb, title, "
                "                             classification, reboot_required, size_bytes, "
                "                             first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(machine, uid) DO UPDATE SET "
                "  source = excluded.source, kb = excluded.kb, title = excluded.title, "
                "  classification = excluded.classification, "
                "  reboot_required = excluded.reboot_required, "
                "  size_bytes = excluded.size_bytes, last_seen = excluded.last_seen",
                (host, uid, update["source"], update["kb"], update["title"],
                 update["classification"], 1 if update["reboot_required"] else 0,
                 update["size_bytes"], existing.get(uid, now), now),
            )
    added = len([u for u in incoming if u not in existing])
    return (added, len(gone), len(incoming) - added)


def list_machine_patches(db_path, machine):
    """Everything one machine currently reports as available, newest finding first."""
    host = _clean(machine, 63)
    if not host:
        return []
    with get_conn(db_path) as conn:
        return [_patch_row(r) for r in conn.execute(
            "SELECT * FROM machine_patches WHERE machine = ? "
            "ORDER BY first_seen DESC, title COLLATE NOCASE", (host,))]


def list_fleet_patches(db_path, machines=None):
    """Available updates rolled up across the fleet, one row per update identity.

    `machines`, when given, is the caller's machine scope -- the same intersection every
    other fleet-wide read in this product applies. None means unscoped; an EMPTY collection
    means a scope that matched nothing, and must return nothing rather than everything.
    That distinction is the one this signature exists to preserve.
    """
    where, args = "", []
    if machines is not None:
        hosts = [_clean(m, 63) for m in machines if _clean(m, 63)]
        if not hosts:
            return []
        where = f" WHERE machine IN ({','.join('?' * len(hosts))})"
        args = hosts
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT uid, source, MAX(kb) AS kb, MAX(title) AS title, "
            "       MAX(classification) AS classification, "
            "       MAX(reboot_required) AS reboot_required, "
            "       COUNT(*) AS machines, MIN(first_seen) AS first_seen, "
            "       MAX(last_seen) AS last_seen "
            f"FROM machine_patches{where} "
            "GROUP BY uid, source ORDER BY machines DESC, title COLLATE NOCASE", args)
        out = []
        for row in rows:
            out.append({
                "uid": row["uid"],
                "source": row["source"],
                "kb": row["kb"] or "",
                "title": row["title"],
                "classification": row["classification"],
                "reboot_required": bool(row["reboot_required"]),
                "machines": row["machines"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            })
    decisions = approvals_map(db_path, [r["uid"] for r in out])
    for row in out:
        row["decision"] = decisions.get(row["uid"], "")
    return out


def known_machines(db_path):
    """Every machine that currently has an available-update row.

    The web layer narrows THIS through the caller's machine filter and hands the result
    back as a scope, rather than the roster of the whole fleet: the filter is a predicate,
    the scoped reads below want a list, and the machines that matter are the ones this
    table actually holds. A fleet-wide read therefore costs one DISTINCT over an indexed
    column instead of a join against the machine table.
    """
    with get_conn(db_path) as conn:
        return sorted(r["machine"] for r in conn.execute(
            "SELECT DISTINCT machine FROM machine_patches"))


def prune_inventory(db_path, retention_days, now=None):
    """Drop available-update rows nothing has re-reported inside the retention window.

    Nothing else prunes this table, and it is one row per update per machine. A machine
    that stops reporting -- decommissioned, re-imaged, agent broken -- would otherwise keep
    its last answer forever, and every compliance figure would carry it. Returns the number
    of rows dropped.
    """
    if now is None:
        now = int(time.time())
    cutoff = now - int(retention_days) * 86400
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM machine_patches WHERE last_seen < ?", (cutoff,))
    return cur.rowcount or 0


def compliance_summary(db_path, machines=None):
    """Counts for the fleet card: how many machines are behind, and on what.

    Deliberately says nothing about machines with no row at all. A PC that has never
    reported patch inventory is not compliant -- it is unknown -- and the caller knows the
    roster this scope covers, so it can subtract. Reporting unknown as compliant here is
    exactly the arithmetic that makes a dashboard say 100%.
    """
    where, args = "", []
    if machines is not None:
        hosts = [_clean(m, 63) for m in machines if _clean(m, 63)]
        if not hosts:
            return {"machines_with_updates": 0, "updates": 0, "by_classification": {},
                    "reporting": 0}
        where = f" WHERE machine IN ({','.join('?' * len(hosts))})"
        args = hosts
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT machine) AS machines, COUNT(*) AS updates "
            f"FROM machine_patches{where}", args).fetchone()
        by_class = {r["classification"]: r["n"] for r in conn.execute(
            "SELECT classification, COUNT(*) AS n "
            f"FROM machine_patches{where} GROUP BY classification", args)}
    return {
        "machines_with_updates": row["machines"] or 0,
        "updates": row["updates"] or 0,
        "by_classification": by_class,
        "reporting": row["machines"] or 0,
    }


# ================================
# APPROVALS
# ================================
def set_approval(db_path, uid, decision, *, actor, title="", note="", auto=False,
                 now=None):
    """Record a decision about one update. Idempotent; re-deciding overwrites."""
    key = normalize_uid(uid)
    if decision not in APPROVAL_DECISIONS:
        raise PatchError(f"Unknown decision: {decision!r}.")
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO patch_approvals (uid, decision, title, note, decided_at, "
            "                             decided_by, auto) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET decision = excluded.decision, "
            "  title = COALESCE(NULLIF(excluded.title, ''), patch_approvals.title), "
            "  note = excluded.note, decided_at = excluded.decided_at, "
            "  decided_by = excluded.decided_by, auto = excluded.auto",
            (key, decision, _clean(title, MAX_TITLE_CHARS), _clean(note, MAX_NOTE_CHARS),
             now, _clean(actor, 200) or "system", 1 if auto else 0),
        )
    return key


def clear_approval(db_path, uid):
    """Return an update to undecided. Absence is a state -- see the schema comment."""
    key = normalize_uid(uid)
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM patch_approvals WHERE uid = ?", (key,))
    return key


def approvals_map(db_path, uids):
    """{uid: decision} for the uids given. Missing keys are undecided."""
    keys = []
    for raw in uids or ():
        try:
            keys.append(normalize_uid(raw))
        except PatchError:
            continue
    if not keys:
        return {}
    out = {}
    with get_conn(db_path) as conn:
        # Chunked so a fleet-wide list cannot outgrow SQLite's parameter limit.
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            for row in conn.execute(
                    f"SELECT uid, decision FROM patch_approvals "
                    f"WHERE uid IN ({','.join('?' * len(chunk))})", chunk):
                out[row["uid"]] = row["decision"]
    return out


def list_approvals(db_path, decision=None):
    where, args = "", []
    if decision:
        where, args = " WHERE decision = ?", [decision]
    with get_conn(db_path) as conn:
        return [{
            "uid": r["uid"], "decision": r["decision"], "title": r["title"] or "",
            "note": r["note"] or "", "decided_at": r["decided_at"],
            "decided_by": r["decided_by"], "auto": bool(r["auto"]),
        } for r in conn.execute(
            f"SELECT * FROM patch_approvals{where} ORDER BY decided_at DESC", args)]


def apply_auto_approvals(db_path, classifications, *, now=None):
    """Approve every undecided update whose classification is on the list.

    Only ever ADDS approvals, and only to updates nobody has decided on: an operator who
    declined a specific KB must not have that reversed because its classification is
    auto-approved. Returns the number of new approvals. A no-op when the list is empty,
    which is the shipped default.
    """
    wanted = [c for c in (classifications or ()) if c in AUTO_APPROVABLE]
    if not wanted:
        return 0
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.uid, p.title FROM machine_patches p "
            "LEFT JOIN patch_approvals a ON a.uid = p.uid "
            f"WHERE a.uid IS NULL AND p.classification IN ({','.join('?' * len(wanted))})",
            wanted).fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO patch_approvals (uid, decision, title, note, "
                "                                       decided_at, decided_by, auto) "
                "VALUES (?,?,?,?,?,?,1)",
                (row["uid"], APPROVAL_APPROVED, row["title"], "", now, "auto-approval"),
            )
    return len(rows)


def approved_for_machine(db_path, machine):
    """The updates this machine currently reports that are also approved.

    This is the selection a run resolves at DISPATCH time, per machine -- see the module
    docstring on why a run does not snapshot a list when it is created.
    """
    host = _clean(machine, 63)
    if not host:
        return []
    with get_conn(db_path) as conn:
        return [_patch_row(r) for r in conn.execute(
            "SELECT p.* FROM machine_patches p "
            "JOIN patch_approvals a ON a.uid = p.uid "
            "WHERE p.machine = ? AND a.decision = ? "
            "ORDER BY p.classification, p.title COLLATE NOCASE",
            (host, APPROVAL_APPROVED))]


def selected_for_machine(db_path, machine, uids):
    """The subset of an explicit uid list this machine actually reports as available.

    Filtering against inventory rather than trusting the list is what stops a run trying to
    install a KB on a machine that does not need it, which Windows Update answers with a
    failure that reads exactly like a broken patch.
    """
    host = _clean(machine, 63)
    keys = []
    for raw in uids or ():
        try:
            keys.append(normalize_uid(raw))
        except PatchError:
            continue
    if not host or not keys:
        return []
    out = []
    with get_conn(db_path) as conn:
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            out.extend(_patch_row(r) for r in conn.execute(
                "SELECT * FROM machine_patches WHERE machine = ? "
                f"AND uid IN ({','.join('?' * len(chunk))})", [host] + chunk))
    return out


# ================================
# MAINTENANCE WINDOWS
# ================================
def _window_row(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "days_mask": row["days_mask"],
        "start_minute": row["start_minute"],
        "duration_minutes": row["duration_minutes"],
        "scope_kind": row["scope_kind"],
        "machines": _json_list(row["scope_json"]),
        "reboot_policy": row["reboot_policy"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"] or "",
    }


def create_window(db_path, *, actor, now=None, **fields):
    clean = validate_window(**fields)
    if now is None:
        now = int(time.time())
    window_id = uuid.uuid4().hex
    with get_conn(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO maintenance_windows (id, name, enabled, days_mask, "
                "  start_minute, duration_minutes, scope_kind, scope_json, reboot_policy, "
                "  created_at, created_by, updated_at, updated_by) "
                "VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?)",
                (window_id, clean["name"], clean["days_mask"], clean["start_minute"],
                 clean["duration_minutes"], clean["scope_kind"],
                 json.dumps(clean["machines"]), clean["reboot_policy"],
                 now, _clean(actor, 200) or "system", now, None),
            )
        except sqlite3.IntegrityError:
            raise PatchError(f"A maintenance window called {clean['name']!r} already exists.")
    return window_id


def update_window(db_path, window_id, *, actor, enabled=None, now=None, **fields):
    existing = get_window(db_path, window_id)
    if existing is None:
        raise PatchError("That maintenance window no longer exists.")
    merged = {
        "name": fields.get("name", existing["name"]),
        "days_mask": fields.get("days_mask", existing["days_mask"]),
        "start_minute": fields.get("start_minute", existing["start_minute"]),
        "duration_minutes": fields.get("duration_minutes", existing["duration_minutes"]),
        "scope_kind": fields.get("scope_kind", existing["scope_kind"]),
        "machines": fields.get("machines", existing["machines"]),
        "reboot_policy": fields.get("reboot_policy", existing["reboot_policy"]),
    }
    clean = validate_window(**merged)
    if now is None:
        now = int(time.time())
    flag = existing["enabled"] if enabled is None else bool(enabled)
    with get_conn(db_path) as conn:
        try:
            conn.execute(
                "UPDATE maintenance_windows SET name = ?, enabled = ?, days_mask = ?, "
                "  start_minute = ?, duration_minutes = ?, scope_kind = ?, scope_json = ?, "
                "  reboot_policy = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (clean["name"], 1 if flag else 0, clean["days_mask"], clean["start_minute"],
                 clean["duration_minutes"], clean["scope_kind"],
                 json.dumps(clean["machines"]), clean["reboot_policy"], now,
                 _clean(actor, 200) or "system", window_id),
            )
        except sqlite3.IntegrityError:
            raise PatchError(f"A maintenance window called {clean['name']!r} already exists.")
    return window_id


def delete_window(db_path, window_id):
    """Remove a window. Runs it already created keep their own copy of the schedule, so
    deleting a window never rewrites history -- the same rule packages applies to a
    deployment's retry policy."""
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM maintenance_windows WHERE id = ?", (window_id,))
    return cur.rowcount > 0


def get_window(db_path, window_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM maintenance_windows WHERE id = ?",
                           (window_id,)).fetchone()
    return _window_row(row) if row else None


def list_windows(db_path):
    with get_conn(db_path) as conn:
        return [_window_row(r) for r in conn.execute(
            "SELECT * FROM maintenance_windows ORDER BY name COLLATE NOCASE")]


def window_is_open(window, now=None):
    """Is this window open right now, in the HUB's local time?

    Local time is the decision recorded in the module docstring, and this is the one
    function a per-machine timezone would change.

    A window that starts at 23:00 and runs four hours is open at 01:00 on the FOLLOWING
    day, and that following day need not be one of its selected days -- the days are when
    the window OPENS, not the set of moments it covers. Getting this backwards means a
    Sunday-night patch window silently stops at midnight, which is when it has barely
    started.
    """
    if not window or not window.get("enabled"):
        return False
    stamp = time.localtime(now if now is not None else time.time())
    minute_of_day = stamp.tm_hour * 60 + stamp.tm_min
    start = window["start_minute"]
    duration = window["duration_minutes"]
    today = stamp.tm_wday                       # 0 = Monday, matching the mask's bit 0
    yesterday = (today - 1) % 7

    if window["days_mask"] & (1 << today) and start <= minute_of_day < start + duration:
        return True
    # The spill-over from a window that opened yesterday.
    if window["days_mask"] & (1 << yesterday):
        end = start + duration
        if end > _DAY_MINUTES and minute_of_day < end - _DAY_MINUTES:
            return True
    return False


def window_covers(window, machine):
    host = _clean(machine, 63).lower()
    if not host:
        return False
    if window["scope_kind"] == SCOPE_ALL:
        return True
    return host in {_clean(m, 63).lower() for m in window["machines"]}


def open_windows_for(db_path, machine, now=None):
    """Every enabled window that is open now and covers this machine."""
    return [w for w in list_windows(db_path)
            if window_is_open(w, now) and window_covers(w, machine)]


# ================================
# RUNS
# ================================
def _run_row(row):
    return {
        "id": row["id"],
        "note": row["note"] or "",
        "status": row["status"],
        "selection": row["selection"],
        "selection_uids": _json_list(row["selection_json"]),
        "window_id": row["window_id"] or "",
        "emergency": bool(row["emergency"]),
        "reboot_policy": row["reboot_policy"],
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "max_attempts": row["max_attempts"],
        "retry_backoff_seconds": row["retry_backoff_seconds"],
        "confirm_timeout_seconds": row["confirm_timeout_seconds"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
    }


def _target_row(row):
    return {
        "run_id": row["run_id"],
        "machine": row["machine"],
        "status": row["status"],
        "attempts": row["attempts"],
        "next_attempt_at": row["next_attempt_at"],
        "command_id": row["command_id"] or "",
        "staged_at": row["staged_at"],
        "reboot_required": bool(row["reboot_required"]),
        "last_error": row["last_error"] or "",
        "updated_at": row["updated_at"],
    }


def create_run(db_path, *, machines, created_by, selection=SELECTION_APPROVED,
               uids=(), note="", window_id="", emergency=False,
               reboot_policy=REBOOT_IF_REQUIRED, window_start=None, window_end=None,
               max_attempts=DEFAULT_MAX_ATTEMPTS,
               retry_backoff_seconds=DEFAULT_RETRY_BACKOFF_SECONDS,
               confirm_timeout_seconds=DEFAULT_CONFIRM_TIMEOUT_SECONDS, now=None):
    """Create a patch run and its per-machine target rows.

    The run stores the retry and confirm policy it was created with rather than reading
    settings at dispatch, so an operator who agreed to three attempts gets three attempts
    even if somebody edits the default an hour later. Same rule, same reason, as
    packages.create_deployment.
    """
    if selection not in SELECTIONS:
        raise PatchError(f"Unknown selection: {selection!r}.")
    hosts, seen = [], set()
    for raw in machines or ():
        host = _clean(raw, 63)
        key = host.lower()
        if host and key not in seen:
            seen.add(key)
            hosts.append(host)
    if not hosts:
        raise PatchError("Pick at least one machine.")
    if len(hosts) > MAX_TARGETS_PER_RUN:
        raise PatchError(f"A run covers at most {MAX_TARGETS_PER_RUN} machines.")

    keys = []
    if selection == SELECTION_EXPLICIT:
        for raw in uids or ():
            keys.append(normalize_uid(raw))
        if not keys:
            raise PatchError("Pick at least one update, or run everything approved.")
        if len(keys) > MAX_UPDATES_PER_RUN:
            raise PatchError(f"A run installs at most {MAX_UPDATES_PER_RUN} updates.")

    if reboot_policy not in REBOOT_POLICIES:
        raise PatchError(f"Unknown reboot policy: {reboot_policy!r}.")
    if now is None:
        now = int(time.time())

    run_id = uuid.uuid4().hex
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO patch_runs (id, note, status, selection, selection_json, "
            "  window_id, emergency, reboot_policy, window_start, window_end, "
            "  max_attempts, retry_backoff_seconds, confirm_timeout_seconds, "
            "  created_at, created_by, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, _clean(note, MAX_NOTE_CHARS), RUN_SCHEDULED, selection,
             json.dumps(keys), _clean(window_id) or None, 1 if emergency else 0,
             reboot_policy, window_start, window_end, int(max_attempts),
             int(retry_backoff_seconds), int(confirm_timeout_seconds),
             now, _clean(created_by, 200) or "system", now),
        )
        conn.executemany(
            "INSERT INTO patch_run_targets (run_id, machine, status, attempts, "
            "  next_attempt_at, updated_at) VALUES (?,?,?,0,NULL,?)",
            [(run_id, host, TARGET_PENDING, now) for host in hosts],
        )
    return run_id


def get_run(db_path, run_id, *, with_targets=True):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM patch_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = _run_row(row)
        if with_targets:
            run["targets"] = [_target_row(r) for r in conn.execute(
                "SELECT * FROM patch_run_targets WHERE run_id = ? "
                "ORDER BY machine COLLATE NOCASE", (run_id,))]
    return run


def run_machines(db_path, run_id):
    """Every machine a run targets. What a scoped write has to be checked against."""
    with get_conn(db_path) as conn:
        return sorted(r["machine"] for r in conn.execute(
            "SELECT machine FROM patch_run_targets WHERE run_id = ?", (run_id,)))


def list_runs(db_path, limit=100, machine=None, machines=None):
    """Runs, newest first.

    `machine` narrows to one machine's runs. `machines` is the caller's machine SCOPE and
    narrows to runs touching at least one of them -- None means unscoped, and an EMPTY
    collection means a scope that matched nothing and must return nothing. Collapsing those
    two is how a fleet-wide list gets shown to somebody entitled to none of it, which is the
    same distinction list_fleet_patches preserves.
    """
    args, clauses = [], []
    if machine:
        clauses.append("id IN (SELECT run_id FROM patch_run_targets WHERE machine = ?)")
        args.append(_clean(machine, 63))
    if machines is not None:
        hosts = [_clean(m, 63) for m in machines if _clean(m, 63)]
        if not hosts:
            return []
        clauses.append(
            f"id IN (SELECT run_id FROM patch_run_targets "
            f"WHERE machine IN ({','.join('?' * len(hosts))}))")
        args.extend(hosts)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM patch_runs{where} ORDER BY created_at DESC LIMIT ?",
            args + [int(limit)]).fetchall()
        out = []
        for row in rows:
            run = _run_row(row)
            run["counts"] = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM patch_run_targets "
                "WHERE run_id = ? GROUP BY status", (row["id"],))}
            out.append(run)
    return out


def list_run_items(db_path, run_id, machine=None):
    args, where = [run_id], ""
    if machine:
        where = " AND machine = ?"
        args.append(_clean(machine, 63))
    with get_conn(db_path) as conn:
        return [{
            "id": r["id"], "run_id": r["run_id"], "machine": r["machine"],
            "uid": r["uid"], "kb": r["kb"] or "", "title": r["title"] or "",
            "classification": r["classification"] or "", "status": r["status"],
            "error": r["error"] or "", "model": r["model"] or "",
            "os_build": r["os_build"] or "", "attempted_at": r["attempted_at"],
            "resolved_at": r["resolved_at"],
        } for r in conn.execute(
            f"SELECT * FROM patch_run_items WHERE run_id = ?{where} "
            "ORDER BY machine COLLATE NOCASE, title COLLATE NOCASE", args)]


def _refresh_run_status(conn, run_id, now):
    """Roll per-target states up to the run. Cancelled is sticky."""
    row = conn.execute("SELECT status FROM patch_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["status"] == RUN_CANCELLED:
        return
    states = [r["status"] for r in conn.execute(
        "SELECT status FROM patch_run_targets WHERE run_id = ?", (run_id,))]
    if states and all(s in TARGET_TERMINAL for s in states):
        status = RUN_COMPLETE
    elif any(s != TARGET_PENDING for s in states):
        status = RUN_RUNNING
    else:
        status = RUN_SCHEDULED
    if status != row["status"]:
        conn.execute("UPDATE patch_runs SET status = ?, updated_at = ? WHERE id = ?",
                     (status, now, run_id))


def cancel_run(db_path, run_id, actor="system", now=None):
    """Stop a run. Targets that have not been dispatched are cancelled; machines already
    REBOOTING are left alone, because the patches are on them and calling that "cancelled"
    would be a lie about the state of a PC."""
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT status FROM patch_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return 0
        conn.execute("UPDATE patch_runs SET status = ?, updated_at = ? WHERE id = ?",
                     (RUN_CANCELLED, now, run_id))
        cur = conn.execute(
            "UPDATE patch_run_targets SET status = ?, next_attempt_at = NULL, "
            "  last_error = ?, updated_at = ? "
            f"WHERE run_id = ? AND status IN ({','.join('?' * len(TARGET_RECALLABLE))})",
            [TARGET_CANCELLED, f"cancelled by {_clean(actor, 200) or 'system'}", now,
             run_id] + sorted(TARGET_RECALLABLE),
        )
    return cur.rowcount


def retry_failures(db_path, run_id, actor="system", now=None):
    """Re-arm every failed or expired target on a run, resetting its attempt budget.

    Mirrors packages.retry_deployment_failures. `nothing_to_do` is deliberately NOT
    re-armed: there was nothing wrong with that machine, and re-running it would dispatch a
    command whose only possible outcome is the same answer.
    """
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE patch_run_targets SET status = ?, attempts = 0, next_attempt_at = NULL, "
            "  command_id = NULL, staged_at = NULL, last_error = NULL, updated_at = ? "
            "WHERE run_id = ? AND status IN (?, ?, ?)",
            (TARGET_PENDING, now, run_id, TARGET_FAILED, TARGET_EXPIRED, TARGET_CANCELLED),
        )
        if cur.rowcount:
            conn.execute("UPDATE patch_runs SET status = ?, updated_at = ? WHERE id = ?",
                         (RUN_SCHEDULED, now, run_id))
            _refresh_run_status(conn, run_id, now)
    return cur.rowcount


# ================================
# SCHEDULER
# ================================
def _finish_target(db_path, run_id, machine, status, *, error="", now=None):
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE patch_run_targets SET status = ?, next_attempt_at = NULL, "
            "  last_error = ?, updated_at = ? WHERE run_id = ? AND machine = ?",
            (status, _clean(error, MAX_ERROR_CHARS) or None, now, run_id, machine),
        )
        _refresh_run_status(conn, run_id, now)


def _claim_target(db_path, run_id, machine, now):
    """Atomically move one target from `pending` to `in_flight`, spending an attempt.

    Claim-before-queue, for exactly the reason packages._claim_target documents: if the
    process dies between the two, a wasted attempt is recoverable and a machine that
    installs the same patch set twice is a reboot nobody scheduled.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE patch_run_targets SET status = ?, attempts = attempts + 1, "
            "  command_id = NULL, next_attempt_at = NULL, updated_at = ? "
            "WHERE run_id = ? AND machine = ? AND status = ?",
            (TARGET_IN_FLIGHT, now, run_id, machine, TARGET_PENDING),
        )
    return cur.rowcount == 1


def resolve_selection(db_path, run, machine):
    """What this run should install on this machine, right now.

    Resolved at dispatch and never snapshotted -- the module docstring says why. Returns a
    list of patch rows, possibly empty, which the caller turns into `nothing_to_do`.
    """
    if run["selection"] == SELECTION_EXPLICIT:
        return selected_for_machine(db_path, machine, run["selection_uids"])
    return approved_for_machine(db_path, machine)


def _record_items(db_path, run_id, machine, updates, facts, now):
    """Write one `patch_run_items` row per update this attempt is about to try."""
    if not updates:
        return
    model = _clean((facts or {}).get("model"), 200)
    os_build = _clean((facts or {}).get("os_build"), 100)
    with get_conn(db_path) as conn:
        for update in updates:
            conn.execute(
                "INSERT INTO patch_run_items (id, run_id, machine, uid, kb, title, "
                "  classification, status, error, model, os_build, attempted_at, "
                "  resolved_at) VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,NULL) "
                "ON CONFLICT(run_id, machine, uid) DO UPDATE SET "
                "  status = excluded.status, error = NULL, "
                "  attempted_at = excluded.attempted_at, resolved_at = NULL",
                (uuid.uuid4().hex, run_id, machine, update["uid"], update["kb"],
                 update["title"], update["classification"], ITEM_PENDING,
                 model, os_build, now),
            )


def dispatch_once(db_path, now=None, ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS,
                  machine_facts=None):
    """Queue an `install_patches` command for every target that is due.

    Due means: the run is open, its window has started, the target is `pending` with its
    backoff elapsed, and -- unless the run is an emergency -- a maintenance window covering
    that machine is open right now. That last clause is the whole point of windows, and it
    is checked HERE rather than at creation because a window is a recurring condition, not
    a start time.

    `machine_facts` is an optional {machine: {"model":..., "os_build":...}} the caller can
    supply from inventory it already has, so this module needs no opinion about where the
    machine table lives.
    """
    if now is None:
        now = int(time.time())

    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.*, r.window_start, r.window_end, r.max_attempts, r.emergency, "
            "       r.selection, r.selection_json, r.reboot_policy, r.created_by, r.id AS rid "
            "FROM patch_run_targets t JOIN patch_runs r ON r.id = t.run_id "
            f"WHERE t.status = ? AND r.status IN ({','.join('?' * len(RUN_OPEN_STATUSES))}) "
            "ORDER BY t.next_attempt_at ASC",
            [TARGET_PENDING] + list(RUN_OPEN_STATUSES))]

    windows = list_windows(db_path)
    open_now = [w for w in windows if window_is_open(w, now)]

    ready, retire = [], []
    for target in rows:
        if target["window_end"] and target["window_end"] <= now:
            retire.append((target["run_id"], target["machine"], TARGET_EXPIRED,
                           "the run's window closed before this machine ran"))
            continue
        if target["window_start"] and target["window_start"] > now:
            continue
        if target["next_attempt_at"] and target["next_attempt_at"] > now:
            continue
        if target["attempts"] >= target["max_attempts"]:
            retire.append((target["run_id"], target["machine"], TARGET_FAILED,
                           target["last_error"] or "every attempt failed"))
            continue
        if not target["emergency"]:
            if not any(window_covers(w, target["machine"]) for w in open_now):
                continue  # not this machine's hour; try again next tick
        ready.append(target)

    for run_id, machine, status, error in retire:
        _finish_target(db_path, run_id, machine, status, error=error, now=now)

    dispatched = 0
    for target in ready:
        run = get_run(db_path, target["run_id"], with_targets=False)
        if run is None:
            continue
        updates = resolve_selection(db_path, run, target["machine"])
        if not updates:
            # Resolve BEFORE claiming so "nothing to do" does not spend an attempt. A
            # machine that is fully patched should read as done, not as one of three tries.
            _finish_target(db_path, target["run_id"], target["machine"],
                           TARGET_NOTHING_TO_DO, now=now)
            continue
        if not _claim_target(db_path, target["run_id"], target["machine"], now):
            continue

        _record_items(db_path, target["run_id"], target["machine"], updates,
                      (machine_facts or {}).get(target["machine"]), now)
        params = {
            "run_id": target["run_id"],
            "uids": [u["uid"] for u in updates],
            "reboot_policy": run["reboot_policy"],
        }
        command_id = fleet.create_command(
            db_path, machine=target["machine"], command_type=COMMAND_TYPE, params=params,
            issued_by=run["created_by"], ttl_seconds=ttl_seconds,
        )
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE patch_run_targets SET command_id = ?, updated_at = ? "
                "WHERE run_id = ? AND machine = ?",
                (command_id, now, target["run_id"], target["machine"]),
            )
            _refresh_run_status(conn, target["run_id"], now)
        dispatched += 1
    return dispatched


def _terminal_outcome(command):
    """Map a command row onto (finished, succeeded, error_text). Mirrors packages'."""
    if command is None:
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


def reconcile_once(db_path, now=None):
    """Read in-flight attempts back off the command queue, and time out stale reboots.

    Two passes over two different states, and they are here together because they are the
    same idea applied to the two ways a patch attempt can stop being observable.

    An IN_FLIGHT target is waiting on a command result. A successful result does NOT mean
    the patches are installed -- it means the agent staged what it could -- so success moves
    the target to REBOOTING rather than to a terminal state. That is the difference between
    this and packages.reconcile_once, and it is the entire reason this module exists.

    A REBOOTING target is waiting on the machine's own next inventory report, which
    confirm_from_inventory consumes. Nothing here can hurry that; all this does is give up
    after the run's confirm timeout, because a machine that has not come back in a day is
    not going to be confirmed by waiting longer.
    """
    if now is None:
        now = int(time.time())
    fleet.expire_stale_commands(db_path, now)

    with get_conn(db_path) as conn:
        in_flight = [dict(r) for r in conn.execute(
            "SELECT t.*, r.max_attempts, r.retry_backoff_seconds, r.window_end "
            "FROM patch_run_targets t JOIN patch_runs r ON r.id = t.run_id "
            "WHERE t.status = ?", (TARGET_IN_FLIGHT,))]
        stale = [dict(r) for r in conn.execute(
            "SELECT t.*, r.confirm_timeout_seconds "
            "FROM patch_run_targets t JOIN patch_runs r ON r.id = t.run_id "
            "WHERE t.status = ?", (TARGET_REBOOTING,))]

    handled = 0
    for target in in_flight:
        if target["command_id"]:
            finished, succeeded, error = _terminal_outcome(
                fleet.get_command(db_path, target["command_id"]))
        else:
            finished, succeeded, error = (
                True, False, "the attempt was interrupted before the command was queued")
        if not finished:
            continue

        if succeeded:
            # Staged, not done. The machine's next inventory report is the evidence.
            with get_conn(db_path) as conn:
                conn.execute(
                    "UPDATE patch_run_targets SET status = ?, staged_at = ?, "
                    "  last_error = NULL, updated_at = ? WHERE run_id = ? AND machine = ?",
                    (TARGET_REBOOTING, now, now, target["run_id"], target["machine"]))
                _refresh_run_status(conn, target["run_id"], now)
            handled += 1
            continue

        if target["attempts"] >= target["max_attempts"]:
            _finish_target(db_path, target["run_id"], target["machine"], TARGET_FAILED,
                           error=error, now=now)
        elif target["window_end"] and target["window_end"] <= now:
            _finish_target(db_path, target["run_id"], target["machine"], TARGET_EXPIRED,
                           error=error, now=now)
        else:
            backoff = target["retry_backoff_seconds"] * (2 ** max(0, target["attempts"] - 1))
            with get_conn(db_path) as conn:
                conn.execute(
                    "UPDATE patch_run_targets SET status = ?, next_attempt_at = ?, "
                    "  last_error = ?, updated_at = ? WHERE run_id = ? AND machine = ?",
                    (TARGET_PENDING, now + backoff, _clean(error, MAX_ERROR_CHARS), now,
                     target["run_id"], target["machine"]))
                _refresh_run_status(conn, target["run_id"], now)
        handled += 1

    for target in stale:
        staged = target["staged_at"] or target["updated_at"]
        if staged + target["confirm_timeout_seconds"] > now:
            continue
        _mark_items(db_path, target["run_id"], target["machine"], applied=(), now=now,
                    fail_remaining="the machine did not report back after its restart")
        _finish_target(
            db_path, target["run_id"], target["machine"], TARGET_FAILED,
            error="the machine never reported patch inventory after its restart, so the "
                  "install could not be confirmed", now=now)
        handled += 1
    return handled


def _mark_items(db_path, run_id, machine, *, applied=(), now=None, fail_remaining=""):
    """Resolve this target's pending item rows: `applied` succeeded, the rest did not.

    `fail_remaining` supplies the reason for the ones that are left. Called once per target
    when its evidence arrives, so an item row is written twice at most.
    """
    if now is None:
        now = int(time.time())
    done = {normalize_uid(u) for u in applied if u}
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT uid FROM patch_run_items WHERE run_id = ? AND machine = ? AND status = ?",
            (run_id, machine, ITEM_PENDING)).fetchall()
        for row in rows:
            if row["uid"] in done:
                conn.execute(
                    "UPDATE patch_run_items SET status = ?, resolved_at = ? "
                    "WHERE run_id = ? AND machine = ? AND uid = ?",
                    (ITEM_APPLIED, now, run_id, machine, row["uid"]))
            else:
                conn.execute(
                    "UPDATE patch_run_items SET status = ?, error = ?, resolved_at = ? "
                    "WHERE run_id = ? AND machine = ? AND uid = ?",
                    (ITEM_FAILED,
                     _clean(fail_remaining, MAX_ERROR_CHARS)
                     or "the machine still reports this update as available",
                     now, run_id, machine, row["uid"]))


def confirm_from_inventory(db_path, machine, now=None):
    """Close out a machine's staged patch run from the inventory it just reported.

    The completion signal the whole feature is built around, and the reason `install_patches`
    could not be a package: an update that is no longer offered is the only honest evidence
    that it installed. Called from the heartbeat's patch-inventory ingest, immediately AFTER
    `ingest_inventory` has replaced the machine's rows -- the order matters, because this
    reads exactly the absence that ingest just created.

      * every attempted update gone      -> APPLIED
      * some gone, some still offered    -> PARTIAL, with the survivors named on their rows
      * none gone                        -> FAILED

    Note there is no "still rebooting" case to skip, unlike firmware's version check. A
    machine that has not restarted yet reports the same list it did before, which lands here
    as FAILED -- so the caller must only invoke this once the agent has actually rebooted.
    In practice that is what REBOOTING plus a fresh inventory report means, and the confirm
    timeout in reconcile_once catches a machine that never gets there. Returns the number of
    targets closed.
    """
    host = _clean(machine, 63)
    if not host:
        return 0
    if now is None:
        now = int(time.time())

    with get_conn(db_path) as conn:
        targets = [dict(r) for r in conn.execute(
            "SELECT run_id, machine FROM patch_run_targets WHERE machine = ? AND status = ?",
            (host, TARGET_REBOOTING))]
        if not targets:
            return 0
        still_offered = {r["uid"] for r in conn.execute(
            "SELECT uid FROM machine_patches WHERE machine = ?", (host,))}

    closed = 0
    for target in targets:
        with get_conn(db_path) as conn:
            attempted = [r["uid"] for r in conn.execute(
                "SELECT uid FROM patch_run_items WHERE run_id = ? AND machine = ?",
                (target["run_id"], host))]
        if not attempted:
            continue
        applied = [uid for uid in attempted if uid not in still_offered]
        _mark_items(db_path, target["run_id"], host, applied=applied, now=now)
        if len(applied) == len(attempted):
            status, error = TARGET_APPLIED, ""
        elif applied:
            status = TARGET_PARTIAL
            error = (f"{len(attempted) - len(applied)} of {len(attempted)} updates are "
                     f"still offered after the restart")
        else:
            status = TARGET_FAILED
            error = ("the machine still reports every update this run tried to install")
        _finish_target(db_path, target["run_id"], host, status, error=error, now=now)
        closed += 1
    return closed


def tick(db_path, now=None, ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS,
         machine_facts=None):
    """One scheduler pass: reconcile finished attempts, then dispatch due ones.

    Reconcile first, so a target whose backoff has just elapsed can be dispatched in the
    same tick that observed its failure. Returns (reconciled, dispatched).
    """
    reconciled = reconcile_once(db_path, now=now)
    dispatched = dispatch_once(db_path, now=now, ttl_seconds=ttl_seconds,
                               machine_facts=machine_facts)
    return reconciled, dispatched


def pending_target_machines(db_path, now=None):
    """Machines a run is waiting on, for the auto-wake pass -- mirrors packages'."""
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        return sorted({r["machine"] for r in conn.execute(
            "SELECT DISTINCT t.machine FROM patch_run_targets t "
            "JOIN patch_runs r ON r.id = t.run_id "
            f"WHERE t.status = ? AND r.status IN ({','.join('?' * len(RUN_OPEN_STATUSES))}) "
            "AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= ?)",
            [TARGET_PENDING] + list(RUN_OPEN_STATUSES) + [now])})


# ================================
# MACHINE LIFECYCLE
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's rows and roll its runs up again.

    `patch_run_items` is deliberately KEPT. It is the outcome history the stability question
    will be asked of, it names a model and an OS build rather than only a hostname, and a
    machine being decommissioned is not a reason to forget that a patch broke it. Mirrors
    the reasoning `audit` applies to a deleted machine's trail.
    """
    host = _clean(machine, 63)
    if not host:
        return 0
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_patches WHERE machine = ?", (host,))
        run_ids = [r["run_id"] for r in conn.execute(
            "SELECT DISTINCT run_id FROM patch_run_targets WHERE machine = ?", (host,))]
        cur = conn.execute("DELETE FROM patch_run_targets WHERE machine = ?", (host,))
        for run_id in run_ids:
            _refresh_run_status(conn, run_id, now)
    return cur.rowcount


def rename_machine(db_path, old_name, new_name):
    """Follow a machine's rows to its new hostname. Mirrors packages.rename_machine."""
    old = _clean(old_name, 63)
    new = _clean(new_name, 63)
    if not old or not new or old == new:
        return 0
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_patches WHERE machine = ?", (new,))
        conn.execute("UPDATE machine_patches SET machine = ? WHERE machine = ?", (new, old))
        conn.execute("UPDATE patch_run_items SET machine = ? WHERE machine = ?", (new, old))
        cur = conn.execute(
            "UPDATE OR REPLACE patch_run_targets SET machine = ? WHERE machine = ?",
            (new, old))
    return cur.rowcount
