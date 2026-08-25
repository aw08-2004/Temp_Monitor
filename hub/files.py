"""The remote file explorer: browsing one machine's disk, and moving bytes both ways.

An operator on the phone with a user ("the report is on my desktop and I can't open it",
"put this driver in C:\\Temp for me") has, until now, had exactly one way to touch a file on
a managed PC: type at the SYSTEM terminal. That works and it will keep working, but `dir`
into a wall of text is not a file list, and there has been no way at all to get a FILE off a
machine or onto one -- a terminal moves text, not bytes.

This module is the hub half of the answer. It holds three things, and they are three
different lifetimes on purpose:

  * **A listing** is a question the operator just asked. One row per request, answered by
    the machine seconds later, read once, and pruned within the hour.
  * **A transfer** is bytes in flight, in either direction, plus the spool file they are
    parked in while the two ends are not both present at once.
  * **The rules** -- what a path may look like, what an operation may be -- which are the
    part worth having exactly once and the reason this file is free of Flask, exactly like
    processes.py and wake.py. files_web.py wires thin HTTP endpoints on top.

**Why a listing is a COMMAND, when a process list is not.** processes.py argues at length
that enumerating processes must NOT be a command: the console polls it every five seconds,
and a security-level audit row every five seconds for the crime of reading a list would
drown the trail that nothing prunes. A directory listing is the opposite case in both
halves. It is human-paced -- one click, one listing, and no poll behind it -- and "who
looked in the CEO's Documents folder, and when" is a question an audit trail should be able
to answer. So a listing goes through fleet.create_command like every other verb, and the
trail gets one row per navigation, which is exactly the record we want.

**Nothing here trusts a path from either end.** A path arriving from the console is checked
before it becomes a command (validate_path), because the console is a browser and a browser
is a thing an attacker can talk to. A path arriving from the AGENT -- inside a listing -- is
re-checked and re-capped here on the way in, because it is arbitrary text from a remote
machine and it is going into a page. The agent enforces its own rules on top of both; see
Files/PathRules.cs. This is a belt-and-braces arrangement and it is meant to be.

**What this module deliberately does NOT do is police WHICH paths.** There is no blocklist
of directories an operator may not browse, and adding one would be theatre: the same
operator, with the same capability, can already read any file on the machine by typing at a
SYSTEM shell, and a list of forbidden folders would only tell them where to type. The gate
that matters is the capability (`issue_commands`) plus machine scope, checked in
files_web.py, and the record that matters is the audit trail. See the CAPS block for the
limits that DO exist and why each is about resources rather than about secrets.
"""
import json
import os
import re
import sqlite3
import time
import uuid

# ================================
# CAPS
# ================================
#: Longest path we will carry. Windows' classic MAX_PATH is 260, but long-path support is
#: on by default on current builds and a deep UNC share goes well past it, so the cap is
#: set where a path stops being plausible rather than where the old API stopped.
MAX_PATH_CHARS = 1024

#: One file or folder name, as typed into the rename/new-folder box. NTFS caps a component
#: at 255; the extra room is for the error message to be about the real problem.
MAX_NAME_CHARS = 260

#: Most entries one listing may carry. A Windows\\System32 has ~5000 files and a user's
#: Downloads can have more; shipping all of them would put a megabyte through the command
#: channel to fill a table nobody scrolls. The agent sorts folders-first-then-name before
#: truncating, so what survives is the top of the list the operator is reading, and
#: `truncated` says how many were dropped rather than letting the list quietly lie.
MAX_ENTRIES = 2000

#: Most paths one operation may name. Multi-select exists so "delete these nine" is one
#: click; it is not a bulk tool, and a request naming ten thousand files is either a mistake
#: or a way to keep one agent busy for an hour.
MAX_OPERATION_PATHS = 200

#: Biggest single transfer, either direction. Two gigabytes is comfortably more than the
#: "send me that log / put this installer there" this feature exists for, and comfortably
#: less than the disk a hub spools onto. A backup is the tool for moving a machine's data;
#: see backups.py.
MAX_TRANSFER_BYTES = 2 * 1024 * 1024 * 1024

#: How long a listing row lives. Long enough that a slow machine's answer still lands in a
#: console that is still waiting, short enough that a table gaining a row per click does not
#: become the largest thing in the database. Pruned by the hub's retention thread.
LISTING_TTL_SECONDS = 900

