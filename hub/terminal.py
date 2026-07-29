"""Interactive terminal sessions (ConPTY) between an operator's browser and an agent.

WHY THIS IS NOT PART OF fleet.py's COMMAND OUTPUT
-------------------------------------------------
A fleet command is a discrete unit of work: it is issued, it runs, it produces a bounded
amount of output and a result that is worth keeping. `command_output_chunks` is shaped for
exactly that -- capped at 256 KB / 2000 chunks per command, after which the agent is told to
stop streaming, because output past that point is nobody's idea of a useful record.

A terminal session is a STREAM. It has no output worth keeping as a record (the durable
answer to "who opened a shell on which machine" is the `shell_open` command's audit row,
which fleet.py already writes) and no bounded size. Running it through the command-output
tables would hit the cap in a couple of minutes of ordinary typing and then silently go
dead. So it gets its own storage with the opposite policy: a ROLLING window, oldest bytes
dropped, deleted when the session ends.

A SESSION OUTLIVES THE PAGE THAT OPENED IT
------------------------------------------
Deliberately. An operator kicks off a download, goes to Packages to do something else, and
comes back expecting to find their shell where they left it -- with its working directory,
its variables, and the output that arrived while they were away. So closing the machine
page does NOT end the session: the console re-attaches to it on return, and replays the
buffer above into a fresh terminal to restore the scrollback. The only things that end a
session are the operator asking for a new one, the shell exiting, and the reapers below.

That makes the rolling buffer above do double duty -- it is the re-attach history, not just
slack between two polls -- and it is why abandonment is measured on a clock of its own; see
PTY_ABANDONED_SECONDS.

The other half of the shape is INPUT. Fleet commands only ever flow hub -> agent once, at
claim time; a terminal needs a continuous keystroke channel in the same direction, at
interactive latency. The agent still never opens a port -- it polls `pty_input` rapidly
while (and only while) it has a session open, so this stays as outbound-only as the rest of
the fleet channel.

LATENCY, AND WHY THE NUMBERS ARE WHAT THEY ARE
----------------------------------------------
Echo round-trip is: browser POSTs a keystroke -> agent's input poll picks it up -> the pty
echoes -> agent POSTs output -> browser's output poll renders it. The hub runs under
waitress with a fixed thread pool, so long-polling (holding requests open) is off the table:
a handful of parked terminal sessions would starve every other request. Instead both sides
poll fast and back off hard when idle, which keeps a session's cost proportional to how
actually-interactive it is. See PTY_* tunables below and their agent-side counterparts in
AgentConfig.

AUTHORIZATION
-------------
A session is bound to ONE operator (its `operator` column) as well as one machine. The
console endpoints refuse a session belonging to someone else even when the caller could
have opened their own on that machine -- an operator's terminal is their keystrokes and
their half-typed credentials, and "can issue commands here" is not consent to watch. The
agent endpoints check the session's machine against the authenticated agent's, so one
agent can neither read another's keystrokes nor inject output into its stream.

Kept free of Flask so it can be unit-tested directly; fleet_web.py wires HTTP on top.
"""
import json
import secrets
import time

from fleet import get_conn

# Session lifecycle.
STATUS_OPEN = "open"        # command issued, the agent has not attached yet
STATUS_LIVE = "live"        # the agent has attached and is streaming
STATUS_CLOSING = "closing"  # the console asked to end it; the agent has yet to confirm
STATUS_CLOSED = "closed"    # over, for whatever reason

PTY_SHELLS = ("powershell", "cmd")

# Rolling REPLAY buffer. This is what a returning operator sees: a terminal session
# outlives the page that opened it, so navigating to Packages and back re-attaches to the
# same shell and replays this buffer into a fresh xterm to restore the scrollback. It is
# therefore sized for "the history a human wants back", not merely for the gap between two
# polls -- an operator who kicked off a download and wandered off should return to the
# output it produced while they were gone.
#
# Still a ROLLING buffer, and still not a record: the oldest bytes are dropped once it is
# full, it is never written to disk beyond this table, and it is deleted when the session
# ends. What is worth keeping about a terminal is the audit_log row saying one was opened.
PTY_REPLAY_MAX_CHARS = 256_000
PTY_REPLAY_MAX_CHUNKS = 2_000
PTY_MAX_CHUNK_CHARS = 16_000
# One keystroke post. Generous enough for a pasted block, small enough that a runaway
# client can't push megabytes into a machine's console in one request.
PTY_MAX_INPUT_CHARS = 8_000

