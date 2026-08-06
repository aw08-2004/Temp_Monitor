"""Live process list for one machine -- the console's task manager.

An operator on the phone with a user ("Word is stuck", "this laptop's fan has been
screaming since lunch") wants what Task Manager shows: which processes are running, what
they are costing, and a way to end or restart one. That is what this module stores and
what `kill_process` / `restart_process` act on.

**This is LIVE STATE, not history.** One row per machine, overwritten by each report,
never appended and never pruned -- a process list five minutes old is not evidence of
anything, and keeping a series of them would grow the database by megabytes per machine
per day to answer a question nobody asks. The readings tables remain the place where
history lives.

**Sampling is WATCHED, not continuous.** Enumerating a few hundred processes, resolving
each one's owner and image path and asking WMI which of them host services costs the
managed machine real CPU, and shipping ~60 KB on every heartbeat from every PC in the
fleet would cost the hub real bandwidth -- to answer a question that is being asked on
exactly one machine, by exactly one operator, for a couple of minutes. So the console's
poll IS the subscription: `note_watch` while the card is open, `is_watched` tells that
agent (and only that agent) to start sampling, and the watch lapses on its own when the
operator navigates away. A machine nobody is looking at does no work and sends nothing.
See WATCH_TTL_SECONDS for why the window is what it is.

**...and the machine asks whether it is watched far more often than it heartbeats.**
`is_watched` is answered on two endpoints: the 10-second heartbeat, and a dedicated
agent-facing poll (`/api/agent/processes/wanted`) that exists purely so an operator does
not wait out a heartbeat tick before their machine even starts looking. That makes this the
most-called function in the module by a wide margin -- every enrolled machine in the fleet
asks it every couple of seconds, watched or not -- so it must stay one indexed lookup on a
table with one row per machine, and must never write.

**PID reuse is the hazard this module is shaped around.** Every snapshot is seconds old by
the time an operator clicks End task, and Windows recycles process ids aggressively -- so
a pid alone is never enough to name what should die. Every kill/restart carries the NAME
the operator saw, the agent re-reads the live pid before touching it, and a mismatch is
refused rather than "helpfully" resolved. The hub validates the same pairing here so a
malformed request is rejected before it becomes a queued command.

Kept free of Flask so it can be unit-tested in isolation, exactly like fleet.py and
remote.py; processes_web.py wires thin HTTP endpoints on top.
"""
import json
import sqlite3
import time

# ================================
# CAPS
# ================================
#: Most processes one report may carry. A busy workstation runs 250-400; a terminal server
#: can run into the thousands, and shipping all of them would put a megabyte on a heartbeat
#: to fill a table nobody can read. The agent sorts by CPU then memory before truncating, so
#: what survives the cap is what an operator opened this card to find, and `truncated` says
#: how many were dropped rather than letting the list quietly lie.
MAX_PROCESSES = 400

#: Field caps. Image paths get the longest allowance (MAX_PATH is 260, but a long UNC or a
#: package path under WindowsApps goes further); everything else is short by nature.
MAX_NAME_CHARS = 128
MAX_PATH_CHARS = 520
MAX_USER_CHARS = 260
MAX_SERVICE_CHARS = 128
MAX_SERVICES_PER_PROCESS = 12

#: How long one console poll keeps a machine sampling. The console polls every
#: POLL_INTERVAL_SECONDS while the card is open, so this survives a couple of missed polls
#: (a slow request, a laptop's browser suspending a background tab) without the list going
#: stale mid-read -- and still stops the machine sampling within a minute of the operator
#: closing the card, which is the whole point of the watch.
WATCH_TTL_SECONDS = 45

#: What the console polls at, published here so the hub and the browser cannot drift apart:
#: the agent's sampling cadence, the watch TTL and this are one design, not three constants.
POLL_INTERVAL_SECONDS = 5

#: A snapshot older than this is stale rather than merely late -- the machine went offline,
#: or its agent is too old to know what a process report is. The console says so instead of
#: rendering a minute-old list as though it were live.
STALE_AFTER_SECONDS = 60