#: How long a transfer -- and the spooled bytes behind it -- lives. An hour covers a machine
#: that was asleep when the operator clicked and woke up ten minutes later; past that the
#: operator has moved on and the spool file is just disk.
TRANSFER_TTL_SECONDS = 3600

#: What the console polls a pending listing or transfer at. Served to the browser rather
#: than hardcoded there, for the same reason processes.py serves its cadence: one decision,
#: not two constants free to drift.
POLL_INTERVAL_SECONDS = 2


# ================================
# STATUSES
# ================================
# Deliberately three, not two. "Nothing has come back yet" and "the machine said no" are
# different facts to an operator -- the first is waited on, the second is read and acted on
# -- and a console that renders them the same way turns every refusal into a hang.
PENDING = "pending"
READY = "ready"
FAILED = "failed"

#: Which way the bytes go. `pull` is machine -> hub -> operator's browser (the Download
#: button); `push` is browser -> hub -> machine (the Upload button). One table for both,
#: because the spool file, the expiry and the failure handling are identical and the only
#: thing that differs is which end fills it in.
PULL = "pull"
PUSH = "push"
DIRECTIONS = (PULL, PUSH)

#: What a `pull` is fetching. A folder cannot be streamed as itself, so the agent zips it
#: and the console is told to expect a .zip -- which is a fact about the download, not a
#: preference, so it is stored rather than re-derived from the filename later.
KIND_FILE = "file"
KIND_FOLDER = "folder"
KINDS = (KIND_FILE, KIND_FOLDER)


# ================================
# OPERATIONS
# ================================
# The verbs the explorer offers. `delete` is the only irreversible one and it is the reason
# the console confirms; the rest are recoverable by doing the opposite.
#
# `rename` and `new_folder` are separate verbs rather than a `move` with a clever
# destination, because their params are genuinely different shapes -- one names a single
# source and a NEW NAME, the other names no source at all -- and folding them into move
# would mean the agent guessing which of the two an operator meant from the shape of a path.
COPY = "copy"
MOVE = "move"
DELETE = "delete"
RENAME = "rename"
NEW_FOLDER = "new_folder"
OPERATIONS = (COPY, MOVE, DELETE, RENAME, NEW_FOLDER)

#: Operations that carry a `destination` folder, and refuse to run without one.
NEEDS_DESTINATION = frozenset({COPY, MOVE, NEW_FOLDER})
#: Operations that name one or more existing `paths`.
NEEDS_PATHS = frozenset({COPY, MOVE, DELETE, RENAME})
#: Operations that carry a `new_name`.
NEEDS_NEW_NAME = frozenset({RENAME, NEW_FOLDER})


# ================================
# PATHS
# ================================
# A drive letter root (C:\) or a UNC share root (\\server\share). Everything the explorer
# navigates is one of these plus components. Matching them explicitly -- rather than asking
# whether the string "looks absolute" -- is what makes a relative path impossible to smuggle
# through: there is no rule to fall through to.
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:$")
_UNC_ROOT = re.compile(r"^\\\\[^\\/:*?\"<>|]+\\[^\\/:*?\"<>|]+$")

#: Characters Windows forbids in a file NAME. Not applied to whole paths (a colon is legal
#: in "C:") -- see validate_name, which is where a single component is checked.
_BAD_NAME_CHARS = set('\\/:*?"<>|')

#: Names Windows reserves whatever extension you give them. Creating one succeeds through
#: some APIs and then produces a file nobody can open or delete through Explorer, which is a
#: worse outcome than being told no.
_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{n}" for n in range(1, 10)),
    *(f"lpt{n}" for n in range(1, 10)),
})


