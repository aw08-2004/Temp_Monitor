"""Remote view/control session state and WebRTC signaling relay (roadmap #2).

This is the hub-side half of "watch and drive a managed PC's screen from the console":
it owns the session lifecycle, relays WebRTC signaling (SDP offer/answer + ICE candidates)
between the operator's browser and the agent's capture helper, and mints the short-lived
TURN credentials both peers use to reach the hub-hosted relay.

Why signaling rides plain authenticated HTTP polling rather than a WebSocket: the agent is
strictly outbound with no listening port and no WebSocket client, and the hub's Socket.IO is
configured polling-only -- so a WebSocket would buy nothing here. Signaling is a small burst of
messages at session setup (one offer, one answer, a handful of trickled ICE candidates); once
ICE completes, media flows peer-to-peer or via TURN and never touches this path again. Both
sides poll with an `after_seq` cursor, exactly like the fleet terminal's scrollback.

Trust model (enforced in remote_web.py, same two planes as fleet_web.py):
  * The console side is gated on the `remote_control` capability + the target machine being in
    the operator's scope, and every session start/stop is audited.
  * The agent side is gated on the per-agent bearer token, and a session only accepts agent
    signals from the agent whose machine owns it.
  * TURN credentials are ephemeral (HMAC of a secret in .env, short TTL) and per-session, so an
    agent never holds a long-lived relay credential -- the same "secrets live in .env, the hub
    mints scoped access on demand" discipline the backup pre-signed URLs use.

Kept free of Flask so it can be unit-tested in isolation; remote_web.py wires thin HTTP
endpoints on top.
"""
import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid

import fleet

# ================================
# SESSION LIFECYCLE
# ================================
STATUS_PENDING = "pending"        # created; start_remote_session command queued for the agent
STATUS_CONNECTING = "connecting"  # agent helper is up and has posted its SDP offer
STATUS_ACTIVE = "active"          # media connected (reported by a peer)
STATUS_ENDED = "ended"            # ended cleanly (operator closed, agent reported bye)
STATUS_EXPIRED = "expired"        # TTL elapsed

# A session may live this long by default before it must be restarted. This bounds how long a
# minted TURN credential and an open capture helper stay valid; it is NOT the command TTL (how
# long the start command waits to be claimed -- that stays fleet's short default).
DEFAULT_SESSION_TTL_SECONDS = 4 * 60 * 60

# Signaling caps. One SDP is a few KB; an ICE candidate is tiny. These bound a misbehaving or
# hostile peer from filling the table -- generous enough that real trickle-ICE never hits them.
MAX_SIGNAL_BYTES = 64 * 1024
MAX_SIGNALS_PER_SESSION = 500

# Who a signal came from. The poller always receives the OTHER side's signals.
SENDER_AGENT = "agent"
SENDER_CONSOLE = "console"
_SENDERS = frozenset({SENDER_AGENT, SENDER_CONSOLE})

