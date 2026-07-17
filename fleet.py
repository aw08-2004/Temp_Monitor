"""Fleet management core -- agent enrollment, per-agent auth, the hub->agent
command queue, and online/offline status.

This is the foundation that turns Temp_Monitor from one-directional telemetry
(companion -> hub) into an RMM: the hub can now queue commands FOR a machine, and
an authenticated agent pulls and executes them. The moment that channel exists,
the open `/api/report` trust model is no longer enough -- anyone who can talk to
the command endpoints could restart or reprogram the whole fleet. Two controls
carry that weight:

  * Agents must ENROLL (presenting a shared enrollment secret) to get a
    per-agent bearer token; only the token's SHA-256 is stored, never the token.
  * Issuing a command requires an authenticated, allow-listed console session
    (ALLOWED_EMAILS). Every issue/claim/completion lands in the append-only
    audit_log, including the full params -- with no second gate, that trail is
    the accountability control, so it must never be allowed to go quiet.

Commands are NOT signed. They used to be: high-risk types (run_script,
install_driver, update_bios) once required an Ed25519 signature made with an
offline private key, verified here and again on the agent. That model assumed a
single operator holding the key, and could not serve a helpdesk group -- no
teammate could run a script without the key holder signing it for them. It was
also never actually live (no key was ever configured on hub or agent, so every
high-risk command was refused outright). It is gone; ALLOWED_EMAILS is now the
whole perimeter for running code as SYSTEM across the fleet.

This does NOT touch the release/self-update trust root, which is a SEPARATE
Ed25519 key and is still fully enforced: see sign_release.py (sign / sign_agent),
companion.py verify_signature, and the agent's SelfUpdater +
AgentConfig.UpdatePublicKeyHex. A compromised hub still cannot push a malicious
binary to the fleet.

Kept deliberately free of Flask so it can be unit-tested in isolation; app.py
wires thin HTTP endpoints on top of these functions.
"""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid

# ================================
# COMMAND TAXONOMY
# ================================
# Every type dispatches on an authenticated + allow-listed console session alone.
# There is deliberately no risk split any more: run_script runs arbitrary code as
# SYSTEM, but so does install_app (winget) and so, effectively, does a rename or a
# restart of the wrong box -- gating a subset behind an offline key bought little
# and cost the helpdesk group the ability to use the channel at all. The audit_log
# is what distinguishes these now, not a signature.
ALL_COMMANDS = frozenset({
    "restart",
    "shutdown",
    "rename",
    "gpupdate",
    "install_app",
    "run_script",
    "install_driver",
    "update_bios",
})

# Command lifecycle states.
STATUS_PENDING = "pending"    # queued, not yet handed to an agent
STATUS_CLAIMED = "claimed"    # delivered to the agent, awaiting a result
STATUS_DONE = "done"          # agent reported success
STATUS_FAILED = "failed"      # agent reported failure
STATUS_EXPIRED = "expired"    # TTL elapsed before an agent claimed it