def validate_path(path):
    """One absolute Windows path, normalized, or ValueError.

    Accepts `C:\\`, `C:\\Users\\bob`, `\\\\server\\share` and below. Rejects relative paths,
    anything containing a `..` component, and anything past MAX_PATH_CHARS.

    **The `..` rule is not about escaping a root** -- there is no root to escape here; an
    operator with this capability may browse the whole disk and that is the point. It is
    about the path being the SAME string on both ends: the console renders a breadcrumb from
    it, the audit log records it, and `C:\\Users\\bob\\..\\alice` is a path whose audit row
    names the wrong person's folder. Normalizing it away would be worse than refusing --
    then the record and the request would differ silently.
    """
    text = str(path or "").strip()
    if not text:
        raise ValueError("a path is required")
    if len(text) > MAX_PATH_CHARS:
        raise ValueError(f"path must be {MAX_PATH_CHARS} characters or fewer")
    # A NUL truncates the string in every Win32 API that eventually sees it, so a path
    # carrying one asks about a different file than the one it appears to name.
    if "\0" in text:
        raise ValueError("path contains an invalid character")

    # Forward slashes are legal on Windows and are what somebody pasting from a browser or a
    # script produces. Accepted, then spoken back in the platform's own dialect so the
    # breadcrumb, the audit row and the agent all see one spelling.
    text = text.replace("/", "\\")

    is_unc = text.startswith("\\\\")
    head, sep, tail = (text[2:].partition("\\") if is_unc else ("", "", ""))
    if is_unc:
        # \\server\share -- both halves are required. A bare \\server is not a path, it is a
        # machine, and the agent has nothing to enumerate for it.
        server = head
        share, _, rest = tail.partition("\\")
        if not server or not share:
            raise ValueError("a UNC path must name a server and a share")
        root = f"\\\\{server}\\{share}"
        parts = [p for p in rest.split("\\") if p != ""]
    else:
        drive, _, rest = text.partition("\\")
        if not _DRIVE_ROOT.match(drive):
            raise ValueError("path must be absolute, like C:\\Users or \\\\server\\share")
        root = drive.upper()
        parts = [p for p in rest.split("\\") if p != ""]

    if any(p == ".." for p in parts):
        raise ValueError("path may not contain '..'")
    # A single "." is merely noise rather than a redirection, but it still makes two strings
    # for one folder, and this module's whole contract is that a path is its own name.
    parts = [p for p in parts if p != "."]

    if not parts:
        # A root renders and enumerates as "C:\" -- with the separator, because "C:" alone
        # means "the current directory on C:" to Windows, which is not what anyone clicked.
        return root + "\\" if not is_unc else root
    return "\\".join([root] + parts)


def validate_name(name):
    """One file or folder NAME -- no separators, no reserved words -- or ValueError.

    Checked here rather than only on the machine so a rename that Windows would refuse is
    refused in the console immediately, with a sentence about what is wrong, instead of ten
    seconds later as a failed command whose output is an HRESULT.
    """
    text = str(name or "").strip()
    if not text:
        raise ValueError("a name is required")
    if len(text) > MAX_NAME_CHARS:
        raise ValueError(f"name must be {MAX_NAME_CHARS} characters or fewer")
    if text in (".", ".."):
        raise ValueError("that is not a name")
    bad = sorted(_BAD_NAME_CHARS & set(text))
    if bad:
        raise ValueError(f"a name may not contain {' '.join(bad)}")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("name contains an invalid character")
    # Windows silently drops a trailing dot or space, so "report." becomes a file the
    # operator did not name and cannot find again by the name they typed.
    if text[-1] in ". ":
        raise ValueError("a name may not end with a space or a dot")
    if text.split(".")[0].lower() in _RESERVED_NAMES:
        raise ValueError(f"{text} is a name Windows reserves")
    return text


def join_path(directory, name):
    """`directory` + `name`, validated as a pair. The one place a path is built rather than
    received, so the console never has to do string surgery on a path it is about to send."""
    parent = validate_path(directory)
    leaf = validate_name(name)
    return (parent + leaf) if parent.endswith("\\") else (parent + "\\" + leaf)


def parent_path(path):
    """The folder containing `path`, or None if it is already a root.

    Used for the explorer's "up" affordance and for confirming where a paste lands. Pure
    string work on an already-validated path, deliberately -- the hub has no idea what is
    actually on the machine's disk, and asking it to guess would produce a breadcrumb that
    disagrees with the listing beside it.
    """
    clean = validate_path(path)
    if clean.endswith("\\"):
        return None                                     # a drive root
    if _UNC_ROOT.match(clean):
        return None                                     # \\server\share
    head, sep, _tail = clean.rpartition("\\")
    if not sep or not head:
        return None
    if _DRIVE_ROOT.match(head):
        return head + "\\"
    return head


