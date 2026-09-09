"""Where a device is, when somebody asked -- roadmap #23 phase B.

**On-demand only. Nothing here polls, and that is the design rather than a first step.** A fix
exists in this table because an operator pressed a button and the device answered; there is no
background collection, no periodic sample, and no trail of where a device has been except the
trail of times somebody asked. The Android agent's manifest used to argue that a fleet agent
asking for location is an agent nobody installs, and that comment is rewritten rather than
deleted (see the manifest): what changed is that there is now a console that can show a fix, a
capability that gates asking, an audit row for every ask, and a notification on the device
naming the operator who asked. What did not change is that nobody is tracked.

**This is the first personal data in the product**, and it is personal in a way an inventory of
disks is not: a machine's location during working hours is a person's location during working
hours. Three things follow, and none of them is housekeeping:

  * Reading is `view` PLUS machine scope, and asking is its own capability
    (`locate_device`) -- not a reuse of `issue_commands`, which every other command-issuing
    feature reuses. "May reboot a PC" must not silently imply "may find out where an employee
    is". That is a departure from the house precedent, made on purpose; see permissions.py.
  * Rows expire on `data.location_retention_days` (30 by default), pruned by age rather than
    kept forever like the audit trail. A location history nobody set out to build is still a
    location history.
  * `forget_machine` drops all of it. Unlike patch outcomes and backup manifests, which have an
    argument for surviving a machine's deletion, the location history of a decommissioned
    device is pure liability.

**A failed locate is stored, not discarded.** "We asked at 14:02 and the device said it had no
fix" is the answer to the question an operator is actually asking, and it is a different answer
from "we never asked". Same reasoning as wake.py's NO_RELAY being a first-class outcome: a row
that records silence truthfully beats no row at all.

**No request lifecycle of its own, unlike wake.py.** A wake needs one because delivery is a
relay through a third machine and success is confirmed by the target's later check-in. A locate
is one command to one device with one answer, so the command queue already models every state
it has -- pending, claimed, done, failed, expired. A second state machine over the top would be
two things to keep in step for no extra information.

Kept free of Flask so it can be unit-tested in isolation.
"""
import json
import sqlite3
import time

import fleet

# ================================
# VOCABULARY
# ================================
#: The command the agent answers. Its executor is Core-side (LocateDeviceExecutor) so the
#: protocol half stays testable without a device.
COMMAND_TYPE = "locate_device"

#: A fix came back with coordinates.
STATUS_LOCATED = "located"
#: The device answered and had no fix to give -- location switched off, no provider, nothing
#: within the timeout, or the permission was never granted. **Not a failure**: the device did
#: exactly what it was asked and the answer is "I cannot tell you". A failure would be
#: indistinguishable from a network problem, which is the same line ShowMessageExecutor draws
#: for its `no_session` case.
STATUS_UNAVAILABLE = "unavailable"
#: The command never produced an answer at all -- it failed, or expired unclaimed. Recorded so
#: that "the device is offline" does not look identical to "the device has no GPS".
STATUS_NO_ANSWER = "no_answer"
STATUSES = (STATUS_LOCATED, STATUS_UNAVAILABLE, STATUS_NO_ANSWER)