DEFAULT_COMMAND_TTL_SECONDS = 15 * 60
# A machine is "online" if we've heard from it within this window. Heartbeats and
# ordinary temp reports both refresh last_seen, so this is really "seconds since
# any contact". Kept a bit above the companion's report cadence so a single missed
# report doesn't flap the status.
DEFAULT_OFFLINE_AFTER_SECONDS = 90


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_fleet_db(db_path):
    """Create the fleet tables if absent. Idempotent -- safe to call next to
    app.init_db() on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                agent_id     TEXT PRIMARY KEY,
                machine      TEXT NOT NULL,
                token_hash   TEXT NOT NULL,
                enrolled_at  INTEGER NOT NULL,
                last_seen    INTEGER,
                revoked      INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # One machine can re-enroll (reinstall) and supersede its old agent row;
        # we look agents up by agent_id, but also want fast machine lookups.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_machine ON agents(machine)")
        # NOTE: databases created before command signing was removed also carry
        # `requires_signature INTEGER NOT NULL DEFAULT 0` and `signature TEXT`
        # here. They are deliberately left in place rather than migrated away:
        # the former has a DEFAULT and the latter is nullable, so the INSERT below
        # (which names its columns) works unchanged against both an old table and
        # a fresh one, and nothing reads them any more. Do NOT re-add them, and do
        # NOT reference them in a SELECT -- a fresh DB has no such columns.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
                id                TEXT PRIMARY KEY,
                machine           TEXT NOT NULL,
                type              TEXT NOT NULL,
                params_json       TEXT NOT NULL,
                issued_by         TEXT NOT NULL,
                created_at        INTEGER NOT NULL,
                expires_at        INTEGER NOT NULL,
                status            TEXT NOT NULL,
                claimed_at        INTEGER,
                claimed_by        TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_commands_machine_status "
            "ON commands(machine, status)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS command_results (
                command_id   TEXT PRIMARY KEY,
                agent_id     TEXT NOT NULL,
                success      INTEGER NOT NULL,
                output       TEXT,
                completed_at INTEGER NOT NULL
            )
            """
        )
        # Live output, streamed by the agent while a command runs, so the console
        # terminal can show progress instead of a spinner. This is SCROLLBACK, not the
        # record: command_results.output remains the durable, complete copy that the
        # agent posts on completion, and these rows are pruned on a short horizon (see
        # prune_command_output).
        #
        # `seq` is a per-command counter owned by the agent. PRIMARY KEY (command_id,
        # seq) + INSERT OR IGNORE makes a retried POST a free no-op -- which is why the
        # agent must retry the SAME seq rather than allocating a new one. stdout and
        # stderr are deliberately not distinguished: ProcessRunner already merges them
        # into one buffer and the terminal renders them identically.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS command_output_chunks (
                command_id  TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                chunk       TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                PRIMARY KEY (command_id, seq)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_output_chunks_cmd "
            "ON command_output_chunks(command_id, seq)"
        )
        # Append-only audit trail: every command issued/claimed/completed and every
        # enrollment. This is the record you reach for after "who restarted prod?".
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                actor       TEXT NOT NULL,
                action      TEXT NOT NULL,
                target      TEXT,
                detail_json TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")


def audit(db_path, actor, action, target=None, detail=None):
    """Record one line in the append-only audit trail. Never raises on a bad
    detail payload -- auditing must not be able to break the action it records."""
    try:
        detail_json = json.dumps(detail, sort_keys=True) if detail is not None else None
    except (TypeError, ValueError):
        detail_json = json.dumps({"_unserializable": str(detail)})
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO audit_log(ts, actor, action, target, detail_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), str(actor), str(action), target, detail_json),
        )


# ================================
# ENROLLMENT & AGENT AUTH
# ================================
def _hash_token(token):
    """Store only the hash, so a DB leak doesn't hand out live agent tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def enroll_agent(db_path, machine, provided_secret, expected_secret):
    """Register an agent for `machine` and return (agent_id, token).

    The agent must present the shared enrollment secret (distributed at install
    time). Compared in constant time so a wrong secret can't be brute-forced by
    timing. The plaintext token is returned exactly once -- only its hash is
    persisted -- so the agent must store it locally after this call.
    """
    machine = str(machine or "").strip()
    if not machine:
        raise ValueError("machine is required to enroll")
    if not expected_secret:
        # Fail closed: an unset enrollment secret must not mean "anyone may enroll".
        raise PermissionError("enrollment is not configured on this hub")
    if not hmac.compare_digest(str(provided_secret or ""), str(expected_secret)):
        raise PermissionError("invalid enrollment secret")

    agent_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, machine, token_hash, enrolled_at, last_seen, revoked) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (agent_id, machine, _hash_token(token), now, now),
        )
    audit(db_path, actor=f"agent:{machine}", action="enroll", target=machine,
          detail={"agent_id": agent_id})
    return agent_id, token


def authenticate_agent(db_path, agent_id, token, touch=True):
    """Return the agent's machine name if (agent_id, token) is valid and not
    revoked, else None. Constant-time token comparison. When `touch`, refreshes
    last_seen so status derivation and heartbeating share one code path."""
    if not agent_id or not token:
        return None
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT machine, token_hash, revoked FROM agents WHERE agent_id = ?",
            (str(agent_id),),
        ).fetchone()
        if row is None or row["revoked"]:
            return None
        if not hmac.compare_digest(row["token_hash"], _hash_token(token)):
            return None
        if touch:
            conn.execute(
                "UPDATE agents SET last_seen = ? WHERE agent_id = ?",
                (int(time.time()), str(agent_id)),
            )
    return row["machine"]


def revoke_agent(db_path, agent_id, actor="system"):
    with get_conn(db_path) as conn:
        conn.execute("UPDATE agents SET revoked = 1 WHERE agent_id = ?", (str(agent_id),))
    audit(db_path, actor=actor, action="revoke_agent", target=agent_id)


def touch_last_seen(db_path, machine):
    """Refresh every (non-revoked) agent row for a machine. Called from the legacy
    telemetry path so an already-reporting machine reads as online even before it
    adopts the new heartbeat endpoint."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE machine = ? AND revoked = 0",
            (int(time.time()), str(machine or "").strip()),
        )