# ================================
# COMMAND PARAMS
# ================================
def validate_operation(op, paths=None, destination=None, new_name=None):
    """Params for a `file_operation` command, or ValueError.

    Every verb is validated as the shape it actually is rather than through one permissive
    schema that accepts all of them: `delete` with a destination, or `new_folder` with a
    list of sources, is a request somebody built by hand and is far more likely to be a
    mistake than an intention. The agent validates the same shapes again -- see
    FileOperationExecutor -- and this copy exists so a malformed request is turned away
    before it becomes a queued command with an audit row.
    """
    verb = str(op or "").strip().lower()
    if verb not in OPERATIONS:
        raise ValueError(f"unknown file operation: {op!r}")

    params = {"op": verb}

    if verb in NEEDS_PATHS:
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, (list, tuple)) or not paths:
            raise ValueError("at least one path is required")
        if len(paths) > MAX_OPERATION_PATHS:
            raise ValueError(f"at most {MAX_OPERATION_PATHS} items at a time")
        clean = []
        for raw in paths:
            item = validate_path(raw)
            if item.endswith("\\") or _UNC_ROOT.match(item):
                # Copying, moving or -- particularly -- deleting a whole drive is not a file
                # operation, and every way this could be meant has a better tool.
                raise ValueError(f"{item} is a drive, not a file or folder")
            if item not in clean:
                clean.append(item)
        params["paths"] = clean
    elif paths:
        raise ValueError(f"{verb} does not take a list of paths")

    if verb in NEEDS_DESTINATION:
        params["destination"] = validate_path(destination)
    elif destination:
        raise ValueError(f"{verb} does not take a destination")

    if verb in NEEDS_NEW_NAME:
        params["new_name"] = validate_name(new_name)
    elif new_name:
        raise ValueError(f"{verb} does not take a new name")

    if verb == RENAME and len(params["paths"]) != 1:
        # Renaming twelve things to one name is not a thing; the console offers rename on a
        # single selection for exactly this reason and this refuses to invent a meaning.
        raise ValueError("rename takes exactly one item")

    if verb in (COPY, MOVE):
        for item in params["paths"]:
            if item == params["destination"]:
                raise ValueError("the destination is one of the items being moved")
            # Moving a folder inside itself is the one case that destroys data instead of
            # failing: the shell walks into the copy it is making. Windows refuses it and so
            # does the agent; this is the copy that refuses before the command exists.
            if params["destination"].lower().startswith(item.lower() + "\\"):
                raise ValueError(f"{params['destination']} is inside {item}")

    return params


# ================================
# STORAGE
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_files_db(db_path):
    """Create the explorer's tables if absent. Idempotent -- safe to call on every hub start
    beside app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One row per question asked. `machine` is stored (rather than derived from the
        # command) because every read of this row re-checks it: an agent may answer its own
        # requests and nobody else's, and the console may read a request for a machine in
        # its scope and nobody else's.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_listings (
                request_id  TEXT PRIMARY KEY,
                machine     TEXT NOT NULL,
                path        TEXT NOT NULL,
                status      TEXT NOT NULL,
                payload_json TEXT,
                error       TEXT,
                created_at  INTEGER NOT NULL,
                answered_at INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_listings_machine "
            "ON file_listings(machine, created_at)"
        )
        # Bytes in flight. `spool` is a bare FILENAME, never a path: the directory is the
        # hub's own configuration (files_web is handed it), and storing the join would let a
        # restored database or a moved install point the hub at a directory that is no
        # longer its own.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_transfers (
                id          TEXT PRIMARY KEY,
                machine     TEXT NOT NULL,
                direction   TEXT NOT NULL,
                path        TEXT NOT NULL,
                name        TEXT NOT NULL,
                kind        TEXT NOT NULL,
                spool       TEXT,
                size_bytes  INTEGER,
                status      TEXT NOT NULL,
                error       TEXT,
                issued_by   TEXT,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_transfers_machine "
            "ON file_transfers(machine, created_at)"
        )


# ---------------- listings ----------------
#: The path a DRIVE listing is filed under -- the explorer's root, above every volume.
#: Not a Windows path and deliberately not one validate_path would accept: there is no
#: string that means "the machine itself" to Windows, so inventing one that looked like a
#: path would be a string an operator could type and a path the agent would try to open.
#: A bare backslash is unambiguous here and renders as the root crumb.
DRIVES_PATH = "\\"


def create_listing(db_path, machine, path, drives=False, now=None):
    """Open a pending listing for `path` on `machine`. Returns (request_id, clean_path).

    With `drives=True` the request is for the machine's volumes rather than a folder, and
    `path` is ignored -- see DRIVES_PATH.

    Written BEFORE the command is queued, deliberately: an agent that is being held open on
    the command channel can claim the command microseconds after it exists, and answering a
    request row that has not been written yet is a 404 the operator would see as "the
    machine refused".
    """
    machine = str(machine or "").strip()
    if not machine:
        raise ValueError("machine is required")
    clean = DRIVES_PATH if drives else validate_path(path)
    request_id = uuid.uuid4().hex
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO file_listings(request_id, machine, path, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, machine, clean, PENDING, int(now if now is not None else time.time())),
        )
    return request_id, clean


def _as_int(value):
    # bool is an int in Python, so `{"size": true}` would otherwise arrive as a 1-byte file.
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_entry(raw):
    """One directory entry, trimmed to what the console renders. Returns None for anything
    without a usable name -- a row that cannot be clicked is noise."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:MAX_NAME_CHARS]
    if not name:
        return None
    size = _as_int(raw.get("size"))
    return {
        "name": name,
        # Anything that is not explicitly a directory renders as a file. The failure this
        # avoids is a mislabelled folder offering a Download button that streams nothing.
        "directory": bool(raw.get("directory")),
        "size": size if (size is not None and size >= 0) else None,
        "modified": _as_int(raw.get("modified")),
        # Windows attributes the console actually renders: hidden and system entries are
        # dimmed rather than omitted (an operator browsing ProgramData needs to see them),
        # and a reparse point is flagged because copying one does not do what it looks like.
        "hidden": bool(raw.get("hidden")),
        "system": bool(raw.get("system")),
        "readonly": bool(raw.get("readonly")),
        "link": bool(raw.get("link")),
    }


