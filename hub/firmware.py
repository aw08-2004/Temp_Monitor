"""Firmware (BIOS) updates -- the `update_bios` half of roadmap #9.

`bios.py` reads and writes firmware *settings*. This module flashes the firmware itself:
an operator uploads a vendor's BIOS image once, describes what hardware it belongs to, and
aims it at machines, optionally inside a maintenance window.

Four things make this different from a package deploy, and each one is why this is a
separate module rather than a `packages.py` source kind:

  * **The reboot is part of the operation, not a side effect.** The flash completes during
    POST, long after the command that started it has been answered. So the command result
    cannot be the answer: a target moves to REBOOTING when the agent says it staged the
    image, and only the machine's own next BIOS-version report can close it out. That is
    `confirm_from_inventory`, called from the heartbeat ingest path -- the same signal the
    settings half uses for verification, for the same reason (see bios.classify_result).
    Folding this into `packages.reconcile_once` would have meant teaching one scheduler two
    completion models, and the one it already has -- exit code plus detection -- is exactly
    the guess this feature must not make.

  * **There is no restore path.** A failed install is re-run; a failed flash can brick the
    machine. So a target gets **one attempt, never a retry**: `packages` retries a target
    three times with backoff, which is right for an installer and is the worst possible
    behaviour for a flash whose outcome is unknown. Re-flashing after an unknown outcome is
    a decision for a human who has looked at the machine, not for a scheduler at 3am.

  * **Preconditions are refusals, not warnings.** A payload names its vendor and the exact
    models it belongs to, and a machine whose reported model is not on that list is refused
    -- named, on its own row, before anything is dispatched. Vendor flash tools are
    perfectly willing to write an image meant for another board.

  * **A refused target is a first-class outcome.** Aiming a Latitude image at a mixed fleet
    refuses the machines it does not fit and proceeds with the ones it does, rather than
    failing the whole job -- with the reason on each refused row. Same shape as
    `plan_restore` naming a selection that matched nothing: silently dropping a target is
    how somebody believes forty machines were updated and thirty were not.

**Payload blobs are content-addressed and stored under their own root**, `<log dir>/firmware`,
using `packages.store_blob` -- the same tested primitive, a different directory. Sharing
`packages`' root would mean the two features' refcounting had to agree about a file neither
one owns, and a package delete could unlink a BIOS image. The sha256 is computed by the hub
as the bytes are written, never accepted from a client, and the agent re-verifies it before
it flashes anything.

**The image URL and the BIOS setup password never travel in command params.**
`fleet.create_command` audits params verbatim. The command carries an opaque `update_id`;
the agent fetches everything else from an authenticated endpoint, and that fetch is a
conditional UPDATE, so a redelivered command cannot flash twice. This is the restore-plan
and `set_bios_settings` precedent, for the same reasons.

Authorization lives entirely upstream at `manage_firmware` plus machine scope (bios_web.py).
Nothing here checks a session, exactly like fleet.py and packages.py.

Kept free of Flask so it can be unit-tested in isolation.
"""
import json
import os
import sqlite3
import time
import uuid

import bios
import fleet
import packages

# ================================
# VOCABULARY
# ================================
#: The command type dispatched to the agent. Already in fleet.ALL_COMMANDS -- it has been
#: accepted, queued and routed to a StubExecutor since the command-signing removal; this
#: module is what finally puts something behind it.
COMMAND_TYPE = "update_bios"

# ---------------------------------------------------------------- job lifecycle
JOB_SCHEDULED = "scheduled"    # nothing attempted yet (a future window, or a tick away)
JOB_RUNNING = "running"        # at least one target attempted, at least one unresolved
JOB_COMPLETE = "complete"      # every target terminal
JOB_CANCELLED = "cancelled"
JOB_STATUSES = (JOB_SCHEDULED, JOB_RUNNING, JOB_COMPLETE, JOB_CANCELLED)
JOB_OPEN_STATUSES = (JOB_SCHEDULED, JOB_RUNNING)

# ---------------------------------------------------------------- target lifecycle
#: Waiting for its window, or for the next dispatch pass.
TARGET_PENDING = "pending"
#: An `update_bios` command is queued. The agent has not answered.
TARGET_IN_FLIGHT = "in_flight"
#: The agent fetched the payload. From here on the firmware may already have been touched,
#: which is why this state cannot be cancelled.
TARGET_FLASHING = "flashing"
#: The agent staged or applied the image and said so. The flash itself completes during POST,
#: so nothing is known yet -- this state exists precisely to stop the command result being
#: mistaken for the answer.
TARGET_REBOOTING = "rebooting"
#: The machine came back reporting the version the payload was pinned to. The only success.
TARGET_APPLIED = "applied"
#: The agent reported a failure, the machine came back on its old version, or confirmation
#: timed out.
TARGET_FAILED = "failed"
#: The machine came back on a version that is neither the old one nor the expected one. Its
#: own outcome rather than a guess in either direction -- exactly bios.OUTCOME_UNKNOWN's
#: reasoning: a vendor may ship an image whose reported string differs from the one the
#: operator typed, and calling that "applied" would confirm a flash nobody verified.
TARGET_UNKNOWN = "unknown"
#: This machine's hardware does not match the payload, or it has no flashable interface.
#: Never dispatched. Distinct from `failed` for the same reason bios keeps `unsupported`
#: distinct from `error`: one is a fact about the hardware, the other is something going wrong.
TARGET_REFUSED = "refused"
#: The window closed before this machine was reachable.
TARGET_EXPIRED = "expired"
TARGET_CANCELLED = "cancelled"

