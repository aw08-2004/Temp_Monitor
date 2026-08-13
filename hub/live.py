"""The live-telemetry watch: who is looking at a machine's charts RIGHT NOW.

The machine page opens on a one-minute window, so it is watched the way a monitor is
watched -- somebody is on the phone with the user, asking them to open the thing that makes
the fan scream, and reading the answer off the CPU Load panel while it happens. At the
agent's ordinary five-second cadence that panel draws about twelve points a minute, and a
spike that lasts three seconds is either one lonely dot or nothing at all.

**So the cadence follows the operator, not the fleet.** While somebody has that page open
the machine reports every second (FAST_INTERVAL_SECONDS) with a full sensor block on every
tick, and the panels move at 1 Hz. The moment nobody is looking it goes back to five
seconds and sensors every ten, which is what it has always done. A fleet nobody has open
costs exactly what it cost before.

**This is the same shape as processes.py's watch, deliberately, and for the same reasons:**
one row per machine (two operators looking at the same PC want one cadence, not two), the
console's poll IS the subscription (`note_watch` renews it, there is no unsubscribe --
a tab that is closed, crashed, or suspended by a sleeping laptop never gets to send one),
and `is_watched` is a single indexed read that never writes, because every enrolled machine
in the fleet asks it every couple of seconds and the answer is almost always no.

**What it costs while somebody IS looking.** Ten reports a minute becomes sixty, each
carrying the sensor block it used to carry every other time -- so roughly 36 KB/s on the
wire and sixty reading rows a minute for that ONE machine, for as long as the page is open.
That is the same bargain the process card strikes (see processes.py), and it is affordable
for the same reason: it is bounded by attention, and attention does not scale with the
fleet.

Kept free of Flask so it can be unit-tested on its own, exactly like processes.py; app.py
serves the console's renewal endpoint and fleet_web.py answers the agent's.
"""
import sqlite3
import time

#: The reporting cadence a watched machine switches to. One second because that is what
#: "live" means to somebody reading a graph while a user clicks something -- and because the
#: chart's window is 60 seconds wide, so 1 Hz is one pixel-column per point rather than a
#: line drawn through a dozen dots.
FAST_INTERVAL_SECONDS = 1

#: What the console renews the watch at while the page is open. Published here (and served
#: to the browser) rather than hardcoded in machine.js so the poll rate, the TTL and the
#: agent's cadence stay one decision instead of three that drift.
POLL_INTERVAL_SECONDS = 5

#: How long one renewal keeps a machine reporting fast. Survives a couple of missed pings (a
#: slow request, a laptop suspending a background tab) without the charts dropping back to
#: five-second steps mid-read, and still returns the machine to its ordinary cadence within
#: ~20 seconds of the operator closing the tab. Shorter than the process watch's 45s because
#: what lapses here is twelve times the traffic.
WATCH_TTL_SECONDS = 20

#: Cap on what a watcher string may store, matching processes.py's MAX_USER_CHARS.
MAX_WATCHER_CHARS = 260


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_live_db(db_path):
    """Create the watch table if absent. Idempotent -- safe to call on every hub start next
    to processes.init_processes_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One row per machine. `watcher` is kept only so a log (or a future "who is looking
        # at this?") can name somebody; nothing reads it to make a decision.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_watch (
                machine    TEXT PRIMARY KEY,
                watcher    TEXT,
                expires_at INTEGER NOT NULL
            )
            """
        )


def note_watch(db_path, machine, watcher=None, now=None, ttl=WATCH_TTL_SECONDS):
    """Record that somebody is watching this machine's charts right now.

    Called from the console's renewal endpoint, so pinging IS subscribing. There is
    deliberately no "stop watching" call to match it -- see the module docstring."""
    machine = str(machine or "").strip()
    if not machine:
        return
    expires = int(now if now is not None else time.time()) + int(ttl)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO live_watch(machine, watcher, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET watcher = excluded.watcher, "
            "expires_at = excluded.expires_at",
            (machine, str(watcher or "")[:MAX_WATCHER_CHARS], expires),
        )


def is_watched(db_path, machine, now=None):
    """Is an operator watching this machine's charts?

    Answered on the heartbeat AND on the agent's dedicated watch poll, so like its process
    counterpart it must stay one indexed lookup and must never write."""
    machine = str(machine or "").strip()
    if not machine:
        return False
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT expires_at FROM live_watch WHERE machine = ?", (machine,)
        ).fetchone()
    return row is not None and int(row["expires_at"]) > now


def clear_watch(db_path, machine):
    """Drop the watch immediately, rather than waiting out the TTL."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM live_watch WHERE machine = ?",
                     (str(machine or "").strip(),))


def forget_machine(db_path, machine):
    """Erase this machine's watch. Called when a machine is deleted: the row would lapse on
    its own within twenty seconds, but a deleted machine leaving behind a row naming who was
    watching it is exactly the residue a deletion is for."""
    clear_watch(db_path, machine)


def prune_watches(db_path, now=None):
    """Delete watches that have lapsed. Housekeeping only -- `is_watched` already ignores
    them -- so that a hub which has been up for months isn't carrying a row for every machine
    anybody ever opened. Returns how many rows went."""
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM live_watch WHERE expires_at <= ?", (now,))
        return cur.rowcount or 0