# ================================
# PROCESSES THAT MAY NOT BE KILLED
# ================================
# Ending any of these does not "close a program": it takes the machine down, and on most of
# them it does so immediately and with a bugcheck rather than a shutdown. csrss, wininit,
# winlogon, services, lsass and smss are the ones Windows itself treats as critical -- a
# console that offers a one-click blue screen to a helpdesk is a console that will eventually
# deliver one. The System/Idle/Registry/Secure System/Memory Compression pseudo-processes
# cannot be ended at all and would merely produce a confusing failure.
#
# svchost is on this list for a different reason, and it is the interesting one: it CAN be
# killed, Task Manager lets you, and doing it takes down every service in that host -- which
# for the RPC host means the machine reboots itself in a minute. The thing an operator
# actually wants there is to restart one SERVICE, which `restart_process` does properly (see
# the agent's RestartProcessExecutor), so the refusal points at the remedy instead of just
# saying no.
#
# The AGENT enforces this list too, and its copy is the authority -- this one exists so a
# refusal is immediate and legible in the console rather than arriving 10 seconds later as a
# failed command, and so a hand-rolled API call is turned away before it is ever queued.
# Compared case-insensitively, with or without the .exe.
PROTECTED_PROCESSES = frozenset({
    "system",
    "system idle process",
    "idle",
    "registry",
    "secure system",
    "memory compression",
    "memcompression",
    "smss",
    "csrss",
    "wininit",
    "winlogon",
    "services",
    "lsass",
    "lsaiso",
    "svchost",
    # The agent itself. Killing it does not just lose this card -- it loses the machine from
    # the console entirely until the service manager restarts it, and the command's own
    # result would never be reported, so the operator would be left watching a spinner for a
    # process they had already ended. Restarting the service is a terminal-tab job.
    "tempmonitoragent",
})


def normalize_name(name):
    """A process name in the form PROTECTED_PROCESSES is keyed on: lowercase, no .exe.

    Windows reports the same process as "chrome.exe" through one API and "chrome" through
    another, and an operator typing into the filter box does neither consistently. One
    normalizer, shared by the guard and the console, so a rename cannot slip past by spelling.
    """
    text = str(name or "").strip().lower()
    if text.endswith(".exe"):
        text = text[:-4]
    return text


def is_protected(name):
    """Would ending this process take the machine down (or fail meaninglessly)?"""
    return normalize_name(name) in PROTECTED_PROCESSES


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_processes_db(db_path):
    """Create the process tables if absent. Idempotent -- safe to call on every hub start
    next to app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One row per machine, replaced wholesale. `captured_at` is the AGENT's clock (when
        # the sample was taken) and `reported_at` is the hub's (when it landed); both are
        # kept because they answer different questions -- the first is how old the numbers
        # are, the second is whether the machine is still talking to us. A box with a skewed
        # clock would otherwise present a snapshot from next Tuesday.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_processes (
                machine     TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                captured_at INTEGER,
                reported_at INTEGER NOT NULL
            )
            """
        )
        # The subscription. One row per machine rather than per operator: two operators
        # looking at the same PC want the same sampling, not two of it, and `watcher` is
        # kept only so a log or a future "who is looking at this" can name somebody.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS process_watch (
                machine    TEXT PRIMARY KEY,
                watcher    TEXT,
                expires_at INTEGER NOT NULL
            )
            """
        )


# ================================
# THE WATCH
# ================================
def note_watch(db_path, machine, watcher=None, now=None, ttl=WATCH_TTL_SECONDS):
    """Record that somebody is looking at this machine's processes right now.

    Called from the console's read endpoint, so polling the list IS renewing the watch.
    There is deliberately no "stop watching" call to match it: a browser tab that is closed,
    crashed or driven off a cliff by a sleeping laptop never gets to send one, and a
    subscription that depends on a farewell message is a subscription that leaks.
    """
    machine = str(machine or "").strip()
    if not machine:
        return
    expires = int(now if now is not None else time.time()) + int(ttl)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO process_watch(machine, watcher, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET watcher = excluded.watcher, "
            "expires_at = excluded.expires_at",
            (machine, str(watcher or "")[:MAX_USER_CHARS], expires),
        )


def is_watched(db_path, machine, now=None):
    """Is an operator looking at this machine's processes?

    Answered on the heartbeat AND on the agent's dedicated watch poll, so it must stay a
    single indexed lookup and must never write: every enrolled machine in the fleet asks
    this every couple of seconds, and the overwhelmingly common answer is no."""
    machine = str(machine or "").strip()
    if not machine:
        return False
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT expires_at FROM process_watch WHERE machine = ?", (machine,)
        ).fetchone()
    return row is not None and int(row["expires_at"]) > now


def clear_watch(db_path, machine):
    """Drop the watch immediately. Used when a machine is deleted, and available to a
    console that knows it is done (a card being collapsed) rather than waiting out the TTL."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM process_watch WHERE machine = ?",
                     (str(machine or "").strip(),))