TARGET_STATUSES = (TARGET_PENDING, TARGET_IN_FLIGHT, TARGET_FLASHING, TARGET_REBOOTING,
                   TARGET_APPLIED, TARGET_FAILED, TARGET_UNKNOWN, TARGET_REFUSED,
                   TARGET_EXPIRED, TARGET_CANCELLED)
TARGET_TERMINAL = frozenset({TARGET_APPLIED, TARGET_FAILED, TARGET_UNKNOWN, TARGET_REFUSED,
                             TARGET_EXPIRED, TARGET_CANCELLED})
#: States in which the machine has NOT yet been handed the image, so a cancel is honest.
TARGET_RECALLABLE = frozenset({TARGET_PENDING, TARGET_IN_FLIGHT})

MAX_NAME_CHARS = 120
MAX_VERSION_CHARS = 80
MAX_MODELS = 64
MAX_ERROR_CHARS = 1000
MAX_ARGS_CHARS = 500
#: Vendor flash payloads are 10-60 MB. The cap is a guard against a mis-picked file, not a
#: policy; `firmware.max_upload_mb` is the real knob.
MAX_TARGETS_PER_JOB = 500

#: How long a staged flash may sit unconfirmed before it is closed out. Deliberately generous
#: -- a machine flashed at 18:00 is confirmed when somebody switches it on the next morning,
#: and a scheduler that gave up after an hour would report a successful flash as a failure.
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 24 * 3600
#: How long an agent may hold a fetched payload before we stop waiting for its report. This is
#: the flash itself plus the reboot; a vendor tool that hangs is the case being caught.
DEFAULT_FLASHING_TIMEOUT_SECONDS = 2 * 3600


class PayloadRejected(ValueError):
    """A payload definition or a job the hub refuses to accept. Its own type so the web layer
    answers 400 while a genuine bug still becomes a 500. Mirrors bios.ChangeRejected."""


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_firmware_db(db_path):
    """Create the firmware tables if absent. Idempotent -- called on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # The image plus everything needed to decide whether it belongs on a given machine.
        # `models_json` is a list of exact model strings rather than a pattern: a glob over
        # model names is one typo away from flashing a Latitude image onto a Precision, and
        # the operator has the vendor's compatibility list in front of them either way.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS firmware_payloads (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                vendor          TEXT NOT NULL,
                models_json     TEXT NOT NULL DEFAULT '[]',
                to_version      TEXT NOT NULL,
                sha256          TEXT NOT NULL,
                size_bytes      INTEGER NOT NULL DEFAULT 0,
                filename        TEXT NOT NULL DEFAULT '',
                install_args    TEXT NOT NULL DEFAULT '',
                notes           TEXT NOT NULL DEFAULT '',
                created_at      INTEGER NOT NULL,
                created_by      TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # One job = one payload aimed at a set of machines, with an optional window. The
        # window fields and the claim-then-queue discipline are lifted from packages.py
        # deliberately: an operator scheduling a BIOS flash for Saturday night should not
        # meet a second, subtly different notion of "maintenance window".
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS firmware_jobs (
                id            TEXT PRIMARY KEY,
                payload_id    TEXT NOT NULL,
                note          TEXT,
                status        TEXT NOT NULL,
                window_start  INTEGER,
                window_end    INTEGER,
                created_at    INTEGER NOT NULL,
                created_by    TEXT NOT NULL DEFAULT '',
                updated_at    INTEGER NOT NULL
            )
            """
        )
        # A target carries its own opaque id because that id is what the command params hold
        # and what the agent authenticates against -- a (job, machine) pair in params would
        # let a machine name someone else's row by guessing a hostname.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS firmware_targets (
                id               TEXT PRIMARY KEY,
                job_id           TEXT NOT NULL,
                machine          TEXT NOT NULL,
                status           TEXT NOT NULL,
                command_id       TEXT,
                from_version     TEXT NOT NULL DEFAULT '',
                observed_version TEXT NOT NULL DEFAULT '',
                dispatched_at    INTEGER,
                flashed_at       INTEGER,
                finished_at      INTEGER,
                error            TEXT NOT NULL DEFAULT '',
                updated_at       INTEGER NOT NULL,
                UNIQUE(job_id, machine)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_firmware_targets_machine "
                     "ON firmware_targets(machine, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_firmware_targets_job "
                     "ON firmware_targets(job_id)")


# ================================
# BLOB STORE
# ================================
def blob_root(log_dir):
    """Where firmware images live: `<log dir>/firmware`, beside the database and beside
    `packages`' own root rather than inside it.

    Its own directory because refcounting is per-feature: `packages.delete_blob_if_orphaned`
    asks `package_sources` whether anything still points at a digest, and it would answer
    "no" about a BIOS image it has never heard of. Two features sharing one root means one
    of them eventually deletes the other's bytes.
    """
    return os.path.join(log_dir, "firmware")


