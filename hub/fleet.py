"""Fleet management core -- agent enrollment, per-agent auth, the hub->agent
command queue, and online/offline status.

This is the foundation that turns Temp_Monitor from one-directional telemetry
(agent -> hub) into an RMM: the hub can now queue commands FOR a machine, and
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
    Operators read it back on the Audit Log tab (audit_web.py, list_audit below).
    Note audit_log is never pruned, by design -- if that ever needs bounding, add a
    `data.audit_retention_days` setting consumed by app.retention_pruner rather than
    deleting rows from anywhere else.

Commands are NOT signed. They used to be: high-risk types (run_script,
install_driver, update_bios) once required an Ed25519 signature made with an
offline private key, verified here and again on the agent. That model assumed a
single operator holding the key, and could not serve a helpdesk group -- no
teammate could run a script without the key holder signing it for them. It was
also never actually live (no key was ever configured on hub or agent, so every
high-risk command was refused outright). It is gone; ALLOWED_EMAILS is now the
whole perimeter for running code as SYSTEM across the fleet.

This does NOT touch the release/self-update trust root, which is a SEPARATE
Ed25519 key and is still fully enforced: see sign_release.py (--sign-agent) and
the agent's SignatureVerifier + SelfUpdater + AgentConfig.UpdatePublicKeyHex. A
compromised hub still cannot push a malicious binary to the fleet.

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
# Session-control types for the interactive terminal. Unlike the executable commands
# above, these carry no work of their own -- they steer an operator's persistent shell
# on the agent (see agent Fleet/Shell/ShellSessionManager): shell_input writes stdin to
# the running submission, shell_signal is Ctrl-C (kill the shell's children, keep the
# shell), shell_reset recycles the shell. They are transient, so they are deliberately
# NOT favoritable (see _validate_favorite) -- there's nothing reusable to save.
SESSION_CONTROL_COMMANDS = frozenset({
    "shell_input",
    "shell_signal",
    "shell_reset",
    # shell_open belongs to the ConPTY terminal (see terminal.py), which superseded the three
    # above: it tells the agent to attach a real pseudoconsole to a session id the hub
    # already created, after which keystrokes and VT output flow over the pty_* endpoints
    # rather than through this queue. The other three are kept for a fleet mid-rollout --
    # an agent too old for a pty still gets the line-oriented terminal.
    #
    # Unlike its siblings this one is NOT issuable by hand from /api/fleet/commands: its
    # params name a session row, so a hand-rolled copy would point the agent at a session
    # that does not exist (or, worse, at somebody else's). It is issued only by
    # fleet_web's POST /api/fleet/pty, which creates the session first.
    "shell_open",
})

# Issued by the deployment scheduler rather than by an operator's hand (see packages.py).
# Its params are a SNAPSHOT of one package recipe plus the deployment id the result rolls
# up to, so -- like the session-control types -- there is nothing reusable to save as a
# favorite: replaying yesterday's params would report progress against a finished deploy.
SCHEDULED_COMMANDS = frozenset({
    "deploy_package",
    # Per-PC file backups (roadmap #1b). Same reasoning as deploy_package: the params are
    # a SNAPSHOT of the backup policy plus a one-shot upload URL and this machine's
    # derived encryption key, so replaying yesterday's params as a favorite would upload
    # to a dead URL against a finished run.
    "backup_files",
    "restore_files",
})

# Remote view/control (roadmap #2). Like the session-control types these steer a live,
# one-shot session rather than carrying reusable work: the params hold a session id and
# short-lived, single-use TURN credentials, so a saved copy would point at a dead session.
# Not favoritable for the same reason. Issued by an operator's hand from the Remote tab,
# gated on the remote_control capability at the console session (see remote_web.py).
REMOTE_CONTROL_COMMANDS = frozenset({
    "start_remote_session",
    # Re-reports the machine's logon sessions and display outputs on its next heartbeat.
    # Cheap and read-only, but it belongs to the remote feature and its answer is only
    # meaningful to the Remote tab, so it is gated with the rest of it.
    "refresh_remote_inventory",
})

# Virtual display driver for headless machines (roadmap #2). A machine with no monitor has
# no display output, so Desktop Duplication has nothing to duplicate and the stream is a
# black screen -- an indirect display driver gives the desktop (and the logon screen)
# somewhere to be composited.
#
# install_ carries a SNAPSHOT of the payload pin (a sha256 + a hub URL) taken when the
# operator clicked, so like the deploy and remote types a saved favorite would pin a digest
# that may no longer be the current release. The other two carry only machine-independent
# settings, but they are kept together here because they share one capability gate and one
# audit story, and splitting them would invite issuing an uninstall through a channel that
# does not know what it is uninstalling.
VIRTUAL_DISPLAY_COMMANDS = frozenset({
    "install_virtual_display",
    "uninstall_virtual_display",
    "set_virtual_display_mode",
})

# BIOS/firmware management (roadmap #9).
#
# `refresh_bios_inventory` forces a re-read for the operator who is looking at the tab right
# now and does not want to wait for the next six-hourly scan. Machine-independent and
# empty-params, so unlike the remote and deploy types there is nothing stale a saved copy
# could carry -- it stays favoritable on purpose, because "re-read firmware on these twelve
# PCs" is exactly the shape a favorite is for.
#
# `set_bios_settings` carries only a CHANGE ID; the attribute list and any BIOS setup
# password are fetched from an authenticated agent endpoint at claim time (see
# bios.get_change_payload). That is the restore-plan precedent, and for the same two reasons:
# create_command audits params verbatim, so the password would otherwise sit in the audit log
# inside the database that is itself backed up.
FIRMWARE_COMMANDS = frozenset({
    "refresh_bios_inventory",
    "set_bios_settings",
})

# ...and the half of it that must NOT be favoritable. A change id names one machine's one
# pending set of attribute writes; replaying a saved copy would re-run a change that has
# already been applied, against a machine whose attribute names may not even match -- v1 maps
# no vendor vocabulary onto a common one, so a Dell's `WakeOnLan` means nothing on a Lenovo.
# `refresh_bios_inventory` stays favoritable; only this one is excluded.
UNSAVEABLE_FIRMWARE_COMMANDS = frozenset({
    "set_bios_settings",
})

ALL_COMMANDS = frozenset({
    "restart",
    "shutdown",
    "rename",
    "gpupdate",
    "install_app",
    "run_script",
    "install_driver",
    "update_bios",
}) | (SESSION_CONTROL_COMMANDS | SCHEDULED_COMMANDS | REMOTE_CONTROL_COMMANDS
      | VIRTUAL_DISPLAY_COMMANDS | FIRMWARE_COMMANDS)

# Command lifecycle states.
STATUS_PENDING = "pending"    # queued, not yet handed to an agent
STATUS_CLAIMED = "claimed"    # delivered to the agent, awaiting a result
STATUS_DONE = "done"          # agent reported success
STATUS_FAILED = "failed"      # agent reported failure
STATUS_EXPIRED = "expired"    # TTL elapsed before an agent claimed it

DEFAULT_COMMAND_TTL_SECONDS = 15 * 60
# A machine is "online" if we've heard from it within this window. Heartbeats and
# ordinary temp reports both refresh last_seen, so this is really "seconds since
# any contact". Kept a bit above the agent's report cadence so a single missed
# report doesn't flap the status.
DEFAULT_OFFLINE_AFTER_SECONDS = 90


# ================================
# AUDIT LEVELS
# ================================
# Every audit row carries a level, and the level decides who may READ it: the Audit Log tab
# shows info + notice to anyone with `view_audit_log`, and security rows only to holders of
# `view_security_audit` as well (permissions.py). The split is about sensitivity of the
# RECORD, not about how much the action matters -- a failed hub backup is operationally
# louder than a permission-group edit, but only one of them tells you how to attack the hub.
LEVEL_INFO = "info"          # routine bookkeeping, mostly written by agents or the hub itself
LEVEL_NOTICE = "notice"      # an operator changed fleet state or configuration
LEVEL_SECURITY = "security"  # identity, authorization, secrets, code execution, remote access
AUDIT_LEVELS = (LEVEL_INFO, LEVEL_NOTICE, LEVEL_SECURITY)

# Fail CLOSED. An action added later without a mapping, or a caller passing something
# unrecognised, is treated as security-sensitive: briefly invisible to ordinary auditors is
# a much cheaper mistake than silently exposing the next backup-key operation to everyone.
DEFAULT_AUDIT_LEVEL = LEVEL_SECURITY

# The level for every action string the hub writes today. Call sites pass `level=`
# explicitly; this is the fallback that makes a levelless row impossible, and the map the
# one-time backfill of pre-level history is derived from -- so it must stay complete.
ACTION_LEVELS = {
    # -- security: grants or reveals credentials, runs code as SYSTEM, opens a session,
    # or changes who can do any of that.
    "enroll": LEVEL_SECURITY,
    "revoke_agent": LEVEL_SECURITY,
    "issue_command": LEVEL_SECURITY,
    "create_deployment": LEVEL_SECURITY,
    "retry_deployment": LEVEL_SECURITY,
    "remote_session_start": LEVEL_SECURITY,
    "remote_session_end": LEVEL_SECURITY,
    "remote_turn_secret_set": LEVEL_SECURITY,
    # Installing the virtual display puts a third-party driver into the DriverStore and its
    # publisher into this machine's certificate store. That is an expansion of what the
    # machine trusts, so it is recorded at the same level as running code as SYSTEM.
    "virtual_display_install": LEVEL_SECURITY,
    "virtual_display_uninstall": LEVEL_SECURITY,
    "virtual_display_payload_set": LEVEL_SECURITY,
    # Firmware. Recorded SEPARATELY from the `issue_command` row the same request writes,
    # because that one audits params verbatim and the params are just a change id -- the
    # attribute names and values an operator actually chose would appear nowhere. The BIOS
    # setup password is deliberately not part of either row; it never travels in params.
    "bios_settings_change": LEVEL_SECURITY,
    "bios_password_set": LEVEL_SECURITY,
    "bios_password_clear": LEVEL_SECURITY,
    "backup_key_create": LEVEL_SECURITY,
    "backup_key_reveal": LEVEL_SECURITY,
    "backup_key_escrowed": LEVEL_SECURITY,
    "backup_restore_start": LEVEL_SECURITY,
    "permission_group.create": LEVEL_SECURITY,
    "permission_group.update": LEVEL_SECURITY,
    "permission_group.delete": LEVEL_SECURITY,
    # Settings are security-level because hub.auto_update decides whether this hub pulls
    # and runs new code, and the retention keys decide how much history survives.
    "settings.update": LEVEL_SECURITY,
    "settings.reset": LEVEL_SECURITY,
    # -- notice: an operator changed fleet state or configuration.
    # Changing modes only reconfigures an already-trusted driver; it grants nothing new.
    "virtual_display_mode": LEVEL_NOTICE,
    "machine.merge": LEVEL_NOTICE,
    "machine.delete": LEVEL_NOTICE,
    "machine.primary_sensor": LEVEL_NOTICE,
    "alert.dismiss": LEVEL_NOTICE,
    # The user directory is a profile list, not access -- permission groups grant that.
    "user.create": LEVEL_NOTICE,
    "user.update": LEVEL_NOTICE,
    "user.delete": LEVEL_NOTICE,
    "create_package": LEVEL_NOTICE,
    "update_package": LEVEL_NOTICE,
    "delete_package": LEVEL_NOTICE,
    "upload_package_file": LEVEL_NOTICE,
    "cancel_deployment": LEVEL_NOTICE,
    "backup_destination_create": LEVEL_NOTICE,
    "backup_destination_update": LEVEL_NOTICE,
    "backup_destination_delete": LEVEL_NOTICE,
    "backup_destination_test": LEVEL_NOTICE,
    "backup_machine_config": LEVEL_NOTICE,
    "backup_schedule_update": LEVEL_NOTICE,
    "backup_files_run": LEVEL_NOTICE,
    "backup_files_run_fleet": LEVEL_NOTICE,
    "backup_files_cancel": LEVEL_NOTICE,
    "backup_files_cancel_fleet": LEVEL_NOTICE,
    # -- info: routine bookkeeping, the consequence of something already audited above.
    "claim_commands": LEVEL_INFO,
    "complete_command": LEVEL_INFO,
    "cancel_command": LEVEL_INFO,
    "create_favorite": LEVEL_INFO,
    "update_favorite": LEVEL_INFO,
    "delete_favorite": LEVEL_INFO,
    "backup_files": LEVEL_INFO,
    "backup_files_discard": LEVEL_INFO,
    "backup_hub_db": LEVEL_INFO,
    "backup_hub_db_failed": LEVEL_INFO,
    "backup_restore": LEVEL_INFO,
    "backup_restore_failed": LEVEL_INFO,
}


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
        # `cwd`: the working directory the agent's persistent shell was left in after a
        # run_script submission, so the terminal can render a real prompt (PS C:\foo>).
        # Added after command_results shipped, so migrate an existing table in place with
        # the PRAGMA-guard idiom used elsewhere (app.py). Nullable -- other command types
        # and pre-3.2 agents simply don't report one.
        result_columns = {r["name"] for r in conn.execute("PRAGMA table_info(command_results)")}
        if "cwd" not in result_columns:
            conn.execute("ALTER TABLE command_results ADD COLUMN cwd TEXT")
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
        # Saved commands/scripts, per operator. Not machine-scoped: a favorite is a
        # reusable template ("fix the print spooler"), and scoping it to one machine
        # would defeat the point. `shared` opts a favorite into the whole team's list --
        # private by default, because a half-finished script shouldn't be fleet-visible
        # the moment it's saved.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fleet_favorites (
                id           TEXT PRIMARY KEY,
                owner_email  TEXT NOT NULL,
                name         TEXT NOT NULL,
                command_type TEXT NOT NULL,
                params_json  TEXT NOT NULL,
                shared       INTEGER NOT NULL DEFAULT 0,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_favorites_owner ON fleet_favorites(owner_email)"
        )
        # Unique per OWNER, not globally: two operators may each keep their own
        # "Fix printer spooler" without colliding.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_favorites_owner_name "
            "ON fleet_favorites(owner_email, name)"
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
        # `level` decides who may READ the row (see AUDIT_LEVELS). Nullable rather than
        # NOT NULL DEFAULT: a default would stamp every historic row with one level and
        # make it indistinguishable from a real classification. Readers COALESCE NULL to
        # the fail-closed default, so an unlevelled row is hidden, never leaked.
        audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_log)")}
        if "level" not in audit_columns:
            conn.execute("ALTER TABLE audit_log ADD COLUMN level TEXT")
        # One-time backfill of history, from the same map new writes fall back to. Guarded
        # by a cheap EXISTS so a hub with a large audit_log doesn't rescan it every boot.
        if conn.execute("SELECT 1 FROM audit_log WHERE level IS NULL LIMIT 1").fetchone():
            for action, level in ACTION_LEVELS.items():
                conn.execute("UPDATE audit_log SET level=? WHERE level IS NULL AND action=?",
                             (level, action))
            conn.execute("UPDATE audit_log SET level=? WHERE level IS NULL",
                         (DEFAULT_AUDIT_LEVEL,))
        # (ts, id) is scanned in reverse to satisfy both "ORDER BY ts DESC, id DESC" and
        # the keyset cursor without a sort; the other two serve the actor filter and the
        # level perimeter, which is the predicate that rejects the most rows for an
        # operator without the security capability.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts_id ON audit_log(ts, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor_ts ON audit_log(actor, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_level_ts ON audit_log(level, ts)")


def _normalize_level(level, action):
    """The level to store: what the caller asked for, else what the action maps to.

    Never returns None or an unknown value -- a levelless row would be unreadable by
    everyone (readers COALESCE to security) or, worse, readable by the wrong people.
    """
    text = str(level or "").strip().lower()
    if text in AUDIT_LEVELS:
        return text
    return ACTION_LEVELS.get(str(action), DEFAULT_AUDIT_LEVEL)


def audit(db_path, actor, action, target=None, detail=None, level=None):
    """Record one line in the append-only audit trail. Never raises on a bad
    detail payload -- auditing must not be able to break the action it records.

    `level` is one of AUDIT_LEVELS and decides who can read the row back; omit it and
    ACTION_LEVELS decides, so adding an audit call can never produce an unclassified row.
    """
    try:
        detail_json = json.dumps(detail, sort_keys=True) if detail is not None else None
    except (TypeError, ValueError):
        detail_json = json.dumps({"_unserializable": str(detail)})
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO audit_log(ts, actor, action, target, detail_json, level) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), str(actor), str(action), target, detail_json,
             _normalize_level(level, action)),
        )


def _decode_audit_row(row):
    d = {"id": row["id"], "ts": row["ts"], "actor": row["actor"], "action": row["action"],
         "target": row["target"], "level": row["level"]}
    try:
        d["detail"] = json.loads(row["detail_json"]) if row["detail_json"] else None
    except (TypeError, ValueError):
        # A row written before a serialization fix, or hand-edited. The audit line itself
        # is still evidence; losing the whole page over its payload would not be.
        d["detail"] = None
    return d


def _audit_level_clause(levels):
    """(sql, params) restricting to `levels`, or (None, None) for no restriction.

    Returns the sentinel ("", None) when the allowed set is empty -- callers must treat
    that as "return nothing", never as "no filter".
    """
    if levels is None:
        return None, None
    allowed = [lv for lv in AUDIT_LEVELS if lv in set(levels)]
    if not allowed:
        return "", None
    placeholders = ",".join("?" for _ in allowed)
    return (f"COALESCE(level, ?) IN ({placeholders})", [DEFAULT_AUDIT_LEVEL] + allowed)


def _like_needle(text):
    """A LIKE pattern matching `text` literally. Without escaping, an operator searching
    for "100%" would match everything after "100"."""
    escaped = str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_audit(db_path, q=None, actor=None, action=None, since=None, until=None,
               levels=None, before_ts=None, before_id=None, limit=50):
    """One page of the audit trail, newest first.

    `levels` is the set of levels the CALLER is allowed to read. It is the security
    perimeter, so it is applied HERE, in SQL -- never by the caller filtering the result,
    and never from anything the client sent. None means unrestricted and is for internal
    callers and tests only.

    `since`/`until` are inclusive epoch seconds. `before_ts`/`before_id` is the cursor from
    a previous page: paging is keyset rather than OFFSET because `ts` is whole seconds and
    bulk operations write many rows within one, so an OFFSET page would duplicate and skip
    rows as new lines land while an operator reads.

    Returns {"entries": [{id, ts, actor, action, target, level, detail}],
             "has_more": bool, "next_cursor": {"ts", "id"} or None}.
    """
    limit = max(1, min(200, int(limit or 50)))
    empty = {"entries": [], "has_more": False, "next_cursor": None}

    clauses, params = [], []
    level_sql, level_params = _audit_level_clause(levels)
    if level_sql == "":
        return empty                      # caller may read nothing at all
    if level_sql:
        clauses.append(level_sql)
        params.extend(level_params)
    if actor:
        clauses.append("actor = ? COLLATE NOCASE")
        params.append(str(actor).strip())
    if action:
        clauses.append("action = ?")
        params.append(str(action).strip())
    if since is not None:
        clauses.append("ts >= ?")
        params.append(int(since))
    if until is not None:
        clauses.append("ts <= ?")
        params.append(int(until))
    if q:
        needle = _like_needle(str(q).strip())
        clauses.append("(actor LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\' "
                       "OR IFNULL(target, '') LIKE ? ESCAPE '\\')")
        params.extend([needle, needle, needle])
    if before_ts is not None and before_id is not None:
        clauses.append("(ts < ? OR (ts = ? AND id < ?))")
        params.extend([int(before_ts), int(before_ts), int(before_id)])

    sql = ("SELECT id, ts, actor, action, target, detail_json, "
           "COALESCE(level, ?) AS level FROM audit_log")
    head = [DEFAULT_AUDIT_LEVEL]
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"

    with get_conn(db_path) as conn:
        # One extra row answers "is there another page?" without a COUNT, which behind a
        # leading-wildcard LIKE would scan the whole table on every keystroke.
        rows = conn.execute(sql, head + params + [limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    entries = [_decode_audit_row(r) for r in rows]
    cursor = ({"ts": entries[-1]["ts"], "id": entries[-1]["id"]}
              if entries and has_more else None)
    return {"entries": entries, "has_more": has_more, "next_cursor": cursor}


def list_audit_actors(db_path, levels=None, limit=200):
    """Distinct actors, for the tab's actor filter. Takes the SAME level perimeter as
    list_audit: an actor who only ever appears in security rows must not be enumerable by
    someone who cannot read those rows."""
    limit = max(1, min(1000, int(limit or 200)))
    level_sql, level_params = _audit_level_clause(levels)
    if level_sql == "":
        return []
    sql = "SELECT DISTINCT actor FROM audit_log"
    params = []
    if level_sql:
        sql += " WHERE " + level_sql
        params.extend(level_params)
    sql += " ORDER BY actor COLLATE NOCASE LIMIT ?"
    with get_conn(db_path) as conn:
        return [r["actor"] for r in conn.execute(sql, params + [limit]).fetchall()]


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
    audit(db_path, actor=f"agent:{machine}", action="enroll",
          level=LEVEL_SECURITY, target=machine,
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
    audit(db_path, actor=actor, action="revoke_agent",
          level=LEVEL_SECURITY, target=agent_id)


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


def delete_machine(db_path, machine):
    """Remove all fleet rows for a machine: its agent enrollments, its queued/past
    commands, and the command_results / command_output_chunks tied to those commands.
    The append-only audit_log is intentionally left intact (the hub audits the deletion
    itself). Called when an operator hard-deletes a decommissioned machine."""
    machine = str(machine or "").strip()
    if not machine:
        return
    with get_conn(db_path) as conn:
        cmd_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM commands WHERE machine = ?", (machine,)
        ).fetchall()]
        if cmd_ids:
            placeholders = ",".join("?" for _ in cmd_ids)
            conn.execute(
                f"DELETE FROM command_results WHERE command_id IN ({placeholders})", cmd_ids
            )
            conn.execute(
                f"DELETE FROM command_output_chunks WHERE command_id IN ({placeholders})", cmd_ids
            )
        conn.execute("DELETE FROM commands WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM agents WHERE machine = ?", (machine,))


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
    audit(db_path, actor=issued_by, action="issue_command",
          level=LEVEL_SECURITY, target=machine,
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


def expire_stale_commands(db_path, now=None):
    """Sweep expired commands across EVERY machine. Returns how many were retired.

    _expire_stale above only runs when an agent polls, which means expiry is lazy: a
    command for a machine that never comes back stays `pending` forever, and the console
    shows a queued command that will never run. That was survivable while a human read
    the list, but the deployment scheduler (packages.py) waits on a command reaching a
    terminal state -- so a target aimed at an offline machine would never fail, never
    retry, and never give up. This gives expiry a heartbeat of its own.
    """
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE commands SET status = ? WHERE status = ? AND expires_at < ?",
            (STATUS_EXPIRED, STATUS_PENDING, int(now)),
        )
        return cur.rowcount or 0


def cancel_command_if_pending(db_path, command_id):
    """Retire a command, but ONLY while it is still pending (unclaimed). Returns True if
    it was pending and is now expired, False if an agent had already claimed it.

    This is the honest half of "cancel". Claiming is at-most-once and there is no channel
    to recall a command an agent already holds -- so a caller that wants to stop work must
    know whether it actually prevented the work or merely arrived too late. The conditional
    UPDATE is the whole point: it closes the race with claim_commands (which flips pending
    -> claimed in its own transaction) rather than reading-then-writing.
    """
    now = int(time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE commands SET status = ? WHERE id = ? AND status = ?",
            (STATUS_EXPIRED, str(command_id), STATUS_PENDING),
        )
        pending = (cur.rowcount or 0) == 1
    if pending:
        audit(db_path, actor="hub", action="cancel_command",
              level=LEVEL_INFO, target=str(command_id))
    return pending


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
            "SELECT id, type, params_json, issued_by "
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
                # The agent keys each operator's persistent shell on this. It comes from
                # the trusted session (create_command's issued_by), never a client body,
                # which is what stops one operator reaching another's shell session.
                "issued_by": row["issued_by"],
            })
    if claimed:
        audit(db_path, actor=f"agent:{agent_id}", action="claim_commands",
              level=LEVEL_INFO, target=machine,
              detail={"command_ids": [c["id"] for c in claimed]})
    return claimed


def complete_command(db_path, command_id, agent_id, success, output=None, cwd=None):
    """Record an agent's result for a command and move it to done/failed. Rejects
    a result for a command that wasn't claimed by this agent, so one agent can't
    close out another's command.

    `cwd` is the working directory the agent's persistent shell was left in (run_script
    only); other commands report None and the terminal falls back to its last known cwd.
    """
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
            "INSERT OR REPLACE INTO command_results"
            "(command_id, agent_id, success, output, completed_at, cwd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (command_id, str(agent_id), 1 if success else 0,
             None if output is None else str(output), now,
             None if cwd is None else str(cwd)),
        )
        conn.execute(
            "UPDATE commands SET status = ? WHERE id = ?",
            (STATUS_DONE if success else STATUS_FAILED, command_id),
        )
        machine = row["machine"]
    audit(db_path, actor=f"agent:{agent_id}", action="complete_command",
          level=LEVEL_INFO, target=machine,
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
# to outlive an operator watching the terminal. The live value is operator-settable
# (data.command_output_retention_seconds); this is the fallback default, kept here so
# fleet.py stays free of any settings dependency -- callers pass the cutoff in.
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
            "SELECT success, output, completed_at, cwd FROM command_results WHERE command_id = ?",
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
            "SELECT success, output, completed_at, cwd FROM command_results WHERE command_id = ?",
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


# ================================
# FAVORITES (saved commands / scripts)
# ================================
FAVORITE_NAME_MAX_CHARS = 120


def _favorite_row(row):
    fav = dict(row)
    fav["params"] = json.loads(fav.pop("params_json"))
    fav["shared"] = bool(fav["shared"])
    return fav


def _validate_favorite(name, command_type, params):
    """Shared by create/update. Mirrors create_command's type+params rules, so a
    favorite can never store something the command endpoint would reject."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > FAVORITE_NAME_MAX_CHARS:
        raise ValueError(f"name must be {FAVORITE_NAME_MAX_CHARS} characters or fewer")
    if command_type not in ALL_COMMANDS:
        raise ValueError(f"unknown command type: {command_type!r}")
    if command_type in SESSION_CONTROL_COMMANDS:
        # These steer a live shell session; there is nothing reusable to save.
        raise ValueError(f"{command_type!r} commands cannot be saved as a favorite")
    if command_type in SCHEDULED_COMMANDS:
        # The scheduler owns these -- a saved copy would carry a stale deployment id.
        raise ValueError(f"{command_type!r} commands are issued by the deployment "
                         f"scheduler and cannot be saved as a favorite")
    if command_type in REMOTE_CONTROL_COMMANDS:
        # These carry a one-shot session id + single-use TURN credentials; a saved copy
        # would point at a dead session.
        raise ValueError(f"{command_type!r} commands start a live remote session and "
                         f"cannot be saved as a favorite")
    if command_type in VIRTUAL_DISPLAY_COMMANDS:
        # install_virtual_display pins a payload DIGEST captured when the operator clicked;
        # a favorite saved today would still be pinning it after the driver is re-uploaded,
        # and would then fail the download's hash check on every machine it was replayed
        # against. The other two ride along so the Remote tab stays the single way in.
        raise ValueError(f"{command_type!r} commands are issued from the Remote tab and "
                         f"cannot be saved as a favorite")
    if command_type in UNSAVEABLE_FIRMWARE_COMMANDS:
        # Carries a change id belonging to one machine and one already-issued set of writes.
        raise ValueError(f"{command_type!r} commands are issued from the Firmware tab and "
                         f"cannot be saved as a favorite")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return name, params