def prune_watches(db_path, now=None):
    """Drop lapsed watch rows. Housekeeping only -- `is_watched` already tests the expiry,
    so a stale row is never believed; this just stops the table growing by one row per
    machine an operator ever looked at. Returns rows removed."""
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM process_watch WHERE expires_at <= ?", (now,))
        return cur.rowcount or 0


# ================================
# INGEST
# ================================
def _as_float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # NaN and the infinities survive float() and json.dumps() writes them as bare NaN /
    # Infinity, which is not JSON and which every browser's JSON.parse refuses -- one odd
    # sensor reading would blank the whole card.
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _as_int(value):
    # bool is an int in Python, so `{"pids": true}` would otherwise arrive as pid 1 and
    # `{"pid": false}` as pid 0. Neither is a process id anybody meant to name.
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_process(raw):
    """One process entry, trimmed to the shape the console renders. Returns None for
    anything without a usable pid and name -- a row that cannot be acted on is noise."""
    if not isinstance(raw, dict):
        return None
    pid = _as_int(raw.get("pid"))
    name = str(raw.get("name") or "").strip()[:MAX_NAME_CHARS]
    if pid is None or pid < 0 or not name:
        return None

    services = []
    for svc in list(raw.get("services") or [])[:MAX_SERVICES_PER_PROCESS]:
        text = str(svc or "").strip()[:MAX_SERVICE_CHARS]
        if text:
            services.append(text)

    entry = {
        "pid": pid,
        "name": name,
        "cpu_pct": _as_float(raw.get("cpu_pct")),
        "mem_mb": _as_float(raw.get("mem_mb")),
        "user": str(raw.get("user") or "")[:MAX_USER_CHARS],
        "session": _as_int(raw.get("session")),
        "path": str(raw.get("path") or "")[:MAX_PATH_CHARS],
        "services": services,
        "started_at": _as_int(raw.get("started_at")),
    }
    # Whether a row may be ended is the hub's answer, not the agent's, so the console never
    # has to know the list and an older agent cannot omit the flag. The agent still enforces
    # it independently -- this is the label, not the gate.
    entry["protected"] = is_protected(name)
    return entry


def record_snapshot(db_path, machine, payload, now=None):
    """Store one process report from an agent. Returns True if something was stored.

    Written from the heartbeat, so it is trimmed and type-checked rather than trusted, and a
    malformed payload is DROPPED rather than raised -- exactly as remote.record_inventory is,
    and for the same reason: a heartbeat that 500s because one machine sent something odd
    takes that machine offline fleet-wide, which is a far worse failure than a card that
    keeps showing the previous list.
    """
    machine = str(machine or "").strip()
    if not machine or not isinstance(payload, dict):
        return False

    raw_list = payload.get("processes")
    if not isinstance(raw_list, list):
        return False

    processes = []
    for raw in raw_list[:MAX_PROCESSES]:
        entry = _clean_process(raw)
        if entry is not None:
            processes.append(entry)

    # An empty list is a legitimate report only in the sense that a machine which enumerated
    # nothing has told us nothing useful -- and overwriting a good snapshot with it would
    # blank the operator's card on one bad scan. Refuse it and keep what we have.
    if not processes:
        return False

    dropped = _as_int(payload.get("truncated")) or 0
    overflow = max(0, len(raw_list) - MAX_PROCESSES)

    stored = {
        "processes": processes,
        "cpu_cores": _as_int(payload.get("cpu_cores")),
        "mem_total_mb": _as_float(payload.get("mem_total_mb")),
        "sample_ms": _as_int(payload.get("sample_ms")),
        # What the machine dropped before sending plus what we dropped on arrival. One
        # number, because "you are not seeing everything" is one fact to an operator and
        # attributing it to a cap on either side helps nobody.
        "truncated": max(0, dropped) + overflow,
    }

    captured = _as_int(payload.get("captured_at"))
    reported = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO machine_processes(machine, payload_json, captured_at, reported_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET payload_json = excluded.payload_json, "
            "captured_at = excluded.captured_at, reported_at = excluded.reported_at",
            (machine, json.dumps(stored), captured, reported),
        )
    return True