# ================================
# ONLINE / OFFLINE STATUS
# ================================
def derive_status(last_seen, now=None, offline_after=DEFAULT_OFFLINE_AFTER_SECONDS):
    """'online' | 'offline' | 'unknown' from a last_seen epoch."""
    if last_seen is None:
        return "unknown"
    if now is None:
        now = time.time()
    return "online" if (now - int(last_seen)) <= offline_after else "offline"


def list_agent_status(db_path, now=None, offline_after=DEFAULT_OFFLINE_AFTER_SECONDS):
    """One row per machine: latest last_seen across its agents + derived status.
    Feeds the asset-inventory online/offline view and the offline-alert rule."""
    if now is None:
        now = time.time()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT machine, MAX(last_seen) AS last_seen FROM agents "
            "WHERE revoked = 0 GROUP BY machine"
        ).fetchall()
    result = []
    for row in rows:
        result.append({
            "machine": row["machine"],
            "last_seen": row["last_seen"],
            "status": derive_status(row["last_seen"], now, offline_after),
        })
    result.sort(key=lambda r: r["machine"])
    return result


# ================================
# COMMAND QUEUE
# ================================
# Cap on the params recorded in the audit trail. Big enough for any realistic
# script, bounded so one pasted megabyte can't bloat the log table.
AUDIT_PARAMS_MAX_CHARS = 4096


def create_command(db_path, machine, command_type, params, issued_by,
                   ttl_seconds=DEFAULT_COMMAND_TTL_SECONDS):
    """Queue a command for a machine. Returns its id.

    Validates the type and params shape; authorization happened upstream, at the
    session gate (see fleet_web.create_fleet_blueprint / app.login_required).
    Every call is audited with the full params, because that record is the only
    thing standing behind "who ran this script?".
    """
    machine = str(machine or "").strip()
    if not machine:
        raise ValueError("machine is required")
    if command_type not in ALL_COMMANDS:
        raise ValueError(f"unknown command type: {command_type!r}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    command_id = uuid.uuid4().hex
    now = int(time.time())
    params_json = json.dumps(params, sort_keys=True)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO commands(id, machine, type, params_json, "
            "issued_by, created_at, expires_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                command_id, machine, command_type, params_json, str(issued_by),
                now, now + int(ttl_seconds), STATUS_PENDING,
            ),
        )
    audit(db_path, actor=issued_by, action="issue_command", target=machine,
          detail={"command_id": command_id, "type": command_type,
                  "params": params_json[:AUDIT_PARAMS_MAX_CHARS]})
    return command_id


def _expire_stale(conn, machine, now):
    """Mark still-pending commands past their TTL as expired, so an agent never
    executes a stale 'restart' that was queued hours ago while it was offline."""
    conn.execute(
        "UPDATE commands SET status = ? "
        "WHERE machine = ? AND status = ? AND expires_at < ?",
        (STATUS_EXPIRED, machine, STATUS_PENDING, now),
    )


def claim_commands(db_path, agent_id, machine):
    """Atomically hand every currently-pending command for `machine` to the
    calling agent and mark them claimed. Returns a list of dicts the agent can
    execute (id, type, params).

    Expiry is enforced first so a long-offline agent coming back doesn't run a
    pile of stale actions. Marking claimed here (rather than on result) makes
    delivery at-most-once by default; a command with no result can be re-issued.
    """
    machine = str(machine or "").strip()
    now = int(time.time())
    claimed = []
    with get_conn(db_path) as conn:
        _expire_stale(conn, machine, now)
        rows = conn.execute(
            "SELECT id, type, params_json "
            "FROM commands WHERE machine = ? AND status = ? ORDER BY created_at ASC",
            (machine, STATUS_PENDING),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE commands SET status = ?, claimed_at = ?, claimed_by = ? WHERE id = ?",
                (STATUS_CLAIMED, now, str(agent_id), row["id"]),
            )
            claimed.append({
                "id": row["id"],
                "type": row["type"],
                "params": json.loads(row["params_json"]),
            })
    if claimed:
        audit(db_path, actor=f"agent:{agent_id}", action="claim_commands", target=machine,
              detail={"command_ids": [c["id"] for c in claimed]})
    return claimed