def record_listing(db_path, request_id, machine, payload, now=None):
    """Store one directory listing from an agent. Returns True if it was stored.

    Refuses a request that is not this machine's, and refuses to answer one twice -- an
    agent that retries a POST which actually landed must not overwrite a listing the
    operator is already reading with a second enumeration of a folder that has since
    changed.

    Malformed entries are DROPPED rather than raised on, exactly as processes.record_snapshot
    drops them: the agent has already done the work, and answering it 400 because one
    filename was odd would make it retry a report that landed.
    """
    row = _listing_row(db_path, request_id)
    if row is None or row["machine"] != str(machine or "").strip():
        return False
    if row["status"] != PENDING:
        return False
    if not isinstance(payload, dict):
        return False

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return False

    entries = []
    for raw in raw_entries[:MAX_ENTRIES]:
        entry = _clean_entry(raw)
        if entry is not None:
            entries.append(entry)

    dropped = max(0, _as_int(payload.get("truncated")) or 0)
    overflow = max(0, len(raw_entries) - MAX_ENTRIES)
    stored = {
        "entries": entries,
        # What the machine dropped before sending plus what we dropped on arrival -- one
        # number, because "you are not seeing everything" is one fact to an operator.
        "truncated": dropped + overflow,
        # Re-validated rather than echoed: this string goes into the console's breadcrumb,
        # and it arrived from a remote machine. A path we cannot make sense of falls back to
        # the one we asked about, which is the one the operator clicked.
        "path": _safe_path(payload.get("path"), row["path"]),
        "parent": _safe_path(payload.get("parent"), None),
        # A drive list, for the root view. Same treatment: names and labels are remote text.
        "drives": _clean_drives(payload.get("drives")),
    }
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE file_listings SET status = ?, payload_json = ?, answered_at = ? "
            "WHERE request_id = ? AND status = ?",
            (READY, json.dumps(stored), int(now if now is not None else time.time()),
             request_id, PENDING),
        )
    return True


def _safe_path(value, fallback):
    if not value:
        return fallback
    try:
        return validate_path(value)
    except ValueError:
        return fallback


def _clean_drives(raw_drives):
    """The machine's volumes, for the explorer's root view. Empty list when the agent sent
    none -- a listing of a folder has no drives to report and should not invent any."""
    if not isinstance(raw_drives, list):
        return []
    drives = []
    for raw in raw_drives[:64]:
        if not isinstance(raw, dict):
            continue
        try:
            path = validate_path(raw.get("path"))
        except ValueError:
            continue
        total = _as_int(raw.get("total_bytes"))
        free = _as_int(raw.get("free_bytes"))
        drives.append({
            "path": path,
            "label": str(raw.get("label") or "")[:MAX_NAME_CHARS],
            "type": str(raw.get("type") or "")[:32],
            "total_bytes": total if (total is not None and total >= 0) else None,
            "free_bytes": free if (free is not None and free >= 0) else None,
        })
    return drives