def get_snapshot(db_path, machine, now=None):
    """The last process report for `machine`, in the shape the console consumes.

    Always answers, even for a machine that has never reported: `processes` is empty and
    `reported_at` is None, which the card renders as "waiting for the machine" rather than as
    an error. The two are genuinely different states and the console says so -- a PC that is
    merely slow to start sampling must not look like one whose agent is too old to sample.
    """
    machine = str(machine or "").strip()
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json, captured_at, reported_at FROM machine_processes "
            "WHERE machine = ?", (machine,)
        ).fetchone()

    empty = {"processes": [], "cpu_cores": None, "mem_total_mb": None,
             "sample_ms": None, "truncated": 0}
    if row is None:
        payload = dict(empty)
        payload.update({"machine": machine, "captured_at": None, "reported_at": None,
                        "age_seconds": None, "stale": True})
        return payload

    try:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except (TypeError, ValueError):
        payload = dict(empty)

    for key, default in empty.items():
        payload.setdefault(key, default)

    age = max(0, now - int(row["reported_at"]))
    payload.update({
        "machine": machine,
        "captured_at": row["captured_at"],
        "reported_at": row["reported_at"],
        # Age is measured on the HUB's clock against the hub's own receipt, never against
        # the agent's captured_at: a machine whose clock is wrong would otherwise report a
        # snapshot as hours old (or from the future) the instant it arrived.
        "age_seconds": age,
        "stale": age > STALE_AFTER_SECONDS,
    })
    return payload


def forget_machine(db_path, machine):
    """Drop everything this module holds for a machine. Called from the machine-delete path
    beside fleet.delete_machine, so a decommissioned PC leaves nothing behind."""
    machine = str(machine or "").strip()
    if not machine:
        return
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_processes WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM process_watch WHERE machine = ?", (machine,))


# ================================
# COMMAND PARAMS
# ================================
# The two verbs this feature adds, validated here so the rules live next to the snapshot they
# are derived from rather than inside a Flask handler.
MAX_KILL_PIDS = 64


def validate_kill(name, pids, tree=False):
    """Params for a `kill_process` command, or ValueError.

    Both halves are required, and that is the point: `pids` says what to end and `name` says
    what the operator BELIEVED they were ending. The agent re-reads each live pid and refuses
    any whose name has changed, so a snapshot that went stale between render and click ends
    nothing instead of ending whatever inherited the id. A caller that could pass a bare pid
    would be a caller that can kill a process at random.
    """
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    if len(clean_name) > MAX_NAME_CHARS:
        raise ValueError(f"name must be {MAX_NAME_CHARS} characters or fewer")
    if is_protected(clean_name):
        raise ValueError(f"{clean_name} is a critical Windows process and cannot be ended "
                         f"from here")

    if isinstance(pids, (int, str)):
        pids = [pids]
    if not isinstance(pids, (list, tuple)):
        raise ValueError("pids must be a list")

    clean_pids = []
    for raw in pids:
        pid = _as_int(raw)
        if pid is None or pid <= 0:
            raise ValueError(f"invalid pid: {raw!r}")
        if pid not in clean_pids:
            clean_pids.append(pid)
    if not clean_pids:
        raise ValueError("at least one pid is required")
    if len(clean_pids) > MAX_KILL_PIDS:
        raise ValueError(f"at most {MAX_KILL_PIDS} processes at a time")

    return {"name": clean_name, "pids": clean_pids, "tree": bool(tree)}


def validate_restart(name, pid):
    """Params for a `restart_process` command, or ValueError.

    One pid, never a list. "Restart" means end this and start it again in the session it was
    running in, and that has no sensible meaning across twelve tabs of a browser -- the
    console offers it on a single instance for exactly that reason, and this refuses to
    invent a broader one.
    """
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    if len(clean_name) > MAX_NAME_CHARS:
        raise ValueError(f"name must be {MAX_NAME_CHARS} characters or fewer")
    clean_pid = _as_int(pid)
    if clean_pid is None or clean_pid <= 0:
        raise ValueError(f"invalid pid: {pid!r}")
    # Deliberately NOT gated on is_protected: restarting a process that hosts a service is
    # the one useful thing to do with an svchost, and the agent turns it into a proper
    # service stop/start rather than a kill. Ending one is what stays refused.
    return {"name": clean_name, "pid": clean_pid}