# TWO kinds of silence, which used to be conflated and must not be, now that a session
# deliberately outlives the page that opened it:
#
#  * The AGENT went quiet. It polls for keystrokes every 150ms-1s for as long as it holds
#    the session open, so a gap this long means the machine is gone (rebooted, offline,
#    agent crashed) and the session is never coming back.
#  * The CONSOLE went quiet. The operator is expected to disappear for a while -- that is
#    the entire point of persistence -- so this horizon is generous. But an open terminal
#    is a live SYSTEM shell, so "generous" is not "forever": a browser closed on Friday
#    must not leave one running over the weekend.
#
# Keying abandonment on the agent's traffic (the old behaviour) would now never fire at all,
# because the agent's own polling keeps last_activity fresh indefinitely.
PTY_AGENT_SILENT_SECONDS = 15 * 60
PTY_ABANDONED_SECONDS = 60 * 60
# How many sessions one operator may hold open on one machine at once. A terminal is a real
# SYSTEM shell; leaking them by opening tabs should not be possible.
PTY_MAX_SESSIONS_PER_OPERATOR = 4


def init_pty_db(db_path):
    """Create the pty tables if absent. Idempotent; called alongside init_fleet_db."""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pty_sessions (
                id            TEXT PRIMARY KEY,
                machine       TEXT NOT NULL,
                operator      TEXT NOT NULL,
                shell         TEXT NOT NULL,
                cols          INTEGER NOT NULL,
                rows          INTEGER NOT NULL,
                command_id    TEXT,
                status        TEXT NOT NULL,
                created_at    INTEGER NOT NULL,
                last_activity INTEGER NOT NULL,
                last_console_at INTEGER NOT NULL DEFAULT 0,
                closed_at     INTEGER,
                close_reason  TEXT,
                next_input_seq INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # next_input_seq must live on the SESSION, not be derived from MAX(seq) in pty_input,
        # and the reason is subtle enough to be worth stating: pull_input DELETES rows the
        # agent has acked, so the moment the queue drains, MAX(seq) is NULL and a derived
        # counter restarts at 0 -- below the agent's cursor. Every keystroke after the first
        # ack would then be filtered out as "already delivered" and the terminal would go
        # silently deaf, which is exactly the class of bug this feature exists to fix.
        session_columns = {r["name"] for r in conn.execute("PRAGMA table_info(pty_sessions)")}
        if "next_input_seq" not in session_columns:
            conn.execute("ALTER TABLE pty_sessions ADD COLUMN "
                         "next_input_seq INTEGER NOT NULL DEFAULT 0")
        # last_activity is refreshed by the AGENT's keystroke polls, so it says nothing about
        # whether an operator is still watching. This is the console's own clock, and it is
        # what abandonment is measured against. See PTY_ABANDONED_SECONDS.
        if "last_console_at" not in session_columns:
            conn.execute("ALTER TABLE pty_sessions ADD COLUMN "
                         "last_console_at INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pty_sessions_machine "
            "ON pty_sessions(machine, status)"
        )
        # VT output from the agent. `seq` is an agent-owned counter, exactly as for
        # command_output_chunks: PRIMARY KEY + INSERT OR IGNORE makes a retried POST a
        # no-op, so the agent must reuse the same seq on retry rather than allocate a new
        # one (a fresh seq would duplicate bytes into the middle of an escape sequence).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pty_output (
                session_id  TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                chunk       TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                PRIMARY KEY (session_id, seq)
            )
            """
        )
        # Keystrokes and control events from the console, waiting for the agent's next poll.
        # `seq` is hub-owned here (the console has no way to order its own posts against a
        # second tab). `kind` is 'data' (raw bytes the operator typed) or 'resize'.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pty_input (
                session_id TEXT NOT NULL,
                seq        INTEGER NOT NULL,
                kind       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (session_id, seq)
            )
            """
        )


# ================================
# SESSIONS
# ================================
def _row(row):
    return dict(row) if row is not None else None


def normalize_shell(shell):
    shell = str(shell or "powershell").strip().lower()
    if shell in ("batch", "bat", "cmd"):
        return "cmd"
    return "powershell"