def list_favorites(db_path, email):
    """This operator's own favorites plus anything a teammate marked shared, newest
    first. `owned` tells the console which rows this user may edit or delete."""
    email = str(email or "").strip()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, owner_email, name, command_type, params_json, shared, "
            "       created_at, updated_at "
            "FROM fleet_favorites WHERE owner_email = ? OR shared = 1 "
            "ORDER BY updated_at DESC",
            (email,),
        ).fetchall()
    favorites = []
    for row in rows:
        fav = _favorite_row(row)
        fav["owned"] = fav["owner_email"] == email
        favorites.append(fav)
    return favorites


def create_favorite(db_path, email, name, command_type, params, shared=False):
    """Save a command as a favorite for `email`. Returns its id."""
    email = str(email or "").strip()
    if not email:
        raise ValueError("owner email is required")
    name, params = _validate_favorite(name, command_type, params)

    favorite_id = uuid.uuid4().hex
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO fleet_favorites(id, owner_email, name, command_type, "
                "params_json, shared, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (favorite_id, email, name, command_type,
                 json.dumps(params, sort_keys=True), 1 if shared else 0, now, now),
            )
    except sqlite3.IntegrityError:
        # The unique (owner, name) index. Surfaced as ValueError so the HTTP layer
        # answers 400 rather than leaking a driver error as a 500.
        raise ValueError(f"you already have a favorite named {name!r}")
    audit(db_path, actor=email, action="create_favorite", level=LEVEL_INFO, target=name,
          detail={"favorite_id": favorite_id, "type": command_type, "shared": bool(shared)})
    return favorite_id


