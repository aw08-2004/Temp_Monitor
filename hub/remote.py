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
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
import uuid

import envfile
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_inventory (
                machine       TEXT PRIMARY KEY,
                sessions_json TEXT NOT NULL,
                displays_json TEXT NOT NULL,
                reported_at   INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS virtual_display_payload (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                version     TEXT NOT NULL,
                sha256      TEXT NOT NULL,
                filename    TEXT NOT NULL,
                uploaded_by TEXT NOT NULL,
                uploaded_at INTEGER NOT NULL
            )
            """
        )


# --------------------------------------------------------------------------- inventory
# What a remote session would find on a machine: which logon sessions exist, and whether
# there is anything to capture. Reported on the heartbeat (change-detected agent-side), and
# consumed by the session switcher and the "no display outputs" badge.
#
# This lives here rather than in its own module because it is remote-control data with no
# other consumer -- the same reasoning that keeps the signal relay in this file.

#: Cap on stored logon sessions. A machine with more than this has a problem, not a need.
MAX_REPORTED_SESSIONS = 32


def record_inventory(db_path, machine, payload):
    """Store the agent's reported logon sessions and display outputs.

    Written from the heartbeat, so unlike everything else here it is not operator input: it is
    trimmed and type-checked before it lands in the database, and a malformed payload is
    dropped rather than raised. A stale badge is an acceptable cost; a heartbeat that 500s
    because a machine sent something odd is not -- that machine would go "offline" fleet-wide.

    Returns True if something was stored.
    """
    if not isinstance(payload, dict):
        return False

    sessions = []
    for raw in list(payload.get("sessions") or [])[:MAX_REPORTED_SESSIONS]:
        if not isinstance(raw, dict):
            continue
        try:
            session_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        sessions.append({
            "id": session_id,
            "user": str(raw.get("user") or "")[:128],
            "domain": str(raw.get("domain") or "")[:128],
            "account": str(raw.get("account") or "")[:260],
            "station": str(raw.get("station") or "")[:64],
            "client": str(raw.get("client") or "")[:128],
            "state": str(raw.get("state_name") or "")[:32],
            "is_console": bool(raw.get("is_console")),
            "is_logon_screen": bool(raw.get("is_logon_screen")),
        })

    raw_displays = payload.get("displays")
    displays = {}
    if isinstance(raw_displays, dict):
        def _int(key, default=0):
            try:
                return int(raw_displays.get(key, default))
            except (TypeError, ValueError):
                return default
        displays = {
            "physical_monitors": _int("physical_monitors"),
            "active_outputs": _int("active_outputs", -1),
            "virtual_display_present": bool(raw_displays.get("virtual_display_present")),
            "virtual_display_started": bool(raw_displays.get("virtual_display_started")),
            "headless": bool(raw_displays.get("headless")),
            "output_names": [str(n)[:128]
                             for n in list(raw_displays.get("output_names") or [])[:16]],
        }

    if not sessions and not displays:
        return False

    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO remote_inventory(machine, sessions_json, displays_json, reported_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET sessions_json = excluded.sessions_json, "
            "displays_json = excluded.displays_json, reported_at = excluded.reported_at",
            (machine, json.dumps(sessions), json.dumps(displays), int(time.time())),
        )
    return True


def get_inventory(db_path, machine):
    """The last reported sessions/displays for `machine`, or empty defaults if it has never
    reported (an agent older than this feature, or one that has not heartbeated yet)."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT sessions_json, displays_json, reported_at FROM remote_inventory "
            "WHERE machine = ?", (machine,)
        ).fetchone()
    if row is None:
        return {"sessions": [], "displays": {}, "reported_at": None}
    try:
        sessions = json.loads(row["sessions_json"])
        displays = json.loads(row["displays_json"])
    except (TypeError, ValueError):
        sessions, displays = [], {}
    return {"sessions": sessions, "displays": displays, "reported_at": row["reported_at"]}