def _clamp(value, low, high, fallback):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, value))


def open_session(db_path, machine, operator, shell, cols=120, rows=30):
    """Register a new terminal session and return its row. The caller (fleet_web) then
    issues the `shell_open` command that tells the agent to attach to this id.

    The id is a 32-byte URL-safe token, not a uuid4: it is the only thing in the URL of the
    keystroke and output endpoints, so it should be unguessable rather than merely unique.
    """
    machine = str(machine or "").strip()
    operator = str(operator or "").strip().lower()
    if not machine:
        raise ValueError("machine is required")
    if not operator:
        raise ValueError("operator is required")

    shell = normalize_shell(shell)
    cols = _clamp(cols, 20, 500, 120)
    rows = _clamp(rows, 5, 200, 30)
    now = int(time.time())

    with get_conn(db_path) as conn:
        # Retire anything already stale before counting, so a run of dead tabs can't lock
        # an operator out of their own terminal.
        _expire_idle(conn, now)
        live = conn.execute(
            "SELECT COUNT(*) AS n FROM pty_sessions "
            "WHERE machine = ? AND operator = ? AND status IN (?, ?, ?)",
            (machine, operator, STATUS_OPEN, STATUS_LIVE, STATUS_CLOSING),
        ).fetchone()["n"]
        if live >= PTY_MAX_SESSIONS_PER_OPERATOR:
            raise ValueError(
                f"you already have {live} terminal sessions open on {machine}; "
                f"close one before opening another")

        session_id = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO pty_sessions(id, machine, operator, shell, cols, rows, "
            "command_id, status, created_at, last_activity, last_console_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (session_id, machine, operator, shell, cols, rows, STATUS_OPEN, now, now, now),
        )
        row = conn.execute("SELECT * FROM pty_sessions WHERE id = ?", (session_id,)).fetchone()
    return _row(row)


def attach_command(db_path, session_id, command_id):
    """Record which `shell_open` command carries this session, so the console can tell
    'the agent never picked it up' (command expired) from 'the shell died'."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE pty_sessions SET command_id = ? WHERE id = ?",
                     (str(command_id), str(session_id)))


def get_session(db_path, session_id):
    with get_conn(db_path) as conn:
        return _row(conn.execute(
            "SELECT * FROM pty_sessions WHERE id = ?", (str(session_id),)).fetchone())


def list_sessions(db_path, machine=None, operator=None, active_only=True):
    """For the console's 'you have a terminal open here' affordance and for admin views."""
    sql = "SELECT * FROM pty_sessions WHERE 1=1"
    args = []
    if machine:
        sql += " AND machine = ?"
        args.append(str(machine).strip())
    if operator:
        sql += " AND operator = ?"
        args.append(str(operator).strip().lower())
    if active_only:
        sql += " AND status IN (?, ?, ?)"
        args.extend([STATUS_OPEN, STATUS_LIVE, STATUS_CLOSING])
    sql += " ORDER BY created_at DESC"
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def request_close(db_path, session_id):
    """Console-initiated close: flag it and let the agent confirm on its next input poll.
    Returns False if the session was already finished."""
    now = int(time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE pty_sessions SET status = ?, last_activity = ? "
            "WHERE id = ? AND status IN (?, ?)",
            (STATUS_CLOSING, now, str(session_id), STATUS_OPEN, STATUS_LIVE),
        )
        return (cur.rowcount or 0) > 0


def finish_session(db_path, session_id, reason=""):
    """Terminal state, from the agent (the shell exited / it honoured a close) or from the
    reaper. Drops the session's buffered streams -- nothing here is a record."""
    now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE pty_sessions SET status = ?, closed_at = ?, close_reason = ?, "
            "last_activity = ? WHERE id = ?",
            (STATUS_CLOSED, now, str(reason or "")[:500], now, str(session_id)),
        )
        conn.execute("DELETE FROM pty_input WHERE session_id = ?", (str(session_id),))
        # Output is deliberately kept for a moment: the console's last poll should still be
        # able to render the shell's parting words. reap_sessions clears it shortly after.