# ---------------------------------------------------------------- ingest bounds
MAX_TEXT_CHARS = 200
#: How many fixes to keep per machine, on top of the age-based retention. The age limit is the
#: privacy control and this is the storage one: a machine somebody locates fifty times in an
#: afternoon must not be able to outgrow the table before the pruner next runs.
MAX_FIXES_PER_MACHINE = 100
#: Accuracy is a radius in metres. Anything past this is not a fix, it is a cell-tower guess
#: covering a city, and plotting it as a point would imply a precision that is not there.
MAX_ACCURACY_M = 50_000


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_location_db(db_path):
    """Create the location table. Idempotent -- safe to call on every hub start next to
    app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_locations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                machine      TEXT NOT NULL,
                status       TEXT NOT NULL,
                lat          REAL,
                lon          REAL,
                accuracy_m   REAL,
                provider     TEXT NOT NULL DEFAULT '',
                stale        INTEGER NOT NULL DEFAULT 0,
                fixed_at     INTEGER,
                received_at  INTEGER NOT NULL,
                requested_by TEXT NOT NULL DEFAULT '',
                command_id   TEXT NOT NULL DEFAULT '',
                detail       TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_locations_machine "
                     "ON machine_locations(machine, received_at DESC)")
        # The pruner sweeps the whole table by age, and the fleet map asks for the newest fix
        # per machine. Both read this column and neither filters by machine first.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_locations_received "
                     "ON machine_locations(received_at)")
        # One command produces at most one row. Without this a retried result post -- an agent
        # that answered, lost the network before reading the response, and answered again --
        # would file the same fix twice and the history would show a device located twice in
        # one second from slightly different coordinates.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_machine_locations_command "
                     "ON machine_locations(command_id) WHERE command_id != ''")


# ================================
# INGEST
# ================================
def _clean(value, limit=MAX_TEXT_CHARS):
    return str(value if value is not None else "").strip()[:limit]


def _coordinate(value, limit):
    """A latitude or longitude, or None if it is not one.

    Zero is a legitimate value on both axes, so this cannot use truthiness -- and (0, 0) in the
    Gulf of Guinea is the classic symptom of exactly that mistake made somewhere upstream.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) > limit:      # NaN, or off the planet
        return None
    return number


def clean_fix(payload):
    """Normalise what an agent reported into a row, or None if it is not a location answer.

    Returns a dict with `status` already decided: coordinates make it LOCATED, anything else
    is UNAVAILABLE with whatever the device said about why. Kept separate from `record_fix` so
    a test can assert the parse without a database.
    """
    if not isinstance(payload, dict):
        return None

    lat = _coordinate(payload.get("lat"), 90)
    lon = _coordinate(payload.get("lon"), 180)
    if lat is None or lon is None:
        # A device that cannot see the sky is the ordinary case, not an error. The reason it
        # gave is what an operator acts on -- "location is switched off" and "no fix in 45
        # seconds indoors" lead to completely different next steps.
        return {
            "status": STATUS_UNAVAILABLE,
            "lat": None, "lon": None, "accuracy_m": None, "provider": "",
            "stale": 0, "fixed_at": None,
            "detail": _clean(payload.get("error") or payload.get("detail")
                             or "the device did not report a position"),
        }

    accuracy = _coordinate(payload.get("accuracy_m"), MAX_ACCURACY_M)
    fixed_at = payload.get("fixed_at")
    try:
        fixed_at = int(fixed_at)
    except (TypeError, ValueError):
        fixed_at = None
    return {
        "status": STATUS_LOCATED,
        "lat": lat,
        "lon": lon,
        # None rather than 0 when it is missing or absurd. Zero metres is a claim of perfect
        # precision, and the console draws the accuracy circle from this number.
        "accuracy_m": None if accuracy is None or accuracy < 0 else accuracy,
        "provider": _clean(payload.get("provider"), 40),
        # The device is telling us this is a LAST KNOWN position rather than one it just took.
        # Carried through to the console because a two-hour-old fix shown as current is the
        # single most misleading thing this feature could do.
        "stale": 1 if payload.get("stale") else 0,
        "fixed_at": fixed_at,
        "detail": "",
    }


def record_fix(db_path, machine, payload, *, requested_by="", command_id="", now=None):
    """Store one answer. Returns the row id, or None if there was nothing to store.

    Called from the command-result path, so it is bounded and type-checked throughout: a
    malformed answer costs a missing fix, never a 500 on the endpoint every agent in the fleet
    posts results to.
    """
    machine = _clean(machine, 200)
    fix = clean_fix(payload)
    if not machine or fix is None:
        return None
    now = int(time.time()) if now is None else int(now)

    with get_conn(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO machine_locations(machine, status, lat, lon, accuracy_m, "
                "                              provider, stale, fixed_at, received_at, "
                "                              requested_by, command_id, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (machine, fix["status"], fix["lat"], fix["lon"], fix["accuracy_m"],
                 fix["provider"], fix["stale"], fix["fixed_at"], now,
                 _clean(requested_by, 200), _clean(command_id, 64), fix["detail"]))
        except sqlite3.IntegrityError:
            # The unique index on command_id. A duplicate result post is not an error worth
            # raising into the agent's result endpoint -- the fix is already filed.
            return None
        row_id = cursor.lastrowid
        _trim(conn, machine)
    return row_id