def payload_for_blob(db_path, sha256):
    """The payload id holding this digest, or None. This is what keeps the agent download
    endpoint from being a general-purpose read primitive over the firmware directory."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT id FROM firmware_payloads WHERE sha256 = ? LIMIT 1",
                           (str(sha256 or "").lower(),)).fetchone()
    return row["id"] if row else None


# ================================
# HELPERS
# ================================
def _clean(value, limit=None):
    text = str(value if value is not None else "").strip()
    return text[:limit] if limit else text


def _same(left, right):
    """Vendor strings compared the way they actually arrive: trimmed and casefolded.

    A BIOS version reported as `1.21.0` one way and `1.21.0 ` another, or a model as
    `Latitude 5540` against `latitude 5540`, is the normal case rather than a mismatch.
    Being strict here would refuse a correct machine or, worse, report a successful flash as
    UNKNOWN and send somebody to look at hardware that is fine.
    """
    return _clean(left).casefold() == _clean(right).casefold()


def _epoch_or_none(value, field):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise PayloadRejected(f"{field} must be a unix timestamp")


def _row(row):
    return dict(row) if row is not None else None


def _payload_row(row):
    if row is None:
        return None
    payload = dict(row)
    try:
        models = json.loads(payload.pop("models_json", "[]"))
    except (TypeError, ValueError):
        models = []
    payload["models"] = [m for m in models if isinstance(m, str)]
    return payload


# ================================
# PAYLOADS
# ================================
def validate_payload(*, name, vendor, models, to_version, sha256, install_args=""):
    """Check a payload definition and return it normalised. Raises PayloadRejected.

    Every field here exists to make a later refusal possible: without `vendor` and `models`
    there is nothing to match a machine against, and without `to_version` there is no way to
    tell an applied flash from a failed one -- the completion signal IS the version.
    """
    name = _clean(name, MAX_NAME_CHARS)
    if not name:
        raise PayloadRejected("A firmware payload needs a name.")

    vendor = _clean(vendor, MAX_NAME_CHARS)
    if not vendor:
        raise PayloadRejected("A firmware payload must name the manufacturer it belongs to.")

    cleaned_models = []
    seen = set()
    for raw in list(models or [])[:MAX_MODELS]:
        model = _clean(raw, MAX_NAME_CHARS)
        if model and model.casefold() not in seen:
            seen.add(model.casefold())
            cleaned_models.append(model)
    if not cleaned_models:
        # Deliberately not optional, and not defaulted to "any model". An image that matches
        # every machine is one refusal away from being written to a board it was not built
        # for, and that is the failure with no restore path.
        raise PayloadRejected("A firmware payload must list at least one model it applies "
                              "to. This is what stops it being flashed onto other hardware.")

    to_version = _clean(to_version, MAX_VERSION_CHARS)
    if not to_version:
        raise PayloadRejected("A firmware payload must state the BIOS version it installs. "
                              "That version is how a flash is confirmed -- without it, a "
                              "machine coming back cannot be told from one that failed.")

    try:
        digest = packages.normalize_sha256(sha256)
    except ValueError as e:
        raise PayloadRejected(str(e))
    if not digest:
        raise PayloadRejected("A firmware payload needs an uploaded image.")

    return {
        "name": name,
        "vendor": vendor,
        "models": cleaned_models,
        "to_version": to_version,
        "sha256": digest,
        "install_args": _clean(install_args, MAX_ARGS_CHARS),
    }


def create_payload(db_path, *, name, vendor, models, to_version, sha256, size_bytes=0,
                   filename="", install_args="", notes="", created_by="system"):
    fields = validate_payload(name=name, vendor=vendor, models=models,
                              to_version=to_version, sha256=sha256,
                              install_args=install_args)
    payload_id = uuid.uuid4().hex
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO firmware_payloads(id, name, vendor, models_json, to_version, "
            "sha256, size_bytes, filename, install_args, notes, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (payload_id, fields["name"], fields["vendor"], json.dumps(fields["models"]),
             fields["to_version"], fields["sha256"], int(size_bytes or 0),
             _clean(filename, MAX_NAME_CHARS), fields["install_args"],
             _clean(notes, 500), now, str(created_by)),
        )
    fleet.audit(db_path, actor=created_by, action="create_firmware_payload",
                level=fleet.LEVEL_SECURITY, target=fields["name"],
                detail={"payload_id": payload_id, "vendor": fields["vendor"],
                        "models": fields["models"], "to_version": fields["to_version"],
                        "sha256": fields["sha256"], "bytes": int(size_bytes or 0)})
    return payload_id


def list_payloads(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM firmware_payloads ORDER BY vendor COLLATE NOCASE, "
            "name COLLATE NOCASE"
        ).fetchall()
    return [_payload_row(row) for row in rows]


def get_payload(db_path, payload_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM firmware_payloads WHERE id = ?",
                           (str(payload_id),)).fetchone()
    return _payload_row(row)


def delete_payload(db_path, payload_id, *, actor="system", blob_root_dir=None):
    """Remove a payload definition, and its image when nothing else points at it.

    Refused while a job is still open against it: the agent fetches the URL and digest at
    claim time, so deleting the payload out from under an in-flight flash would hand a
    machine a 404 in the middle of the one operation with no restore path.
    """
    payload = get_payload(db_path, payload_id)
    if payload is None:
        return False
    with get_conn(db_path) as conn:
        open_jobs = conn.execute(
            "SELECT COUNT(*) AS n FROM firmware_jobs WHERE payload_id = ? AND status IN "
            "(?, ?)", (payload_id, JOB_SCHEDULED, JOB_RUNNING)).fetchone()["n"]
        if open_jobs:
            raise PayloadRejected("That image is being used by a firmware update that has "
                                  "not finished. Cancel it first.")
        conn.execute("DELETE FROM firmware_payloads WHERE id = ?", (payload_id,))
        still_used = conn.execute(
            "SELECT COUNT(*) AS n FROM firmware_payloads WHERE sha256 = ?",
            (payload["sha256"],)).fetchone()["n"]

    if blob_root_dir and not still_used:
        try:
            os.remove(packages.blob_path(blob_root_dir, payload["sha256"]))
        except OSError:
            # A missing or unremovable file is not worth failing the delete over: the row is
            # gone, which is what the operator asked for, and an orphan blob costs disk.
            pass
    fleet.audit(db_path, actor=actor, action="delete_firmware_payload",
                level=fleet.LEVEL_SECURITY, target=payload["name"],
                detail={"payload_id": payload_id, "sha256": payload["sha256"]})
    return True


# ================================
# PRECONDITIONS
# ================================
def check_machine(payload, machine_info, inventory):
    """Decide whether this payload may be flashed onto this machine.

    Returns None if it may, or a human-readable refusal reason if it may not. A reason
    rather than a boolean because the reason IS the feature: "PC-14 reports model
    'OptiPlex 7010', which this image does not list" is something an operator can act on,
    and "refused" alone is not.

    Checked hub-side so a mismatch never reaches a machine at all, and re-checked agent-side
    against what the hardware says about itself at flash time -- the inventory here can be
    hours old, and a machine can be re-seated into a different chassis between the two.
    """
    machine_info = machine_info or {}
    inventory = inventory or {}

    manufacturer = _clean(machine_info.get("manufacturer"))
    if not manufacturer:
        # Null means no agent has told us, which is not the same as a mismatch -- but it is
        # not a match either, and this is the one feature where proceeding on an unknown is
        # unacceptable. See bios.get_inventory on the same distinction.
        return ("This machine has not reported a manufacturer, so it cannot be matched "
                "against the image. Update its agent, or wait for its next check-in.")
    if not _same(manufacturer, payload["vendor"]):
        return (f"This machine reports manufacturer {manufacturer!r}, and the image is for "
                f"{payload['vendor']!r}.")

    model = _clean(machine_info.get("model"))
    if not model:
        return ("This machine has not reported a model, so it cannot be matched against "
                "the image.")
    if not any(_same(model, candidate) for candidate in payload["models"]):
        return (f"This machine reports model {model!r}, which is not one of the models this "
                f"image lists ({', '.join(payload['models'])}).")

    support = inventory.get("support")
    if support == "unsupported":
        return ("This machine has no manageable firmware interface, so the hub cannot flash "
                "it.")

    current = _clean(inventory.get("bios_version"))
    if current and _same(current, payload["to_version"]):
        # A no-op flash is refused for the same reason bios.validate_changes refuses a no-op
        # write: the completion signal is the version, so a flash from X to X can never be
        # confirmed and would sit REBOOTING until it timed out as a failure.
        return f"This machine is already on BIOS version {current}."
    return None


def read_machine_facts(db_path, machines):
    """Collect what `check_machine` needs for each name: `{machine: (info, inventory)}`.

    Reads `machine_info` directly, the way permissions.py and directory.py already do --
    there is no shared accessor, and the alternative is the web layer growing SQL of its
    own. A machine with no row is absent from the result, which `create_job` turns into a
    refusal rather than an error: a hostname typed into the picker before that PC ever
    enrolled is a normal thing to have happen.
    """
    facts = {}
    names = [_clean(m) for m in (machines or []) if _clean(m)]
    if not names:
        return facts
    with get_conn(db_path) as conn:
        placeholders = ",".join("?" for _ in names)
        rows = conn.execute(
            f"SELECT machine, manufacturer, model FROM machine_info "
            f"WHERE machine IN ({placeholders})", names).fetchall()
    for row in rows:
        facts[row["machine"]] = (dict(row), bios.get_inventory(db_path, row["machine"]))
    return facts


# ================================
# JOBS
# ================================
def create_job(db_path, *, payload_id, machines, created_by, note=None, window_start=None,
               window_end=None, machine_facts=None):
    """Aim a payload at a set of machines. Returns (job_id, targets).

    `machine_facts` is `{machine: (machine_info, bios_inventory)}` -- supplied by the caller
    rather than read here, so this module stays free of the machine tables and the web layer
    keeps ownership of scope filtering. Machines it does not cover are refused as unknown.

    Nothing is dispatched here; the scheduler tick owns dispatch, so an immediate job and a
    windowed one travel one code path. Two mechanisms for "send it" is how the immediate case
    grows a bug the scheduled case does not have -- packages.py's reasoning, unchanged.

    A machine failing its preconditions is recorded as a REFUSED target with its reason, not
    dropped and not fatal to the job. Refusing the whole job because three machines out of
    forty are the wrong model would make this unusable on the mixed fleets it is for.
    """
    payload = get_payload(db_path, payload_id)
    if payload is None:
        raise PayloadRejected("unknown firmware payload")

    names = []
    seen = set()
    for raw in machines or []:
        name = _clean(raw)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    if not names:
        raise PayloadRejected("A firmware update needs at least one target machine.")
    if len(names) > MAX_TARGETS_PER_JOB:
        raise PayloadRejected(f"At most {MAX_TARGETS_PER_JOB} machines can be updated in one "
                              f"job.")

    window_start = _epoch_or_none(window_start, "window_start")
    window_end = _epoch_or_none(window_end, "window_end")
    now = int(time.time())
    if window_start and window_end and window_end <= window_start:
        raise PayloadRejected("The maintenance window must end after it starts.")
    if window_end and window_end <= now:
        raise PayloadRejected("That maintenance window has already closed.")

    machine_facts = machine_facts or {}
    job_id = uuid.uuid4().hex
    targets = []
    for name in names:
        info, inventory = machine_facts.get(name, (None, None))
        if info is None:
            reason = "There is no such machine, or you cannot see it."
        else:
            reason = check_machine(payload, info, inventory)
        targets.append({
            "id": uuid.uuid4().hex,
            "machine": name,
            "status": TARGET_REFUSED if reason else TARGET_PENDING,
            "error": _clean(reason, MAX_ERROR_CHARS),
            "from_version": _clean((inventory or {}).get("bios_version"),
                                   MAX_VERSION_CHARS),
        })

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO firmware_jobs(id, payload_id, note, status, window_start, "
            "window_end, created_at, created_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (job_id, payload_id, _clean(note, 500) or None, JOB_SCHEDULED, window_start,
             window_end, now, str(created_by), now),
        )
        conn.executemany(
            "INSERT INTO firmware_targets(id, job_id, machine, status, from_version, "
            "error, finished_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(t["id"], job_id, t["machine"], t["status"], t["from_version"], t["error"],
              now if t["status"] == TARGET_REFUSED else None, now) for t in targets],
        )
        _refresh_job_status(conn, job_id, now)

    fleet.audit(db_path, actor=created_by, action="create_firmware_job",
                level=fleet.LEVEL_SECURITY, target=payload["name"],
                detail={"job_id": job_id, "payload_id": payload_id,
                        "to_version": payload["to_version"],
                        "machines": [t["machine"] for t in targets
                                     if t["status"] == TARGET_PENDING],
                        "refused": {t["machine"]: t["error"] for t in targets
                                    if t["status"] == TARGET_REFUSED},
                        "window_start": window_start, "window_end": window_end})
    return job_id, targets


def _refresh_job_status(conn, job_id, now):
    """Roll a job's own status up from its targets. Called inside an open transaction."""
    row = conn.execute("SELECT status FROM firmware_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["status"] == JOB_CANCELLED:
        return
    counts = {}
    for tally in conn.execute(
            "SELECT status, COUNT(*) AS n FROM firmware_targets WHERE job_id = ? "
            "GROUP BY status", (job_id,)).fetchall():
        counts[tally["status"]] = tally["n"]
    unresolved = sum(n for status, n in counts.items() if status not in TARGET_TERMINAL)
    if not unresolved:
        status = JOB_COMPLETE
    elif counts.get(TARGET_PENDING, 0) == unresolved:
        status = JOB_SCHEDULED
    else:
        status = JOB_RUNNING
    conn.execute("UPDATE firmware_jobs SET status = ?, updated_at = ? WHERE id = ?",
                 (status, now, job_id))


def list_jobs(db_path, limit=100, machine=None):
    """Recent jobs with a per-status target tally, newest first. The tally is computed in
    SQL: a fleet-wide job has one row per machine and the list only needs the counts."""
    sql = ("SELECT j.*, p.name AS payload_name, p.vendor AS payload_vendor, "
           "       p.to_version AS payload_version "
           "FROM firmware_jobs j LEFT JOIN firmware_payloads p ON p.id = j.payload_id")
    params = []
    if machine:
        sql += (" WHERE EXISTS (SELECT 1 FROM firmware_targets t WHERE t.job_id = j.id "
                "AND t.machine = ?)")
        params.append(_clean(machine))
    sql += " ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?"
    params.append(int(limit))

    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        ids = [row["id"] for row in rows]
        counts = {}
        if ids:
            placeholders = ",".join("?" for _ in ids)
            for tally in conn.execute(
                    f"SELECT job_id, status, COUNT(*) AS n FROM firmware_targets "
                    f"WHERE job_id IN ({placeholders}) GROUP BY job_id, status",
                    ids).fetchall():
                counts.setdefault(tally["job_id"], {})[tally["status"]] = tally["n"]

    jobs = []
    for row in rows:
        job = dict(row)
        job["target_counts"] = counts.get(job["id"], {})
        job["target_total"] = sum(job["target_counts"].values())
        jobs.append(job)
    return jobs


def get_job(db_path, job_id, *, with_targets=True):
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT j.*, p.name AS payload_name, p.vendor AS payload_vendor, "
            "       p.to_version AS payload_version, p.sha256 AS payload_sha256 "
            "FROM firmware_jobs j LEFT JOIN firmware_payloads p ON p.id = j.payload_id "
            "WHERE j.id = ?", (str(job_id),)).fetchone()
        if row is None:
            return None
        job = dict(row)
        if with_targets:
            job["targets"] = [dict(t) for t in conn.execute(
                "SELECT * FROM firmware_targets WHERE job_id = ? "
                "ORDER BY machine COLLATE NOCASE", (job_id,)).fetchall()]
    return job