# --------------------------------------------------------------------------- VDD payload
def set_virtual_display_payload(db_path, version, sha256, filename, uploaded_by):
    """Pin which uploaded package blob is the virtual display driver.

    Only a pointer is stored. The bytes live in the existing package blob store, and agents
    fetch them through the existing authenticated, digest-verified package channel -- so this
    feature adds no new download path, no second signed artifact, and nothing to the agent's
    own update manifest.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO virtual_display_payload(id, version, sha256, filename, uploaded_by, "
            "uploaded_at) VALUES (1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "sha256 = excluded.sha256, filename = excluded.filename, "
            "uploaded_by = excluded.uploaded_by, uploaded_at = excluded.uploaded_at",
            (version, sha256, filename, uploaded_by, int(time.time())),
        )


def get_virtual_display_payload(db_path):
    """The pinned driver payload, or None if an admin has not uploaded one yet."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT version, sha256, filename, uploaded_by, uploaded_at "
            "FROM virtual_display_payload WHERE id = 1"
        ).fetchone()
    return dict(row) if row else None


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
    fleet.audit(db_path, actor=issued_by, action="remote_session_start",
                level=fleet.LEVEL_SECURITY, target=machine,
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
                    level=fleet.LEVEL_SECURITY,
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


def generate_turn_secret(nbytes=24):
    """A fresh, strong TURN shared secret (hex). Matches the installer's New-RandomSecret shape
    so a secret minted here and one minted by the installer are interchangeable."""
    return secrets.token_hex(nbytes)


def set_env_var(env_path, key, value):
    """Upsert `KEY=value` in a dotenv file. Kept as remote.py's own name because the TURN
    secret control calls it, but the implementation now lives in envfile.py -- several
    features write .env, and the BOM trap it encodes is worth having in exactly one place.
    Returns the value written."""
    return envfile.set_var(env_path, key, value)


# --------------------------------------------------------------------------- vantage
# Which of the configured relay URLs a given peer can actually reach.
#
# The hub publishes up to three TURN URLs for one relay (see install.ps1 New-TurnUrlList):
# the public listener by its public hostname, the same listener by the hub's LAN address, and
# a second listener bound to the LAN address that does NOT rewrite relay candidates. The URLs
# with a PRIVATE IP literal for a host only mean anything to a peer sitting inside that
# network; to a machine out on the internet they are unroutable, and every allocation against
# them is a few seconds of ICE gathering spent on a candidate that can never appear.
#
# The hub already knows where each peer is: it is the source address of the peer's own request
# (the console's at /start, the agent's at /ice). So each side is handed the URLs that make
# sense from where it is standing, rather than the union that only ever made sense for one of
# them.
#
# The rule is deliberately one-directional -- it only ever DROPS URLs a peer provably cannot
# reach, and never drops the last one -- because the cost of being wrong in the other direction
# is a session that cannot connect at all.

def ice_url_host(url):
    """The host out of a `turn:host:port?transport=tcp` or `stun:host:port` URL (or the `[v6]`
    bracketed form). '' if there is nothing after the scheme."""
    rest = str(url or "").partition(":")[2].strip()
    rest = rest.split("?", 1)[0]
    if rest.startswith("["):
        return rest[1:].partition("]")[0]
    return rest.partition(":")[0]


def _ip_or_none(text):
    try:
        return ipaddress.ip_address(str(text).strip())
    except ValueError:
        return None


def is_lan_address(text):
    """True if `text` is an IP LITERAL that is only meaningful inside some private network.

    A hostname answers False: it may well resolve to a private address, but the hub cannot know
    that, and guessing wrong would drop the one URL a peer could have used.
    """
    ip = _ip_or_none(text)
    if ip is None:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def select_urls_for_peer(urls_in, peer_ip=None):
    """Order (and where provable, narrow) a list of STUN/TURN URLs for a peer seen coming
    from `peer_ip`.

      * peer on a public address -> drop the private-literal URLs. It cannot route to them; all
        they buy is gathering time spent on allocations that will time out.
      * peer on a private address -> keep everything, LAN URLs first. It may be on the hub's own
        network (where the LAN listener is the only URL that yields a usable relay candidate) or
        on some OTHER private network reached through NAT, where only the public URL works -- and
        nothing in the request distinguishes the two, so both stay on the list.
      * unknown peer -> unchanged, which is what every caller did before this existed.

    Never returns an empty list for a non-empty input.
    """
    urls = [str(u).strip() for u in (urls_in or []) if str(u).strip()]
    if not urls:
        return []
    ip = _ip_or_none(peer_ip)
    if ip is None:
        return urls
    lan = [u for u in urls if is_lan_address(ice_url_host(u))]
    lan_set = set(lan)
    wan = [u for u in urls if u not in lan_set]
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return lan + wan
    return wan or urls


def ice_servers(session_id, stun_urls=None, turn_urls=None, turn_secret=None, turn_ttl=600,
                peer_ip=None):
    """Build the ICE server list handed to one peer. STUN servers need no credential; TURN
    servers get a freshly minted ephemeral credential. Pure and config-driven -- remote_web.py
    supplies the URLs from settings and the secret from .env -- so an empty/unconfigured TURN
    simply yields whatever STUN is set (or nothing, which still works on a LAN via host
    candidates).

    `peer_ip` is the source address of the peer this list is for; see select_urls_for_peer.
    """
    servers = []
    for url in select_urls_for_peer(stun_urls, peer_ip):
        if url:
            # urls is always a list, even for a single STUN server -- both the browser's
            # RTCIceServer and the agent's parser accept a list, and one shape is simpler
            # than two on both consumers.
            servers.append({"urls": [url]})
    if turn_urls and turn_secret:
        urls = select_urls_for_peer(turn_urls, peer_ip)
        if urls:
            cred = mint_turn_credentials(turn_secret, session_id, turn_ttl)
            servers.append({
                "urls": urls,
                "username": cred["username"],
                "credential": cred["password"],
            })
    return servers