def fail_listing(db_path, request_id, machine, error, now=None):
    """Record that the machine could not answer -- access denied, path gone, disk offline.

    A refusal is a RESULT, not an error: "Access is denied" is the answer to "what is in
    this folder", it is what the operator needs to read, and rendering it as a failed
    request would send them looking for a fault in the console instead.
    """
    row = _listing_row(db_path, request_id)
    if row is None or row["machine"] != str(machine or "").strip():
        return False
    if row["status"] != PENDING:
        return False
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE file_listings SET status = ?, error = ?, answered_at = ? "
            "WHERE request_id = ? AND status = ?",
            (FAILED, str(error or "")[:500], int(now if now is not None else time.time()),
             request_id, PENDING),
        )
    return True


def _listing_row(db_path, request_id):
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT * FROM file_listings WHERE request_id = ?",
            (str(request_id or "").strip(),)
        ).fetchone()


def get_listing(db_path, request_id, machine=None):
    """One listing in the shape the console consumes, or None.

    `machine` is checked when given, and the console always gives it: a request id is a
    bearer of nothing -- the scope check upstream authorised a MACHINE, and a request id
    from another machine must not slip through it just because it was guessable.
    """
    row = _listing_row(db_path, request_id)
    if row is None:
        return None
    if machine is not None and row["machine"] != str(machine or "").strip():
        return None

    payload = {
        "request_id": row["request_id"],
        "machine": row["machine"],
        "path": row["path"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "answered_at": row["answered_at"],
        "entries": [],
        "drives": [],
        "truncated": 0,
        "parent": None,
    }
    if row["payload_json"]:
        try:
            stored = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            payload.update({k: v for k, v in stored.items() if k in payload})
    if payload["parent"] is None and row["status"] == READY:
        # Derived rather than required from the agent, so an older or terser agent still
        # gets a working "up" button.
        try:
            payload["parent"] = parent_path(row["path"])
        except ValueError:
            payload["parent"] = None
    return payload


def prune_listings(db_path, now=None, ttl=LISTING_TTL_SECONDS):
    """Drop listing rows past their TTL. Returns rows removed.

    Housekeeping only -- a stale listing is never served as fresh, because the console asks
    for a new one on every navigation. This just stops a table that gains a row per click
    from keeping every click forever.
    """
    cutoff = int(now if now is not None else time.time()) - int(ttl)
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM file_listings WHERE created_at <= ?", (cutoff,))
        return cur.rowcount or 0


# ---------------- transfers ----------------
def create_transfer(db_path, machine, direction, path, name, kind=KIND_FILE,
                    issued_by=None, spool=None, size_bytes=None, status=PENDING, now=None):
    """Open a transfer row. Returns its id.

    A `pull` starts PENDING with no spool -- the bytes do not exist yet. A `push` starts
    READY with the spool already written, because the operator's browser handed us the file
    before we ever told the machine about it. That asymmetry is the whole difference between
    the two directions and it is why one table serves both.
    """
    machine = str(machine or "").strip()
    if not machine:
        raise ValueError("machine is required")
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown transfer direction: {direction!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown transfer kind: {kind!r}")
    clean_path = validate_path(path)
    clean_name = validate_name(name)
    transfer_id = uuid.uuid4().hex
    stamp = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO file_transfers(id, machine, direction, path, name, kind, spool, "
            "size_bytes, status, issued_by, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (transfer_id, machine, direction, clean_path, clean_name, kind, spool,
             size_bytes, status, str(issued_by or ""), stamp, stamp + TRANSFER_TTL_SECONDS),
        )
    return transfer_id


def get_transfer(db_path, transfer_id, machine=None):
    """One transfer as a dict, or None. `machine` is checked when given, for the same reason
    get_listing checks it: an id is not an authorisation."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM file_transfers WHERE id = ?",
                           (str(transfer_id or "").strip(),)).fetchone()
    if row is None:
        return None
    if machine is not None and row["machine"] != str(machine or "").strip():
        return None
    return dict(row)


def mark_transfer_ready(db_path, transfer_id, spool, size_bytes):
    """The bytes have landed and the spool file is complete. Only a PENDING transfer moves,
    so a retried upload cannot replace a file the operator is already downloading."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE file_transfers SET status = ?, spool = ?, size_bytes = ? "
            "WHERE id = ? AND status = ?",
            (READY, spool, int(size_bytes), str(transfer_id or "").strip(), PENDING),
        )
        return (cur.rowcount or 0) > 0