def _expire_idle(conn, now,
                 agent_silent_seconds=PTY_AGENT_SILENT_SECONDS,
                 abandoned_seconds=PTY_ABANDONED_SECONDS):
    """Mark dead and abandoned sessions closed. Runs inside a caller's transaction.

    Two separate rules, because they answer two different questions -- see the comment on
    PTY_AGENT_SILENT_SECONDS. Collapsing them back into one check on last_activity would
    silently disable abandonment entirely, since the agent's own polling keeps that column
    fresh for as long as the session exists.
    """
    conn.execute(
        "UPDATE pty_sessions SET status = ?, closed_at = ?, close_reason = ? "
        "WHERE status IN (?, ?, ?) AND last_activity < ?",
        (STATUS_CLOSED, now, "the machine stopped responding",
         STATUS_OPEN, STATUS_LIVE, STATUS_CLOSING, now - int(agent_silent_seconds)),
    )
    conn.execute(
        "UPDATE pty_sessions SET status = ?, closed_at = ?, close_reason = ? "
        "WHERE status IN (?, ?, ?) AND last_console_at < ?",
        (STATUS_CLOSED, now, "nobody came back to this terminal",
         STATUS_OPEN, STATUS_LIVE, STATUS_CLOSING, now - int(abandoned_seconds)),
    )


def note_console_seen(db_path, session_id, now=None):
    """The operator's browser is still watching this session. Called from every console
    read/write; it is the only thing keeping an open terminal off the abandonment reaper."""
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        conn.execute("UPDATE pty_sessions SET last_console_at = ? WHERE id = ?",
                     (int(now), str(session_id)))


def reap_sessions(db_path, agent_silent_seconds=PTY_AGENT_SILENT_SECONDS,
                  abandoned_seconds=PTY_ABANDONED_SECONDS, now=None):
    """Housekeeping: close dead/abandoned sessions and drop the streams of finished ones.
    Returns how many sessions were reaped. Wired into the same periodic sweep as
    fleet.expire_stale_commands."""
    if now is None:
        now = int(time.time())
    with get_conn(db_path) as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM pty_sessions WHERE status = ?", (STATUS_CLOSED,)
        ).fetchone()["n"]
        _expire_idle(conn, now, agent_silent_seconds, abandoned_seconds)
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM pty_sessions WHERE status = ?", (STATUS_CLOSED,)
        ).fetchone()["n"]

        # Streams of sessions closed a while ago. The grace here is not arbitrary: a shell
        # can exit (or be reaped) while its operator is on another page, and coming back to
        # a blank terminal that says nothing about what happened is exactly the experience
        # persistence exists to avoid. Ten minutes is long enough to walk back and read the
        # last screen, short enough that closed sessions aren't storage.
        conn.execute(
            "DELETE FROM pty_output WHERE session_id IN ("
            "  SELECT id FROM pty_sessions WHERE status = ? AND closed_at < ?)",
            (STATUS_CLOSED, now - 600),
        )
        conn.execute(
            "DELETE FROM pty_input WHERE session_id IN ("
            "  SELECT id FROM pty_sessions WHERE status = ?)",
            (STATUS_CLOSED,),
        )
        # Session rows themselves are cheap, but not free forever.
        conn.execute(
            "DELETE FROM pty_sessions WHERE status = ? AND closed_at < ?",
            (STATUS_CLOSED, now - 24 * 60 * 60),
        )
        return after - before