def complete_command(db_path, command_id, agent_id, success, output=None):
    """Record an agent's result for a command and move it to done/failed. Rejects
    a result for a command that wasn't claimed by this agent, so one agent can't
    close out another's command."""
    command_id = str(command_id)
    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT machine, status, claimed_by FROM commands WHERE id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown command")
        if row["claimed_by"] != str(agent_id):
            raise PermissionError("command was not claimed by this agent")
        conn.execute(
            "INSERT OR REPLACE INTO command_results(command_id, agent_id, success, output, completed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (command_id, str(agent_id), 1 if success else 0,
             None if output is None else str(output), now),
        )
        conn.execute(
            "UPDATE commands SET status = ? WHERE id = ?",
            (STATUS_DONE if success else STATUS_FAILED, command_id),
        )
        machine = row["machine"]
    audit(db_path, actor=f"agent:{agent_id}", action="complete_command", target=machine,
          detail={"command_id": command_id, "success": bool(success)})
    return machine


# ================================
# LIVE OUTPUT STREAMING
# ================================
# Caps. The per-chunk cap bounds one request; the per-command cap bounds a runaway
# script (a `while($true){ echo x }` would otherwise fill the disk). The agent flushes
# at the same per-chunk threshold so it splits before the hub has to reject anything.
STREAM_MAX_CHUNK_CHARS = 16_000
STREAM_MAX_COMMAND_CHARS = 256_000
STREAM_MAX_CHUNKS = 2000
STREAM_TRUNCATION_MARKER = "\n…(output cap reached — streaming stopped)\n"

# Scrollback horizon. command_results.output is the durable record, so chunks only need
# to outlive an operator watching the terminal.
OUTPUT_RETENTION_SECONDS = 24 * 60 * 60


def append_command_output(db_path, command_id, agent_id, seq, chunk):
    """Append one streamed output chunk from the executing agent. Returns True if the
    per-command cap has been hit (the agent should stop streaming), else False.

    Idempotent on (command_id, seq): a retried POST for a chunk that already landed is
    a silent no-op, so the agent can retry a timed-out request without risking a
    duplicate or a gap. Refuses a command this agent didn't claim, mirroring
    complete_command -- one agent must not be able to inject output into another's.
    """
    command_id = str(command_id)
    try:
        seq = int(seq)
    except (TypeError, ValueError):
        raise ValueError("seq must be an integer")
    if seq < 0:
        raise ValueError("seq must be non-negative")
    if chunk is None:
        chunk = ""
    chunk = str(chunk)
    if len(chunk) > STREAM_MAX_CHUNK_CHARS:
        raise ValueError(
            f"chunk exceeds {STREAM_MAX_CHUNK_CHARS} chars; split it agent-side"
        )

    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, claimed_by FROM commands WHERE id = ?", (command_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown command")
        if row["claimed_by"] != str(agent_id):
            raise PermissionError("command was not claimed by this agent")
        if row["status"] != STATUS_CLAIMED:
            # Already done/failed/expired: the run is over, so late output is either a
            # retry racing the result or a confused agent. Either way, don't reopen it.
            raise PermissionError(f"command is {row['status']}, not accepting output")

        # The marker's presence IS the "capped" flag -- no extra column, and it survives
        # a hub restart because it's just another chunk row.
        stats = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(chunk)), 0) AS total, "
            "       COALESCE(MAX(chunk = ?), 0) AS capped "
            "FROM command_output_chunks WHERE command_id = ?",
            (STREAM_TRUNCATION_MARKER, command_id),
        ).fetchone()
        if stats["capped"]:
            return True  # already capped; drop silently and keep telling the agent to stop

        cur = conn.execute(
            "INSERT OR IGNORE INTO command_output_chunks"
            "(command_id, seq, chunk, received_at) VALUES (?, ?, ?, ?)",
            (command_id, seq, chunk, now),
        )
        # OR IGNORE means a duplicate seq changes nothing, so don't count it toward the cap.
        inserted = (cur.rowcount or 0) > 0
        total = stats["total"] + (len(chunk) if inserted else 0)
        count = stats["n"] + (1 if inserted else 0)

        if total >= STREAM_MAX_COMMAND_CHARS or count >= STREAM_MAX_CHUNKS:
            # Write the marker in the SAME transaction as the chunk that crossed the cap,
            # so `truncated` is true for the console the moment it is true for the agent.
            # seq+1 is safe: the agent stops streaming on this return value, so it will
            # never post that number itself.
            conn.execute(
                "INSERT OR IGNORE INTO command_output_chunks"
                "(command_id, seq, chunk, received_at) VALUES (?, ?, ?, ?)",
                (command_id, seq + 1, STREAM_TRUNCATION_MARKER, now),
            )
            return True
        return False