def arm_push(db_path, transfer_id, path, name):
    """Bind a spooled upload to its destination and let the machine fetch it.

    The second half of a two-step upload, and the split is a CSRF control rather than an
    ergonomic one. A multipart POST is the one state-changing request a cross-site HTML form
    can still make, so the endpoint that RECEIVES the bytes is deliberately inert -- it
    spools them and hands back an id, creating no command and touching no machine. This is
    the step that gives those bytes a meaning, it takes JSON, and JSON is what the hub's CSRF
    rule covers. Exactly the arrangement packages.upload_package_file already uses; see
    app.CSRF_UPLOAD_ENDPOINTS.

    Only a PENDING push moves, so a second call cannot re-aim bytes an agent is already
    fetching.
    """
    clean_path = validate_path(path)
    clean_name = validate_name(name)
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE file_transfers SET status = ?, path = ?, name = ? "
            "WHERE id = ? AND status = ? AND direction = ?",
            (READY, clean_path, clean_name, str(transfer_id or "").strip(), PENDING, PUSH),
        )
        return (cur.rowcount or 0) > 0


def fail_transfer(db_path, transfer_id, error):
    """The machine could not read the file, or the bytes never arrived. Terminal: a failed
    transfer is not retried in place, the operator clicks again and gets a new one."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE file_transfers SET status = ?, error = ? WHERE id = ? AND status != ?",
            (FAILED, str(error or "")[:500], str(transfer_id or "").strip(), FAILED),
        )
        return (cur.rowcount or 0) > 0


def spool_path(spool_dir, spool):
    """Where one transfer's bytes live on the hub's disk.

    The join happens HERE and only here, and it re-checks that the stored name is a bare
    filename. The name is written by this module and never by a request, so this is not
    defence against a caller -- it is defence against a future caller, and against a
    database restored from somewhere else.
    """
    name = str(spool or "").strip()
    if not name or name != os.path.basename(name) or name in (".", ".."):
        raise ValueError("invalid spool name")
    return os.path.join(spool_dir, name)


def new_spool_name(transfer_id):
    """The spool filename for a transfer. The id, and nothing from the operator's filename:
    a name that came off a remote machine has no business being a name on the hub's disk."""
    return f"{str(transfer_id or '').strip()}.bin"


def discard_spool(spool_dir, spool):
    """Delete a spooled file, ignoring the case where it is already gone. Never raises --
    every caller is on a path where the transfer is over and a leftover file is a disk
    problem to notice later, not a request to fail now."""
    if not spool:
        return
    try:
        os.remove(spool_path(spool_dir, spool))
    except (OSError, ValueError):
        pass


def prune_transfers(db_path, spool_dir, now=None):
    """Drop expired transfers AND the bytes behind them. Returns rows removed.

    This is the only thing that bounds the spool directory, so it deletes the file first and
    the row second: a row without its file renders as an expired download, while a file
    without its row is disk nothing will ever reclaim.
    """
    cutoff = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, spool FROM file_transfers WHERE expires_at <= ?", (cutoff,)
        ).fetchall()
        for row in rows:
            discard_spool(spool_dir, row["spool"])
        conn.execute("DELETE FROM file_transfers WHERE expires_at <= ?", (cutoff,))
    return len(rows)


def forget_machine(db_path, machine, spool_dir=None):
    """Drop everything this module holds for a machine. Called from the machine-delete path
    beside fleet.delete_machine, so a decommissioned PC leaves no listings and no spooled
    bytes behind."""
    machine = str(machine or "").strip()
    if not machine:
        return
    with get_conn(db_path) as conn:
        if spool_dir:
            for row in conn.execute(
                    "SELECT spool FROM file_transfers WHERE machine = ?", (machine,)):
                discard_spool(spool_dir, row["spool"])
        conn.execute("DELETE FROM file_listings WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM file_transfers WHERE machine = ?", (machine,))