# ================================
# INPUT (console -> agent)
# ================================
def push_input(db_path, session_id, kind, payload):
    """Queue one keystroke batch ('data') or a terminal resize ('resize') for the agent.

    Raw bytes are passed through UNTOUCHED. No trimming, no newline translation, no
    filtering of control characters: '\\r' alone is a bare Enter, '\\x03' is Ctrl-C, and an
    escape sequence is an arrow key. Every one of those is meaningful to the console on the
    other end, and "helpfully" normalising any of them is what makes a remote shell unable
    to answer a prompt.
    """
    session_id = str(session_id)
    if kind not in ("data", "resize"):
        raise ValueError(f"unknown input kind: {kind!r}")

    if kind == "data":
        payload = str(payload or "")
        if not payload:
            return None
        if len(payload) > PTY_MAX_INPUT_CHARS:
            raise ValueError(f"input must be {PTY_MAX_INPUT_CHARS} characters or fewer")
        stored = payload
    else:
        cols = _clamp((payload or {}).get("cols"), 20, 500, 120)
        rows = _clamp((payload or {}).get("rows"), 5, 200, 30)
        stored = json.dumps({"cols": cols, "rows": rows})

    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, cols, rows, next_input_seq FROM pty_sessions WHERE id = ?",
            (session_id,)).fetchone()
        if row is None:
            raise KeyError("unknown session")
        if row["status"] in (STATUS_CLOSING, STATUS_CLOSED):
            raise PermissionError("session is closed")

        # Monotonic for the session's whole life -- see init_pty_db on why this cannot be
        # derived from the rows still sitting in pty_input.
        seq = row["next_input_seq"]
        conn.execute("UPDATE pty_sessions SET next_input_seq = ? WHERE id = ?",
                     (seq + 1, session_id))
        conn.execute(
            "INSERT INTO pty_input(session_id, seq, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, kind, stored, now),
        )
        if kind == "resize":
            size = json.loads(stored)
            conn.execute("UPDATE pty_sessions SET cols = ?, rows = ? WHERE id = ?",
                         (size["cols"], size["rows"], session_id))
        conn.execute("UPDATE pty_sessions SET last_activity = ? WHERE id = ?", (now, session_id))
    return seq