def record_no_answer(db_path, machine, *, requested_by="", command_id="", detail="",
                     now=None):
    """File the fact that a locate produced no answer at all -- the command failed, or expired
    without the device ever claiming it.

    Its own entry point rather than a status inside `record_fix`, because the two arrive from
    different places: this one has no payload to parse. Distinguishing it matters to whoever is
    reading the history -- "the phone is switched off" and "the phone is on and cannot see the
    sky" send somebody to look in completely different places.
    """
    machine = _clean(machine, 200)
    if not machine:
        return None
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO machine_locations(machine, status, received_at, requested_by, "
                "                              command_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (machine, STATUS_NO_ANSWER, now, _clean(requested_by, 200),
                 _clean(command_id, 64),
                 _clean(detail) or "the device did not answer the request"))
        except sqlite3.IntegrityError:
            return None
        row_id = cursor.lastrowid
        _trim(conn, machine)
    return row_id


def _trim(conn, machine):
    """Keep at most MAX_FIXES_PER_MACHINE rows for one machine, newest first.

    The age-based prune is the privacy control and runs on the retention thread; this is the
    storage one and runs on every insert, so a machine somebody locates fifty times in an
    afternoon cannot outgrow the table between prunes.
    """
    conn.execute(
        "DELETE FROM machine_locations WHERE machine = ? AND id NOT IN "
        "(SELECT id FROM machine_locations WHERE machine = ? "
        " ORDER BY received_at DESC, id DESC LIMIT ?)",
        (machine, machine, MAX_FIXES_PER_MACHINE))


def handle_result(db_path, command_id, *, success=True, result=None, output=None, now=None):
    """Turn a finished `locate_device` command into a row. Returns the row id, or None.

    Returns None immediately for any command that is not a locate, which is very nearly all of
    them: one indexed lookup per command result, the same shape rules.handle_probe_result uses
    on the same hook.

    The answer arrives as the command's OUTPUT -- a JSON object -- rather than in a new wire
    field, so the result envelope is unchanged and an agent too old to know about locating
    cannot send something this misreads. A structured `result` is honoured first in case a
    later agent sends one.
    """
    command = fleet.get_command(db_path, command_id)
    if command is None or command.get("type") != COMMAND_TYPE:
        return None

    machine = command.get("machine")
    requested_by = command.get("issued_by") or ""

    if result is None and output:
        try:
            parsed = json.loads(output)
            result = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            result = None

    if not success and result is None:
        # The agent reported a failure with nothing parseable in it -- an executor that threw,
        # or a hub-side refusal. That is "no answer", not "no fix": see record_no_answer.
        return record_no_answer(db_path, machine, requested_by=requested_by,
                                command_id=command_id, detail=_clean(output), now=now)
    return record_fix(db_path, machine, result or {}, requested_by=requested_by,
                      command_id=command_id, now=now)


def sweep_unanswered(db_path, now=None):
    """File a NO_ANSWER row for every locate command that expired or failed without one.

    Runs on the retention tick. Without it a locate aimed at a phone that is switched off sits
    in the console as "waiting" forever -- the command expires quietly and nothing is watching
    the queue for a type that has no scheduler of its own. Returns how many were filed.
    """
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.id, c.machine, c.issued_by, c.status FROM commands c "
            "WHERE c.type = ? AND c.status IN (?, ?) AND c.id NOT IN "
            "(SELECT command_id FROM machine_locations WHERE command_id != '')",
            (COMMAND_TYPE, fleet.STATUS_EXPIRED, fleet.STATUS_FAILED))]
    filed = 0
    for row in rows:
        detail = ("the device never collected the request"
                  if row["status"] == fleet.STATUS_EXPIRED
                  else "the device could not answer the request")
        if record_no_answer(db_path, row["machine"], requested_by=row["issued_by"] or "",
                            command_id=row["id"], detail=detail, now=now) is not None:
            filed += 1
    return filed