# What a signal carries. offer/answer are SDP; ice is a trickled candidate; bye tears down.
SIGNAL_KINDS = frozenset({"offer", "answer", "ice", "bye"})


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_remote_db(db_path):
    """Create the remote-session tables if absent. Idempotent -- safe to call on every hub
    start next to app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_sessions (
                id           TEXT PRIMARY KEY,
                machine      TEXT NOT NULL,
                issued_by    TEXT NOT NULL,
                consent_mode TEXT NOT NULL,
                status       TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                expires_at   INTEGER NOT NULL,
                ended_at     INTEGER,
                ended_reason TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_remote_sessions_machine "
            "ON remote_sessions(machine, status)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_signals (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                sender     TEXT NOT NULL,
                kind       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_remote_signals_session "
            "ON remote_signals(session_id, id)"
        )


def _row_to_session(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "machine": row["machine"],
        "issued_by": row["issued_by"],
        "consent_mode": row["consent_mode"],
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "ended_at": row["ended_at"],
        "ended_reason": row["ended_reason"],
    }


def create_session(db_path, machine, issued_by, consent_mode,
                   ttl_seconds=DEFAULT_SESSION_TTL_SECONDS):
    """Open a remote session for `machine`. Returns its id. Authorization happened upstream at
    the console session gate (remote_control capability + machine scope)."""
    machine = str(machine or "").strip()
    if not machine:
        raise ValueError("machine is required")
    consent_mode = str(consent_mode or "unattended").strip() or "unattended"
    session_id = uuid.uuid4().hex
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO remote_sessions(id, machine, issued_by, consent_mode, status, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, machine, str(issued_by), consent_mode, STATUS_PENDING,
             now, now + int(ttl_seconds)),
        )
    fleet.audit(db_path, actor=issued_by, action="remote_session_start", target=machine,
                detail={"session_id": session_id, "consent_mode": consent_mode})
    return session_id


def get_session(db_path, session_id):
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM remote_sessions WHERE id = ?", (str(session_id),)
        ).fetchone()
    return _row_to_session(row)


def list_sessions(db_path, machine=None, active_only=False):
    """Sessions for a machine (or all), newest first. `active_only` filters to the live
    states, which is what the console shows as 'currently being viewed'."""
    clauses, params = [], []
    if machine is not None:
        clauses.append("machine = ?")
        params.append(str(machine).strip())
    if active_only:
        clauses.append("status IN (?, ?, ?)")
        params.extend([STATUS_PENDING, STATUS_CONNECTING, STATUS_ACTIVE])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM remote_sessions{where} ORDER BY created_at DESC", params
        ).fetchall()
    return [_row_to_session(r) for r in rows]


def _is_live(status):
    return status in (STATUS_PENDING, STATUS_CONNECTING, STATUS_ACTIVE)


def mark_status(db_path, session_id, status):
    """Advance a session's status (pending -> connecting -> active). A no-op on an already
    ended/expired session, so a late report can't reopen a closed session."""
    if status not in (STATUS_PENDING, STATUS_CONNECTING, STATUS_ACTIVE):
        raise ValueError(f"invalid status {status!r}")
    now = int(time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE remote_sessions SET status = ? "
            "WHERE id = ? AND status IN (?, ?, ?) AND expires_at > ?",
            (status, str(session_id), STATUS_PENDING, STATUS_CONNECTING, STATUS_ACTIVE, now),
        )
        return (cur.rowcount or 0) == 1


def end_session(db_path, session_id, reason, actor="hub"):
    """Terminate a session. Returns True if it was live and is now ended. Idempotent -- ending
    an already-ended session returns False without a second audit line."""
    now = int(time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE remote_sessions SET status = ?, ended_at = ?, ended_reason = ? "
            "WHERE id = ? AND status IN (?, ?, ?)",
            (STATUS_ENDED, now, str(reason)[:200], str(session_id),
             STATUS_PENDING, STATUS_CONNECTING, STATUS_ACTIVE),
        )
        ended = (cur.rowcount or 0) == 1
        if ended:
            row = conn.execute(
                "SELECT machine FROM remote_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
    if ended:
        fleet.audit(db_path, actor=actor, action="remote_session_end",
                    target=row["machine"] if row else str(session_id),
                    detail={"session_id": str(session_id), "reason": str(reason)[:200]})
    return ended


def expire_sessions(db_path, now=None):
    """Sweep sessions past their TTL to expired, across every machine. Returns how many were
    retired. Gives session expiry a heartbeat of its own, the same way
    fleet.expire_stale_commands does for commands, so a browser tab that vanished without a
    clean 'stop' doesn't leave a session (and its TURN credential) live forever."""
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE remote_sessions SET status = ?, ended_at = ?, ended_reason = ? "
            "WHERE status IN (?, ?, ?) AND expires_at <= ?",
            (STATUS_EXPIRED, int(now), "ttl expired",
             STATUS_PENDING, STATUS_CONNECTING, STATUS_ACTIVE, int(now)),
        )
        return cur.rowcount or 0