def get_command_output(db_path, command_id, after_seq=-1):
    """Chunks with seq > after_seq, in order, plus the command's current status and
    result. Bundled so the terminal needs ONE request per poll rather than two.

    `next_seq` is the cursor to pass back as after_seq. It stays 0 for a command whose
    agent never streamed (a pre-3.1 agent), which is how the console tells "no output
    yet" from "this agent doesn't stream" -- see the render rule in fleet-terminal.js.
    """
    command_id = str(command_id)
    try:
        after_seq = int(after_seq)
    except (TypeError, ValueError):
        raise ValueError("after_seq must be an integer")

    with get_conn(db_path) as conn:
        command = conn.execute(
            "SELECT status FROM commands WHERE id = ?", (command_id,)
        ).fetchone()
        if command is None:
            raise KeyError("unknown command")
        rows = conn.execute(
            "SELECT seq, chunk FROM command_output_chunks "
            "WHERE command_id = ? AND seq > ? ORDER BY seq ASC",
            (command_id, after_seq),
        ).fetchall()
        highest = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM command_output_chunks "
            "WHERE command_id = ?",
            (command_id,),
        ).fetchone()["m"]
        truncated = conn.execute(
            "SELECT 1 FROM command_output_chunks WHERE command_id = ? AND chunk = ? LIMIT 1",
            (command_id, STREAM_TRUNCATION_MARKER),
        ).fetchone() is not None
        result = conn.execute(
            "SELECT success, output, completed_at FROM command_results WHERE command_id = ?",
            (command_id,),
        ).fetchone()

    return {
        "chunks": [{"seq": r["seq"], "text": r["chunk"]} for r in rows],
        "next_seq": highest + 1,
        "status": command["status"],
        "truncated": truncated,
        "result": dict(result) if result else None,
    }


def prune_command_output(db_path, older_than):
    """Drop scrollback for commands whose last chunk predates `older_than` (epoch).
    Keeps the chunk table bounded; command_results.output is untouched, so history and
    the audit trail are unaffected. Returns rows removed."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM command_output_chunks WHERE command_id IN ("
            "  SELECT command_id FROM command_output_chunks"
            "  GROUP BY command_id HAVING MAX(received_at) < ?"
            ")",
            (int(older_than),),
        )
        return cur.rowcount or 0


def get_command(db_path, command_id):
    """Full command row + its result (if any), for the console command view."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM commands WHERE id = ?", (str(command_id),)).fetchone()
        if row is None:
            return None
        result = conn.execute(
            "SELECT success, output, completed_at FROM command_results WHERE command_id = ?",
            (str(command_id),),
        ).fetchone()
    command = dict(row)
    # SELECT * over a pre-1.10 DB also picks up the vestigial signing columns (see
    # init_fleet_db). Drop them so this response is identical whatever the DB's age,
    # and so nothing downstream reads a dead 'signature' field as meaningful.
    for legacy in ("requires_signature", "signature"):
        command.pop(legacy, None)
    command["params"] = json.loads(command.pop("params_json"))
    command["result"] = dict(result) if result else None
    return command


def list_commands(db_path, machine=None, limit=100):
    """Recent commands, newest first, optionally scoped to one machine."""
    sql = "SELECT id, machine, type, issued_by, created_at, status FROM commands"
    params = []
    if machine:
        sql += " WHERE machine = ?"
        params.append(str(machine).strip())
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]