def update_favorite(db_path, favorite_id, email, name=None, command_type=None,
                    params=None, shared=None):
    """Edit a favorite. Owner-only: a shared favorite is readable by the team but still
    belongs to whoever made it. Only the fields passed are changed."""
    favorite_id = str(favorite_id)
    email = str(email or "").strip()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT owner_email, name, command_type, params_json, shared "
            "FROM fleet_favorites WHERE id = ?",
            (favorite_id,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown favorite")
        if row["owner_email"] != email:
            raise PermissionError("only the owner can change this favorite")

        new_name = row["name"] if name is None else name
        new_type = row["command_type"] if command_type is None else command_type
        new_params = json.loads(row["params_json"]) if params is None else params
        new_name, new_params = _validate_favorite(new_name, new_type, new_params)
        new_shared = row["shared"] if shared is None else (1 if shared else 0)

        try:
            conn.execute(
                "UPDATE fleet_favorites SET name = ?, command_type = ?, params_json = ?, "
                "shared = ?, updated_at = ? WHERE id = ?",
                (new_name, new_type, json.dumps(new_params, sort_keys=True),
                 new_shared, int(time.time()), favorite_id),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"you already have a favorite named {new_name!r}")
    audit(db_path, actor=email, action="update_favorite", level=LEVEL_INFO, target=new_name,
          detail={"favorite_id": favorite_id, "shared": bool(new_shared)})


def delete_favorite(db_path, favorite_id, email):
    favorite_id = str(favorite_id)
    email = str(email or "").strip()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT owner_email, name FROM fleet_favorites WHERE id = ?", (favorite_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown favorite")
        if row["owner_email"] != email:
            raise PermissionError("only the owner can delete this favorite")
        conn.execute("DELETE FROM fleet_favorites WHERE id = ?", (favorite_id,))
    audit(db_path, actor=email, action="delete_favorite",
          level=LEVEL_INFO, target=row["name"],
          detail={"favorite_id": favorite_id})


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