# ================================
# SIGNALING RELAY
# ================================
def add_signal(db_path, session_id, sender, kind, payload):
    """Store one signaling message for the other side to poll. Returns its seq (the row id).

    Refuses signals on a session that isn't live, an unknown sender/kind, an oversized payload,
    or a session already at the per-session signal cap -- each of which is either a bug or an
    abuse rather than legitimate trickle ICE.
    """
    session_id = str(session_id)
    if sender not in _SENDERS:
        raise ValueError(f"unknown sender {sender!r}")
    if kind not in SIGNAL_KINDS:
        raise ValueError(f"unknown signal kind {kind!r}")
    payload_json = json.dumps(payload, separators=(",", ":"))
    if len(payload_json) > MAX_SIGNAL_BYTES:
        raise ValueError("signal payload too large")

    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, expires_at FROM remote_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown session")
        if not _is_live(row["status"]) or row["expires_at"] <= now:
            raise PermissionError("session is not active")
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM remote_signals WHERE session_id = ?", (session_id,)
        ).fetchone()["n"]
        if count >= MAX_SIGNALS_PER_SESSION:
            raise PermissionError("signal limit reached for this session")
        cur = conn.execute(
            "INSERT INTO remote_signals(session_id, sender, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, sender, kind, payload_json, now),
        )
        return cur.lastrowid


def get_signals(db_path, session_id, for_sender, after_seq=0):
    """Return the signals the caller hasn't seen yet -- i.e. those from the OTHER side, with
    seq > after_seq -- plus a `next_seq` cursor to pass next time.

    `for_sender` is who is polling: the agent receives console signals (answer + ICE), the
    console receives agent signals (offer + ICE). This is the whole relay: each side writes with
    add_signal(sender=itself) and reads with get_signals(for_sender=itself).
    """
    if for_sender not in _SENDERS:
        raise ValueError(f"unknown sender {for_sender!r}")
    other = SENDER_CONSOLE if for_sender == SENDER_AGENT else SENDER_AGENT
    with get_conn(db_path) as conn:
        if get_session(db_path, session_id) is None:
            raise KeyError("unknown session")
        rows = conn.execute(
            "SELECT id, sender, kind, payload, created_at FROM remote_signals "
            "WHERE session_id = ? AND sender = ? AND id > ? ORDER BY id ASC",
            (str(session_id), other, int(after_seq)),
        ).fetchall()
    signals = [
        {"seq": r["id"], "sender": r["sender"], "kind": r["kind"],
         "payload": json.loads(r["payload"]), "created_at": r["created_at"]}
        for r in rows
    ]
    next_seq = signals[-1]["seq"] if signals else int(after_seq)
    return {"signals": signals, "next_seq": next_seq}


# ================================
# TURN CREDENTIALS
# ================================
def mint_turn_credentials(secret, session_id, ttl_seconds=600):
    """Mint an ephemeral TURN credential using the standard coturn/pion REST scheme
    (draft-uberti-behave-turn-rest): username = '<expiry-unix>:<session-id>', password =
    base64(HMAC-SHA1(secret, username)). The TURN server validates the same HMAC with the
    shared secret, so the hub can hand out scoped, expiring credentials without the TURN server
    holding a per-user database. The session id is baked into the username so a leaked
    credential is traceable and dies with the TTL.
    """
    if not secret:
        raise ValueError("TURN secret is not configured")
    expiry = int(time.time()) + int(ttl_seconds)
    username = f"{expiry}:{session_id}"
    digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
    password = base64.b64encode(digest).decode("ascii")
    return {"username": username, "password": password, "expiry": expiry}


def ice_servers(session_id, stun_urls=None, turn_urls=None, turn_secret=None, turn_ttl=600):
    """Build the ICE server list handed to both peers. STUN servers need no credential; TURN
    servers get a freshly minted ephemeral credential. Pure and config-driven -- remote_web.py
    supplies the URLs from settings and the secret from .env -- so an empty/unconfigured TURN
    simply yields whatever STUN is set (or nothing, which still works on a LAN via host
    candidates).
    """
    servers = []
    for url in (stun_urls or []):
        url = str(url).strip()
        if url:
            # urls is always a list, even for a single STUN server -- both the browser's
            # RTCIceServer and the agent's parser accept a list, and one shape is simpler
            # than two on both consumers.
            servers.append({"urls": [url]})
    if turn_urls and turn_secret:
        cred = mint_turn_credentials(turn_secret, session_id, turn_ttl)
        urls = [str(u).strip() for u in turn_urls if str(u).strip()]
        if urls:
            servers.append({
                "urls": urls,
                "username": cred["username"],
                "credential": cred["password"],
            })
    return servers