def get_target(db_path, target_id):
    """One target joined to everything the agent needs. The payload fields ride along
    because the agent endpoint answers from exactly this row."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT t.*, j.payload_id, j.status AS job_status, p.name AS payload_name, "
            "       p.vendor AS payload_vendor, p.models_json, p.to_version, p.sha256, "
            "       p.size_bytes, p.filename, p.install_args "
            "FROM firmware_targets t JOIN firmware_jobs j ON j.id = t.job_id "
            "LEFT JOIN firmware_payloads p ON p.id = j.payload_id WHERE t.id = ?",
            (str(target_id),)).fetchone()
    if row is None:
        return None
    target = dict(row)
    try:
        models = json.loads(target.pop("models_json", "[]") or "[]")
    except (TypeError, ValueError):
        models = []
    target["models"] = [m for m in models if isinstance(m, str)]
    return target


def _finish_target(db_path, target_id, status, *, error="", observed_version=None):
    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT job_id FROM firmware_targets WHERE id = ?",
                           (target_id,)).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE firmware_targets SET status = ?, error = ?, finished_at = ?, "
            "observed_version = COALESCE(?, observed_version), updated_at = ? WHERE id = ?",
            (status, _clean(error, MAX_ERROR_CHARS), now,
             None if observed_version is None else _clean(observed_version,
                                                          MAX_VERSION_CHARS),
             now, target_id))
        _refresh_job_status(conn, row["job_id"], now)
    return True


# ================================
# DISPATCH
# ================================
def _claim_target(db_path, target_id, now):
    """Move one target from PENDING to IN_FLIGHT, atomically.

    Claim before queueing, exactly as packages._claim_target does and for a sharper version
    of the same reason: the other order leaves a queued `update_bios` command whose target is
    still pending, and the next tick would flash the machine a second time.
    """
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE firmware_targets SET status = ?, dispatched_at = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (TARGET_IN_FLIGHT, now, now, target_id, TARGET_PENDING))
    return (cursor.rowcount or 0) == 1


def dispatch_once(db_path, now=None, ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS,
                  online_machines=None):
    """Queue an `update_bios` command for every target whose window is open.

    `online_machines` -- a set of machine names, or None to skip the check -- is the
    catch-up discipline the file-backup scheduler needed: dispatching into the void burns a
    command TTL and, here, would retire the target as failed on a machine that was merely
    switched off. A target left PENDING is still due when the machine reappears.
    """
    if now is None:
        now = int(time.time())

    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.*, j.window_start, j.window_end, j.created_by, j.payload_id "
            "FROM firmware_targets t JOIN firmware_jobs j ON j.id = t.job_id "
            "WHERE t.status = ? AND j.status IN (?, ?) ORDER BY t.updated_at ASC",
            (TARGET_PENDING, JOB_SCHEDULED, JOB_RUNNING))]

    dispatched = 0
    for target in rows:
        if target["window_end"] and target["window_end"] <= now:
            _finish_target(db_path, target["id"], TARGET_EXPIRED,
                           error="the maintenance window closed before this machine was "
                                 "reachable")
            continue
        if target["window_start"] and target["window_start"] > now:
            continue
        if online_machines is not None and target["machine"] not in online_machines:
            continue

        payload = get_payload(db_path, target["payload_id"])
        if payload is None:
            _finish_target(db_path, target["id"], TARGET_FAILED,
                           error="the firmware image was deleted before this machine ran")
            continue

        if not _claim_target(db_path, target["id"], now):
            continue  # someone else took it
        try:
            command_id = fleet.create_command(
                db_path, machine=target["machine"], command_type=COMMAND_TYPE,
                # An opaque id and nothing else. The image URL, its digest and the BIOS
                # setup password are fetched from an authenticated endpoint, because
                # create_command audits params verbatim.
                params={"update_id": target["id"]},
                issued_by=target["created_by"] or "system", ttl_seconds=ttl_seconds)
        except ValueError as e:
            _finish_target(db_path, target["id"], TARGET_FAILED, error=str(e))
            continue
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE firmware_targets SET command_id = ?, updated_at = ? WHERE id = ?",
                (command_id, now, target["id"]))
            _refresh_job_status(conn, target["job_id"], now)
        dispatched += 1
    return dispatched


def start_target(db_path, target_id):
    """Hand the payload over, once. Conditional on the target still being IN_FLIGHT, so two
    polls delivering the same command cannot flash twice -- and so cancel stops being
    possible at exactly the moment the machine could have started writing. One UPDATE gives
    both properties, the same way bios.start_change does."""
    now = int(time.time())
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE firmware_targets SET status = ?, flashed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (TARGET_FLASHING, now, now, target_id, TARGET_IN_FLIGHT))
        if (cursor.rowcount or 0) != 1:
            return False
        row = conn.execute("SELECT job_id FROM firmware_targets WHERE id = ?",
                           (target_id,)).fetchone()
        _refresh_job_status(conn, row["job_id"], now)
    return True


# ================================
# RESULTS AND CONFIRMATION
# ================================
def ingest_result(db_path, target_id, payload):
    """Record what the agent said about the flash it ran.

    **Success here is not success.** A vendor tool that returns 0 has staged an image the
    firmware writes during POST; whether it took is decided by `confirm_from_inventory` when
    the machine comes back. So a good report moves the target to REBOOTING, which is not a
    terminal state, and the console must not render it as done. Only a *failure* -- or a
    machine saying it cannot flash at all -- is terminal here, because those are the two
    answers the agent genuinely knows.

    Written from a machine's report, so trimmed and non-raising on shape, like every other
    ingest path in this codebase.
    """
    target = get_target(db_path, target_id)
    if target is None:
        return None
    if target["status"] not in (TARGET_IN_FLIGHT, TARGET_FLASHING):
        # A late or duplicated report for something already resolved. Dropped rather than
        # reopening a terminal row -- and on this feature "reopening" could mean re-flashing.
        return target

    payload = payload if isinstance(payload, dict) else {}
    error = _clean(payload.get("error"), MAX_ERROR_CHARS)
    if payload.get("unsupported"):
        _finish_target(db_path, target_id, TARGET_REFUSED,
                       error=error or "this machine has no flashable firmware interface")
    elif payload.get("ok"):
        now = int(time.time())
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE firmware_targets SET status = ?, flashed_at = COALESCE(flashed_at, "
                "?), error = '', updated_at = ? WHERE id = ?",
                (TARGET_REBOOTING, now, now, target_id))
            _refresh_job_status(conn, target["job_id"], now)
    else:
        _finish_target(db_path, target_id, TARGET_FAILED,
                       error=error or "the machine reported no result")
    return get_target(db_path, target_id)


def confirm_from_inventory(db_path, machine, bios_version):
    """Close out a machine's staged flash from the BIOS version it now reports.

    This is the completion signal the whole feature is built around, and the reason
    `update_bios` could not be a package: the flash happens during POST, so the only
    honest evidence that it worked is the machine coming back and saying what it is now
    running. Called from the heartbeat's BIOS-inventory ingest, so it costs nothing on a
    fleet with no flash in flight.

      * reports the payload's version   -> APPLIED
      * reports the version it had      -> still pending its reboot; left alone, because a
                                           machine that has not restarted yet is the normal
                                           case for hours. `expire_stale` is what eventually
                                           calls that a failure.
      * reports something else entirely -> UNKNOWN, its own outcome. The image may have
                                           installed under a different version string, or a
                                           different image may have been applied by hand.
                                           Confirming a flash nobody verified is the one
                                           thing this must not do.

    Returns the number of targets closed.
    """
    version = _clean(bios_version, MAX_VERSION_CHARS)
    if not machine or not version:
        return 0
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.id, t.from_version, p.to_version FROM firmware_targets t "
            "JOIN firmware_jobs j ON j.id = t.job_id "
            "LEFT JOIN firmware_payloads p ON p.id = j.payload_id "
            "WHERE t.machine = ? AND t.status = ?", (_clean(machine), TARGET_REBOOTING))]

    closed = 0
    for row in rows:
        if _same(version, row["to_version"]):
            _finish_target(db_path, row["id"], TARGET_APPLIED, observed_version=version)
            closed += 1
        elif _same(version, row["from_version"]):
            continue  # not back from its reboot yet
        else:
            _finish_target(
                db_path, row["id"], TARGET_UNKNOWN, observed_version=version,
                error=f"the machine came back reporting BIOS version {version!r}, which is "
                      f"neither the version it had nor the one this image installs")
            closed += 1
    return closed


def reconcile_once(db_path):
    """Retire IN_FLIGHT targets whose command reached a terminal state without a result.

    An `update_bios` command is dispatched to a machine that was online a moment ago, and
    then anything can happen: it sleeps, it is unplugged, its agent dies before fetching the
    image. The command queue notices -- `fleet.expire_stale_commands` expires it on the
    housekeeping heartbeat -- but nothing was telling the TARGET, so it sat `in_flight`
    forever and its job stayed at 39/40 with no way to reach 40. The timeouts below do not
    catch it either: both of them start counting from a fetch that never happened.

    Everything here is FAILED rather than retried, and says which of the three it was. This
    feature has no retry by design (see the module docstring), so the honest outcome is "it
    did not happen, and here is why" -- which an operator can re-issue in one click, having
    looked at the machine first.
    """
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.id, c.status AS command_status, r.output AS output "
            "FROM firmware_targets t JOIN commands c ON c.id = t.command_id "
            "LEFT JOIN command_results r ON r.command_id = c.id "
            "WHERE t.status = ? AND c.status IN (?, ?, ?)",
            (TARGET_IN_FLIGHT, fleet.STATUS_EXPIRED, fleet.STATUS_FAILED,
             fleet.STATUS_DONE))]

    for target in rows:
        if target["command_status"] == fleet.STATUS_EXPIRED:
            message = ("the machine never picked the update up before the command expired, "
                       "so nothing was flashed")
        elif target["command_status"] == fleet.STATUS_DONE:
            # The agent answers the command only after posting its result, so this means it
            # finished without ever fetching the image -- an agent too old to implement
            # `update_bios`, most likely, which is worth saying rather than calling a
            # generic failure.
            message = ("the machine finished the command without ever fetching the image. "
                       "Its agent may be too old to install firmware.")
        else:
            message = _clean(target["output"], MAX_ERROR_CHARS) or \
                "the machine reported the update failed"
        _finish_target(db_path, target["id"], TARGET_FAILED, error=message)
    return len(rows)


def expire_stale(db_path, now=None, flashing_timeout=DEFAULT_FLASHING_TIMEOUT_SECONDS,
                 confirm_timeout=DEFAULT_CONFIRM_TIMEOUT_SECONDS):
    """Close out targets nobody is going to hear from again.

    Two separate clocks, because they mean different things:

      * FLASHING past `flashing_timeout` -- the agent fetched the image and never reported.
        FAILED, and the message says the firmware may have been touched, because it may
        have been: this is the state in which the machine was actually running a vendor
        flash tool.
      * REBOOTING past `confirm_timeout` -- the flash was staged and the machine has not
        come back on the new version. Also FAILED, but for the opposite reason: nothing is
        wrong with the hub, the machine simply never confirmed. Left generous (a day by
        default) so a machine flashed on a Friday evening is not written off before Monday.

    Without this a target sits unresolved forever, its job never completes, and the console
    shows a fleet update permanently at 39/40.
    """
    if now is None:
        now = int(time.time())
    closed = 0
    with get_conn(db_path) as conn:
        stale = [dict(r) for r in conn.execute(
            "SELECT id, status FROM firmware_targets WHERE "
            "(status = ? AND COALESCE(flashed_at, dispatched_at, updated_at) < ?) OR "
            "(status = ? AND COALESCE(flashed_at, updated_at) < ?)",
            (TARGET_FLASHING, now - int(flashing_timeout),
             TARGET_REBOOTING, now - int(confirm_timeout)))]
    for target in stale:
        if target["status"] == TARGET_FLASHING:
            message = ("the machine never reported the outcome. It may have been left "
                       "mid-flash -- check it before trying again")
        else:
            message = ("the machine never came back reporting the new BIOS version. It may "
                       "not have restarted yet, or the flash may not have taken")
        _finish_target(db_path, target["id"], TARGET_FAILED, error=message)
        closed += 1
    return closed


# ================================
# CANCEL
# ================================
def cancel_target(db_path, target_id, *, actor="system"):
    """Give up on one machine's flash, if it has not been handed the image yet.

    PENDING and IN_FLIGHT can be recalled -- the command is expired via
    `fleet.cancel_command_if_pending`, whose conditional UPDATE closes the race with
    `claim_commands`, so the machine never starts. FLASHING and REBOOTING cannot: the agent
    holds the image and the firmware may already be written, and a console row reading
    "cancelled" over a machine that is mid-flash is worse than no cancel button. Same
    three-outcome honesty the backup cancel needed, minus the middle case.
    """
    target = get_target(db_path, target_id)
    if target is None:
        return False, "unknown"
    if target["status"] not in TARGET_RECALLABLE:
        return False, target["status"]
    if target["command_id"]:
        fleet.cancel_command_if_pending(db_path, target["command_id"])
    _finish_target(db_path, target_id, TARGET_CANCELLED, error="cancelled by an operator")
    fleet.audit(db_path, actor=actor, action="cancel_firmware_update",
                level=fleet.LEVEL_NOTICE, target=target["machine"],
                detail={"update_id": target_id, "job_id": target["job_id"]})
    return True, TARGET_CANCELLED


def cancel_job(db_path, job_id, *, actor="system"):
    """Cancel every target that can still be recalled, and close the job.

    Returns (cancelled, left_running). The second number is the honest half: a job cancelled
    while six machines are already flashing has stopped nothing for those six, and the
    console has to say so rather than showing a cancelled job over hardware that is being
    written to right now.
    """
    job = get_job(db_path, job_id)
    if job is None:
        return 0, 0
    cancelled = 0
    left = 0
    for target in job["targets"]:
        if target["status"] in TARGET_RECALLABLE:
            ok, _ = cancel_target(db_path, target["id"], actor=actor)
            cancelled += 1 if ok else 0
        elif target["status"] not in TARGET_TERMINAL:
            left += 1
    now = int(time.time())
    with get_conn(db_path) as conn:
        if left:
            # Not marked cancelled while machines are still flashing: the job is genuinely
            # still running, and saying otherwise would hide them.
            _refresh_job_status(conn, job_id, now)
        else:
            conn.execute("UPDATE firmware_jobs SET status = ?, updated_at = ? WHERE id = ?",
                         (JOB_CANCELLED, now, job_id))
    fleet.audit(db_path, actor=actor, action="cancel_firmware_job",
                level=fleet.LEVEL_NOTICE, target=job.get("payload_name") or job_id,
                detail={"job_id": job_id, "cancelled": cancelled, "still_flashing": left})
    return cancelled, left


def tick(db_path, now=None, ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS,
         online_machines=None, flashing_timeout=DEFAULT_FLASHING_TIMEOUT_SECONDS,
         confirm_timeout=DEFAULT_CONFIRM_TIMEOUT_SECONDS):
    """One scheduler pass: retire what nobody will answer for, then dispatch what is due.
    Returns (retired, dispatched) for the caller's log line."""
    expired = reconcile_once(db_path)
    expired += expire_stale(db_path, now=now, flashing_timeout=flashing_timeout,
                            confirm_timeout=confirm_timeout)
    dispatched = dispatch_once(db_path, now=now, ttl_seconds=ttl_seconds,
                               online_machines=online_machines)
    return expired, dispatched