def pull_input(db_path, session_id, after_seq=-1):
    """The agent's fast poll. Returns queued items plus whether it should shut the session
    down. Consumed rows are deleted, so this is the agent's ack -- `after_seq` only guards
    against a response the agent never received."""
    session_id = str(session_id)
    try:
        after_seq = int(after_seq)
    except (TypeError, ValueError):
        after_seq = -1

    now = int(time.time())
    with get_conn(db_path) as conn:
        session = conn.execute(
            "SELECT status, cols, rows FROM pty_sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise KeyError("unknown session")

        rows = conn.execute(
            "SELECT seq, kind, payload FROM pty_input "
            "WHERE session_id = ? AND seq > ? ORDER BY seq ASC",
            (session_id, after_seq),
        ).fetchall()
        # Everything at or below the cursor has been delivered; the agent will never ask for
        # it again, so it can go.
        conn.execute("DELETE FROM pty_input WHERE session_id = ? AND seq <= ?",
                     (session_id, after_seq))
        conn.execute("UPDATE pty_sessions SET last_activity = ? WHERE id = ?", (now, session_id))

    items = []
    for r in rows:
        if r["kind"] == "resize":
            items.append({"seq": r["seq"], "kind": "resize", "size": json.loads(r["payload"])})
        else:
            items.append({"seq": r["seq"], "kind": "data", "data": r["payload"]})

    return {
        "items": items,
        "next_seq": (rows[-1]["seq"] + 1) if rows else after_seq + 1,
        "closing": session["status"] in (STATUS_CLOSING, STATUS_CLOSED),
    }


# ================================
# OUTPUT (agent -> console)
# ================================
def push_output(db_path, session_id, seq, chunk):
    """Append one VT chunk from the agent and drop anything that has rolled out of the
    window. Returns the new oldest retained seq, so the agent knows the stream is being
    consumed (it has no use for it beyond that)."""
    session_id = str(session_id)
    try:
        seq = int(seq)
    except (TypeError, ValueError):
        raise ValueError("seq must be an integer")
    if seq < 0:
        raise ValueError("seq must be >= 0")

    chunk = str(chunk or "")
    if len(chunk) > PTY_MAX_CHUNK_CHARS:
        raise ValueError(f"chunk must be {PTY_MAX_CHUNK_CHARS} characters or fewer")

    now = int(time.time())
    with get_conn(db_path) as conn:
        session = conn.execute(
            "SELECT status FROM pty_sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise KeyError("unknown session")
        if session["status"] == STATUS_CLOSED:
            raise PermissionError("session is closed")

        if chunk:
            conn.execute(
                "INSERT OR IGNORE INTO pty_output(session_id, seq, chunk, received_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, seq, chunk, now),
            )
            # Trim from the FRONT, never refuse a write: a terminal that stops echoing
            # because it hit a cap is a dead terminal. Two bounds, because chunk sizes vary
            # by three orders of magnitude (one keystroke's echo vs. a 16 KB paste) and
            # either one alone lets the buffer be the wrong size:
            #  * a cheap count bound, so a flood of tiny chunks can't pile up rows;
            #  * a size bound, which is the one that actually decides how much scrollback a
            #    returning operator gets back.
            conn.execute(
                "DELETE FROM pty_output WHERE session_id = ? AND seq <= ?",
                (session_id, seq - PTY_REPLAY_MAX_CHUNKS),
            )
            # Everything older than the newest PTY_REPLAY_MAX_CHARS worth of bytes. The
            # window function totals backwards from the newest chunk, so the cut lands on a
            # chunk boundary at (or just past) the budget.
            conn.execute(
                "DELETE FROM pty_output WHERE session_id = ? AND seq <= ("
                "  SELECT COALESCE(MAX(seq), -1) FROM ("
                "    SELECT seq, SUM(LENGTH(chunk)) OVER "
                "           (ORDER BY seq DESC ROWS UNBOUNDED PRECEDING) AS running"
                "    FROM pty_output WHERE session_id = ?"
                "  ) WHERE running > ?)",
                (session_id, session_id, PTY_REPLAY_MAX_CHARS),
            )
        # An agent posting output IS the agent having attached.
        conn.execute(
            "UPDATE pty_sessions SET last_activity = ?, status = ? "
            "WHERE id = ? AND status = ?",
            (now, STATUS_LIVE, session_id, STATUS_OPEN),
        )
        conn.execute("UPDATE pty_sessions SET last_activity = ? WHERE id = ?", (now, session_id))

        oldest = conn.execute(
            "SELECT COALESCE(MIN(seq), -1) AS m FROM pty_output WHERE session_id = ?",
            (session_id,),
        ).fetchone()["m"]
    return oldest


def clear_replay(db_path, session_id):
    """Drop the replay buffer without touching the session.

    This is what the console's Clear button means once a terminal persists: clearing only
    the local xterm view would leave the history sitting on the hub, so it would reappear
    the next time the operator navigated back -- which reads as the button not working. The
    shell itself is untouched; this is the scrollback, not the session.

    Safe for a live console mid-poll: its cursor is left where it is and the agent's seq
    keeps climbing, so the next chunk is still ahead of the cursor and nothing re-renders.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM pty_output WHERE session_id = ?", (str(session_id),))


def pull_output(db_path, session_id, after_seq=-1):
    """The console's poll, and also its RE-ATTACH: `after_seq=-1` returns the whole retained
    replay buffer, which is how an operator who navigated away and came back gets their
    scrollback restored into a fresh terminal.

    Two different "you missed something" flags, and they mean different things:

      * `lost`   -- a LIVE console's cursor fell behind the rolling window, so there is a
                    hole in the middle of the stream. A hole in VT is not "some missing
                    text", it is a half-eaten escape sequence that corrupts everything
                    after it, so the console must reset its emulator.
      * `replay_truncated` -- a re-attaching console is being given history that starts
                    partway in, because the session produced more than the buffer holds.
                    Nothing is corrupt; there is simply older output that is gone.
    """
    session_id = str(session_id)
    try:
        after_seq = int(after_seq)
    except (TypeError, ValueError):
        after_seq = -1

    with get_conn(db_path) as conn:
        session = conn.execute(
            "SELECT * FROM pty_sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise KeyError("unknown session")
        rows = conn.execute(
            "SELECT seq, chunk FROM pty_output WHERE session_id = ? AND seq > ? "
            "ORDER BY seq ASC",
            (session_id, after_seq),
        ).fetchall()
        oldest = conn.execute(
            "SELECT COALESCE(MIN(seq), -1) AS m FROM pty_output WHERE session_id = ?",
            (session_id,),
        ).fetchone()["m"]
        highest = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM pty_output WHERE session_id = ?",
            (session_id,),
        ).fetchone()["m"]

    return {
        "chunks": [{"seq": r["seq"], "text": r["chunk"]} for r in rows],
        "next_seq": highest + 1,
        "status": session["status"],
        "close_reason": session["close_reason"],
        "shell": session["shell"],
        "cols": session["cols"],
        "rows": session["rows"],
        # after_seq == -1 is an attach, which has missed nothing by definition -- but it may
        # be getting a partial history, which is the other flag.
        "lost": after_seq >= 0 and oldest > after_seq + 1,
        "replay_truncated": after_seq < 0 and oldest > 0,
    }
