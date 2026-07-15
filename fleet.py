"""Fleet management core -- agent enrollment, per-agent auth, the hub->agent
command queue, signed-command verification, and online/offline status.

This is the foundation that turns Temp_Monitor from one-directional telemetry
(companion -> hub) into an RMM: the hub can now queue commands FOR a machine, and
an authenticated agent pulls and executes them. The moment that channel exists,
the open `/api/report` trust model is no longer enough -- anyone who can talk to
the command endpoints could restart or reprogram the whole fleet. So this module
is built security-first:

  * Agents must ENROLL (presenting a shared enrollment secret) to get a
    per-agent bearer token; only the token's SHA-256 is stored, never the token.
  * High-risk command types (arbitrary script, driver install, BIOS flash) must
    carry an Ed25519 signature made with the OFFLINE private key, verified both
    here and again on the agent -- the exact same trust root as the signed
    self-update (see companion.py verify_signature / sign_release.py). A
    compromised hub therefore still cannot brick the fleet without the offline
    key.

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
# Low-risk commands are dispatched on authenticated + authorized session alone.
# High-risk commands additionally REQUIRE a valid offline Ed25519 signature over
# the canonical payload (see canonical_command_bytes) -- these are the ones that
# can run arbitrary code or physically brick hardware, so a stolen hub session or
# a compromised hub process must not be enough to issue them.
LOW_RISK_COMMANDS = frozenset({
    "restart",
    "shutdown",
    "rename",
    "gpupdate",
    "install_app",
})
HIGH_RISK_COMMANDS = frozenset({
    "run_script",
    "install_driver",
    "update_bios",
})
ALL_COMMANDS = LOW_RISK_COMMANDS | HIGH_RISK_COMMANDS

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


def is_high_risk(command_type):
    return command_type in HIGH_RISK_COMMANDS


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
                id                TEXT PRIMARY KEY,
                machine           TEXT NOT NULL,
                type              TEXT NOT NULL,
                params_json       TEXT NOT NULL,
                requires_signature INTEGER NOT NULL DEFAULT 0,
                signature         TEXT,
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
# COMMAND SIGNING (verify side)
# ================================
def canonical_command_bytes(command_type, machine, params):
    """The exact bytes an offline signer signs and both hub and agent verify.

    Deterministic JSON (sorted keys, no spaces) over ONLY the security-relevant
    fields -- type, machine, params. Hub-assigned metadata (id, timestamps, TTL)
    is intentionally excluded so the same signature is valid regardless of when
    the command is queued. Signer (sign_release.py --sign-command), this module,
    and the agent must all build these bytes identically.
    """
    payload = {
        "type": str(command_type),
        "machine": str(machine),
        "params": params if params is not None else {},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_command_signature(public_key_hex, command_type, machine, params, signature_hex):
    """Verify an Ed25519 signature over canonical_command_bytes. Fails closed on
    an unset key, missing cryptography, malformed input, or mismatch -- mirrors
    companion.py's verify_signature exactly."""
    if not public_key_hex:
        return False
    if not signature_hex:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except Exception:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(
            bytes.fromhex(str(signature_hex).strip()),
            canonical_command_bytes(command_type, machine, params),
        )
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


# ================================
# COMMAND QUEUE
# ================================
def create_command(db_path, machine, command_type, params, issued_by,
                   signature=None, public_key_hex=None,
                   ttl_seconds=DEFAULT_COMMAND_TTL_SECONDS):
    """Queue a command for a machine. Returns its id.

    Enforces the risk tier: an unknown type is rejected, and a high-risk type is
    rejected unless `signature` verifies against `public_key_hex` over the
    canonical payload. The agent verifies the SAME signature again before
    executing -- this hub-side check is the first of two gates, so a bad command
    never even reaches the fleet.
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

    requires_signature = is_high_risk(command_type)
    if requires_signature:
        if not verify_command_signature(public_key_hex, command_type, machine, params, signature):
            raise PermissionError(
                f"{command_type} is high-risk and requires a valid offline signature"
            )

    command_id = uuid.uuid4().hex
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO commands(id, machine, type, params_json, requires_signature, "
            "signature, issued_by, created_at, expires_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                command_id, machine, command_type, json.dumps(params, sort_keys=True),
                1 if requires_signature else 0, signature, str(issued_by),
                now, now + int(ttl_seconds), STATUS_PENDING,
            ),
        )
    audit(db_path, actor=issued_by, action="issue_command", target=machine,
          detail={"command_id": command_id, "type": command_type,
                  "high_risk": requires_signature})
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
    execute (id, type, params, requires_signature, signature).

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
            "SELECT id, type, params_json, requires_signature, signature "
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
                "requires_signature": bool(row["requires_signature"]),
                "signature": row["signature"],
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
    command["params"] = json.loads(command.pop("params_json"))
    command["result"] = dict(result) if result else None
    return command


def list_commands(db_path, machine=None, limit=100):
    """Recent commands, newest first, optionally scoped to one machine."""
    sql = "SELECT id, machine, type, issued_by, created_at, status, requires_signature FROM commands"
    params = []
    if machine:
        sql += " WHERE machine = ?"
        params.append(str(machine).strip())
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]