# ================================
# MACHINE LIFECYCLE
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's targets and roll its jobs up again -- the same lifecycle
    hook permissions, packages and bios all implement, so a deleted machine cannot leave a
    job stuck at 9/10 forever."""
    machine = _clean(machine)
    if not machine:
        return
    now = int(time.time())
    with get_conn(db_path) as conn:
        jobs = [r["job_id"] for r in conn.execute(
            "SELECT DISTINCT job_id FROM firmware_targets WHERE machine = ?", (machine,))]
        conn.execute("DELETE FROM firmware_targets WHERE machine = ?", (machine,))
        for job_id in jobs:
            _refresh_job_status(conn, job_id, now)


def rename_machine(db_path, old_name, new_name):
    """Follow a machine through a duplicate-serial merge. If the survivor is already a target
    of the same job the dropped row is removed rather than colliding on UNIQUE(job, machine)
    -- both rows describe one physical machine, and it only needs flashing once."""
    old_name = _clean(old_name)
    new_name = _clean(new_name)
    if not old_name or not new_name or old_name == new_name:
        return
    now = int(time.time())
    with get_conn(db_path) as conn:
        for row in conn.execute(
                "SELECT id, job_id FROM firmware_targets WHERE machine = ?",
                (old_name,)).fetchall():
            exists = conn.execute(
                "SELECT 1 FROM firmware_targets WHERE job_id = ? AND machine = ?",
                (row["job_id"], new_name)).fetchone()
            if exists:
                conn.execute("DELETE FROM firmware_targets WHERE id = ?", (row["id"],))
            else:
                conn.execute(
                    "UPDATE firmware_targets SET machine = ?, updated_at = ? WHERE id = ?",
                    (new_name, now, row["id"]))