# ================================
# READ
# ================================
def _row(row):
    fix = dict(row)
    fix["stale"] = bool(fix["stale"])
    return fix


def latest_fix(db_path, machine):
    """The newest row that actually has coordinates, or None.

    Deliberately not "the newest row": a locate that came back unavailable an hour ago does not
    erase where the device was this morning, and a map that blanked on every failed attempt
    would be least useful exactly when somebody is trying hardest to find a device.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM machine_locations WHERE machine = ? AND status = ? "
            "ORDER BY received_at DESC, id DESC LIMIT 1",
            (str(machine or "").strip(), STATUS_LOCATED)).fetchone()
    return _row(row) if row is not None else None


def history(db_path, machine, limit=20):
    """Recent attempts, newest first, INCLUDING the ones that produced no fix. What an operator
    reads after the fact: "did we ask, and what did it say"."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM machine_locations WHERE machine = ? "
            "ORDER BY received_at DESC, id DESC LIMIT ?",
            (str(machine or "").strip(), max(1, min(200, int(limit or 20))))).fetchall()
    return [_row(r) for r in rows]


def latest_fixes(db_path, machines=None):
    """The newest fix for each machine that has one -- the fleet map's whole query.

    One statement rather than a query per machine: the map asks for every machine in scope at
    once, and a hundred round trips is how a map page becomes the slowest thing in the console.
    `machines` is the scope filter and is applied HERE for the same reason wake.count_open_requests
    takes one -- filtering in Python means reading rows the caller may not see.
    """
    clauses = ["l.status = ?"]
    params = [STATUS_LOCATED]
    if machines is not None:
        scope = [str(m).strip() for m in machines if str(m or "").strip()]
        if not scope:
            return []
        clauses.append(f"l.machine IN ({','.join('?' for _ in scope)})")
        params.extend(scope)
    where = " AND ".join(clauses)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT l.* FROM machine_locations l WHERE {where} AND l.id = "
            f"(SELECT id FROM machine_locations WHERE machine = l.machine AND status = ? "
            f" ORDER BY received_at DESC, id DESC LIMIT 1) "
            f"ORDER BY l.machine ASC", params + [STATUS_LOCATED]).fetchall()
    return [_row(r) for r in rows]


def open_request(db_path, machine):
    """This machine's in-flight locate command, if it has one.

    Read straight off the command queue rather than from a request table of our own -- see the
    module docstring for why this feature has no lifecycle of its own. Returns the command row
    so the console can show pending-versus-claimed without a second call.
    """
    machine = str(machine or "").strip()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, status, created_at, expires_at, issued_by FROM commands "
            "WHERE machine = ? AND type = ? AND status IN (?, ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (machine, COMMAND_TYPE, fleet.STATUS_PENDING, fleet.STATUS_CLAIMED)).fetchone()
    return dict(row) if row is not None else None


# ================================
# RETENTION
# ================================
def prune(db_path, cutoff):
    """Drop fixes older than `cutoff` (epoch seconds). Returns how many went.

    **The one prune in this hub that is a privacy control rather than a disk-space one.** Old
    readings are pruned because the table grows; these are pruned because a record of where a
    person was on a Tuesday three months ago should not exist by default. `data.location_retention_days`
    is what an operator sets it from, and the default is deliberately short.
    """
    with get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM machine_locations WHERE received_at < ?",
                              (int(cutoff),))
    return cursor.rowcount or 0


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's location history.

    No exception here, unlike patch outcomes and backup manifests, which survive a machine's
    deletion because they are facts about an update or an archive rather than about the
    machine. Where a device was is a fact about a PERSON, and keeping it after the device is
    gone is pure liability.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_locations WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move location history during a duplicate-serial merge. Unlike the capability report,
    history MERGES rather than picking a winner: a merge folds two records of one physical
    device together, and "where was this phone on Tuesday" is the same question afterwards."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE machine_locations SET machine = ? WHERE machine = ?",
                     (new_machine, old_machine))
        _trim(conn, new_machine)
