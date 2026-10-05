"""Full-depth disk usage history: the size of every folder and file on every day -- roadmap #27.

**What this answers.** "How big was this folder (or this one file) on any day this year", "which
files grew yesterday", and "what did this folder look like last Tuesday, beside today". disk_usage
.py answers the volume-level questions from small daily rows; this module answers them for every
path on the disk, which is a different scale of data and is stored differently on purpose.

**The scale.** A Windows system drive is 200-300 thousand folders and up to a million and a half
files. Stored as rows in the hub's database, a full copy a day is a billion rows a year for a
modest fleet, in the one file every backup copies and every query shares. So:

  * **One SQLite file per machine and volume**, under `<LOG_DIR>/disk_history/<machine>/<C>.db`,
    never in the main database. Forgetting a machine is deleting a directory; a slow query on one
    PC's history cannot hold a lock anything else needs.
  * **A change log, not snapshots.** `changes` holds a row only when an entry's size, size on
    disk or file count CHANGED: "from this day on, the value is X". The value on any day is the
    newest row on or before it. The agent sends exactly those rows (agent `TreeDelta.cs`), so a
    settled PC uploads a few thousand lines a day, not its whole disk.

**This is the "weekly full plus daily diffs" the operator chose, stored more compactly.** Every
day in the daily window can be rebuilt exactly. Past it, `prune` folds each week's changes into
its Monday, which is what keeping only Monday snapshots would have kept -- without storing every
unchanged file again each Monday. Past the keep window, everything older is folded into one
baseline day, so the history starts there instead of disappearing.

**Deltas only apply to the scan they were taken from.** Every upload names its base (the scan
time it was diffed against). If that is not the last scan this hub applied, `accept` answers
"need full", and the agent resends the whole tree diffed against nothing. That repairs a lost
upload, a restored hub, or a PC re-imaged under the same name; without it one lost day would leave
every path that changed that day wrong forever, with nothing on screen to say so.

**Uploads are spooled and applied off the request thread.** A full tree is a million and a half
lines and takes the better part of a minute to apply. The request writes the gzip to disk and
returns; a worker applies spooled uploads oldest first, per volume, one at a time.

**Rejected:** the full tree as rows in the main database (above); a snapshot file per day (two to
five gigabytes per PC per year, and "size of this file over a year" would open 365 files); paths as
the key of every change row (a path is ~80 bytes; an interned entry id is 4, which is most of the
difference between hundreds of megabytes and gigabytes per year); deriving a child's deletion
from its parent's (a folder deleted and recreated the same day would bring its old children back).

Kept free of Flask so it can be unit-tested in isolation.
"""
import gzip
import json
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import date, datetime

#: A compressed upload larger than this is refused. A full tree of a large, busy disk is tens of
#: megabytes; a gigabyte is a broken or hostile client.
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
#: Lines in one upload. Twenty million is more files than NTFS volumes in this fleet hold.
MAX_LINES = 20_000_000
#: Longest path accepted. Windows' long-path limit is 32k, but nothing an operator browses is
#: anywhere near this, and a cap keeps one hostile line from becoming a megabyte of index.
MAX_PATH_CHARS = 2048
MAX_NAME_CHARS = 255
#: 1 EiB, as in disk_usage. A larger number is a parsing bug on the machine.
MAX_BYTES = 1 << 60
#: Rows a "what changed" answer lists. A deleted node_modules is thousands of files, and the
#: console shows the biggest.
MAX_FILE_CHANGES = 200
#: Children one browse answer lists. Matches the agent's listing cap.
MAX_BROWSE_ENTRIES = 2000

ROOT_ID = 1
DELETED = -1

_lock = threading.Lock()          # serialises ingest; reads need none (WAL)
_wake = threading.Event()
_worker = None


# ================================
# DAYS
# ================================
# Days are stored as proleptic Gregorian ordinals (date.toordinal()): an int is four bytes
# against ten for "2026-10-05", on every one of millions of rows, and a Monday is arithmetic.
# Ordinal 1 (0001-01-01) is a Monday, so weekday = (day - 1) % 7 with Monday 0.

def day_number(epoch):
    """The hub-local calendar day of a timestamp. Hub-local for the reason disk_usage.day_of
    gives: one machine's points line up on one axis whichever arrived first."""
    return datetime.fromtimestamp(int(epoch)).date().toordinal()


def day_iso(number):
    return date.fromordinal(int(number)).isoformat()


def parse_day(text):
    """`YYYY-MM-DD` -> ordinal, or None."""
    try:
        return date.fromisoformat(str(text)).toordinal()
    except (TypeError, ValueError):
        return None


def monday_on_or_before(number):
    return number - (number - 1) % 7


# SQL for "the Monday on or after `day`": the bucket a day folds into in the weekly window.
_BUCKET = "(day + (7 - ((day - 1) % 7)) % 7)"


# ================================
# STORAGE
# ================================
def machine_dir(root, machine):
    """A machine's directory. Hex of the name: any machine name becomes a safe, reversible
    directory name, with no case-folding collisions on a case-insensitive filesystem."""
    return os.path.join(root, str(machine).encode("utf-8").hex())


def volume_path(root, machine, volume):
    return os.path.join(machine_dir(root, machine), f"{volume[0].upper()}.db")


def _connect(path, create=True):
    if not create and not os.path.isfile(path):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id     INTEGER PRIMARY KEY,
            parent INTEGER NOT NULL,
            name   TEXT NOT NULL COLLATE NOCASE,
            is_dir INTEGER NOT NULL,
            UNIQUE (parent, name)
        );
        CREATE TABLE IF NOT EXISTS changes (
            entry INTEGER NOT NULL,
            day   INTEGER NOT NULL,
            size  INTEGER NOT NULL,
            alloc INTEGER NOT NULL,
            files INTEGER NOT NULL,
            PRIMARY KEY (entry, day)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_changes_day ON changes(day);
        CREATE TABLE IF NOT EXISTS current (
            entry INTEGER PRIMARY KEY,
            size  INTEGER NOT NULL,
            alloc INTEGER NOT NULL,
            files INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scans (
            day        INTEGER PRIMARY KEY,
            scanned_at INTEGER NOT NULL,
            full       INTEGER NOT NULL,
            lines      INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value);
        INSERT OR IGNORE INTO entries(id, parent, name, is_dir) VALUES (1, 0, '', 1);
        """
    )
    return conn


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def volumes(root, machine):
    """The volume letters with history for this machine, as `C:`."""
    directory = machine_dir(root, machine)
    if not os.path.isdir(directory):
        return []
    return sorted(f"{name[0]}:" for name in os.listdir(directory)
                  if len(name) == 4 and name.endswith(".db") and name[0].isalpha())


# ================================
# ACCEPTING AN UPLOAD
# ================================
def _spool_dir(root):
    return os.path.join(root, "spool")


def _spooled(root):
    """Every spooled upload's metadata, oldest scan first."""
    directory = _spool_dir(root)
    if not os.path.isdir(directory):
        return []
    items = []
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            continue
        meta["_id"] = name[:-5]
        items.append(meta)
    return sorted(items, key=lambda m: (int(m.get("scanned_at") or 0), m["_id"]))


def last_scanned_at(root, machine, volume):
    """The scan time the next delta for this volume must be based on: the newest one queued,
    else the newest one applied, else None."""
    queued = [int(m["scanned_at"]) for m in _spooled(root)
              if m.get("machine") == machine and m.get("volume") == volume]
    if queued:
        return max(queued)
    conn = _connect(volume_path(root, machine, volume), create=False)
    if conn is None:
        return None
    try:
        value = _meta(conn, "last_scanned_at")
        return int(value) if value is not None else None
    finally:
        conn.close()


def accept(root, machine, volume, scanned_at, base, full, stream, length):
    """Take one upload. Returns `"stored"`, `"duplicate"` or `"need_full"`.

    Raises ValueError for an upload that is not acceptable at all (too big, no length).
    The body is spooled to disk in 1 MB chunks and applied later by the worker.
    """
    if not length or length <= 0:
        raise ValueError("a Content-Length is required")
    if length > MAX_UPLOAD_BYTES:
        raise ValueError("that upload is larger than this hub accepts")

    last = last_scanned_at(root, machine, volume)
    if last is not None and scanned_at <= last:
        # The agent retried an upload that already landed. Saying "stored" lets it delete it.
        return "duplicate"
    if not full and (base is None or base != last):
        return "need_full"

    directory = _spool_dir(root)
    os.makedirs(directory, exist_ok=True)
    spool_id = uuid.uuid4().hex
    data_path = os.path.join(directory, spool_id + ".gz")
    written = 0
    try:
        with open(data_path, "wb") as handle:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > length or written > MAX_UPLOAD_BYTES:
                    raise ValueError("body is longer than its Content-Length")
                handle.write(chunk)
    except (OSError, ValueError):
        _remove(data_path)
        raise
    # The metadata is written last: a spool entry without it is invisible to the worker, so a
    # crash mid-body leaves an orphan file rather than half a tree applied as a whole one.
    with open(os.path.join(directory, spool_id + ".json"), "w", encoding="utf-8") as handle:
        json.dump({"machine": machine, "volume": volume, "scanned_at": int(scanned_at),
                   "full": bool(full)}, handle)
    _wake.set()
    return "stored"


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ================================
# APPLYING AN UPLOAD
# ================================
def process_spool(root):
    """Apply every spooled upload, oldest scan first. Returns how many were applied."""
    applied = 0
    with _lock:
        for meta in _spooled(root):
            directory = _spool_dir(root)
            data_path = os.path.join(directory, meta["_id"] + ".gz")
            try:
                ingest(root, meta["machine"], meta["volume"], int(meta["scanned_at"]),
                       bool(meta.get("full")), data_path)
                applied += 1
            except Exception as e:  # noqa: BLE001 -- one bad upload must not stop the rest
                print(f"[disk-history] Could not apply an upload for {meta.get('machine')!r} "
                      f"{meta.get('volume')}: {e}")
            finally:
                _remove(data_path)
                _remove(os.path.join(directory, meta["_id"] + ".json"))
    return applied


def _clean_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= MAX_BYTES else None


class _Resolver:
    """Path -> entry id, creating entries as needed. Folder ids are cached for the length of
    one upload; file ids are not, because there are millions and each is seen once."""

    def __init__(self, conn, volume, create):
        self.conn = conn
        self.volume = volume
        self.create = create
        self.dirs = {volume.lower() + "\\": ROOT_ID}

    def split(self, path):
        """`C:\\a\\b` -> ["a", "b"], or None when the path is not on this volume or is unsafe."""
        if not isinstance(path, str) or len(path) > MAX_PATH_CHARS or len(path) < 3:
            return None
        if path[:3].lower() != self.volume.lower() + "\\":
            return None
        if any(ord(ch) < 32 for ch in path):
            return None
        parts = [p for p in path[3:].split("\\") if p]
        if any(len(p) > MAX_NAME_CHARS or p in (".", "..") for p in parts):
            return None
        return parts

    def resolve(self, parts, is_dir):
        """The id of the entry at `parts`, or None (missing and not creating)."""
        if not parts:
            return ROOT_ID
        parent = ROOT_ID
        key = self.volume.lower() + "\\"
        for i, name in enumerate(parts):
            last = i == len(parts) - 1
            key = key + name.lower() + ("" if last else "\\")
            cached = None if last else self.dirs.get(key)
            if cached is not None:
                parent = cached
                continue
            row = self.conn.execute("SELECT id, name, is_dir FROM entries "
                                    "WHERE parent = ? AND name = ?", (parent, name)).fetchone()
            want_dir = 1 if (is_dir or not last) else 0
            if row is None:
                if not self.create:
                    return None
                entry = self.conn.execute(
                    "INSERT INTO entries(parent, name, is_dir) VALUES (?, ?, ?)",
                    (parent, name, want_dir)).lastrowid
            else:
                entry = row["id"]
                if self.create and (row["is_dir"] != want_dir or row["name"] != name):
                    # Same name now a different kind (a file replaced by a folder), or the
                    # same name in a new case. Keep the entry, so its history continues.
                    self.conn.execute("UPDATE entries SET is_dir = ?, name = ? WHERE id = ?",
                                      (want_dir, name, entry))
            if not last:
                self.dirs[key] = entry
            parent = entry
        return parent


def ingest(root, machine, volume, scanned_at, full, gz_path):
    """Apply one upload: write a change row for every line whose value differs from what this
    hub holds, and, for a full tree, a deletion for everything it holds that the tree lacks.

    One transaction: a crash halfway leaves the previous state, and the agent's next delta then
    fails its base check and becomes a full tree. Never half a day.
    """
    day = day_number(scanned_at)
    conn = _connect(volume_path(root, machine, volume))
    lines = 0
    try:
        with conn:
            if full:
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen (entry INTEGER PRIMARY KEY)")
                conn.execute("DELETE FROM seen")
            resolver = _Resolver(conn, volume, create=True)
            with gzip.open(gz_path, "rt", encoding="utf-8") as text:
                for raw in text:
                    lines += 1
                    if lines > MAX_LINES:
                        raise ValueError("too many lines in one upload")
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    parts = resolver.split(obj.get("p"))
                    if parts is None:
                        continue
                    if obj.get("x"):
                        _apply_gone(conn, resolver, parts, day)
                        continue
                    size, alloc = _clean_number(obj.get("s")), _clean_number(obj.get("a"))
                    files = _clean_number(obj.get("f")) or 0
                    if size is None or alloc is None:
                        continue
                    entry = resolver.resolve(parts, bool(obj.get("d")))
                    _apply_value(conn, entry, day, size, alloc, files)
                    if full:
                        conn.execute("INSERT OR IGNORE INTO seen(entry) VALUES (?)", (entry,))
            if full:
                gone = [r["entry"] for r in conn.execute(
                    "SELECT entry FROM current WHERE entry NOT IN (SELECT entry FROM seen)")]
                for entry in gone:
                    _mark_gone(conn, entry, day)
                conn.execute("DELETE FROM seen")
            conn.execute("INSERT OR REPLACE INTO scans(day, scanned_at, full, lines) "
                         "VALUES (?, ?, ?, ?)", (day, int(scanned_at), 1 if full else 0, lines))
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_scanned_at', ?)",
                         (int(scanned_at),))
    finally:
        conn.close()
    return lines


def _apply_value(conn, entry, day, size, alloc, files):
    held = conn.execute("SELECT size, alloc, files FROM current WHERE entry = ?",
                        (entry,)).fetchone()
    if held is not None and (held["size"], held["alloc"], held["files"]) == (size, alloc, files):
        return
    conn.execute("INSERT OR REPLACE INTO changes(entry, day, size, alloc, files) "
                 "VALUES (?, ?, ?, ?, ?)", (entry, day, size, alloc, files))
    conn.execute("INSERT OR REPLACE INTO current(entry, size, alloc, files) VALUES (?, ?, ?, ?)",
                 (entry, size, alloc, files))


def _apply_gone(conn, resolver, parts, day):
    entry = _find(conn, resolver, parts)
    if entry is not None:
        _mark_gone(conn, entry, day)


def _find(conn, resolver, parts):
    finder = _Resolver(conn, resolver.volume, create=False)
    finder.dirs = resolver.dirs
    return finder.resolve(parts, False)


def _mark_gone(conn, entry, day):
    if conn.execute("SELECT 1 FROM current WHERE entry = ?", (entry,)).fetchone() is None:
        return
    conn.execute("INSERT OR REPLACE INTO changes(entry, day, size, alloc, files) "
                 "VALUES (?, ?, ?, ?, ?)", (entry, day, DELETED, DELETED, DELETED))
    conn.execute("DELETE FROM current WHERE entry = ?", (entry,))


# ================================
# READ
# ================================
def _open_read(root, machine, volume):
    return _connect(volume_path(root, machine, volume), create=False)


def _state_at(conn, entry, day):
    """(size, alloc, files) of `entry` on `day`, or None when it did not exist then."""
    row = conn.execute("SELECT size, alloc, files FROM changes WHERE entry = ? AND day <= ? "
                       "ORDER BY day DESC LIMIT 1", (entry, day)).fetchone()
    if row is None or row["size"] == DELETED:
        return None
    return row["size"], row["alloc"], row["files"]


def _path_of(conn, entry, volume, cache):
    """The full path of an entry. `cache` maps entry id -> path across calls, so a change list
    of two hundred files in the same few folders walks each folder once."""
    root = volume + "\\"
    chain = []
    current = entry
    while current != ROOT_ID and current not in cache:
        row = conn.execute("SELECT parent, name FROM entries WHERE id = ?", (current,)).fetchone()
        if row is None:
            break
        chain.append((current, row["name"]))
        current = row["parent"]
    path = cache.get(current, root)
    for entry_id, name in reversed(chain):
        path = path + name if path.endswith("\\") else path + "\\" + name
        cache[entry_id] = path
    return path


def scan_days(root, machine, volume):
    """Days with an applied scan, newest first, as `[{"day", "scanned_at", "full"}]`."""
    conn = _open_read(root, machine, volume)
    if conn is None:
        return []
    try:
        return [{"day": day_iso(r["day"]), "scanned_at": r["scanned_at"], "full": bool(r["full"])}
                for r in conn.execute("SELECT day, scanned_at, full FROM scans ORDER BY day DESC")]
    finally:
        conn.close()


def series(root, machine, volume, path):
    """Every recorded value of one folder or file, oldest first.

    Change points only -- the value holds until the next point -- because that is what is
    stored, and expanding it to a row per day would send 365 copies of an unchanged number.
    The console draws it as a stepped line. `deleted: true` points are where it went away.
    """
    conn = _open_read(root, machine, volume)
    empty = {"path": path, "volume": volume, "known": False, "directory": None, "points": []}
    if conn is None:
        return empty
    try:
        resolver = _Resolver(conn, volume, create=False)
        parts = resolver.split(_normalise(path, volume))
        entry = resolver.resolve(parts, False) if parts is not None else None
        if entry is None:
            return empty
        is_dir = conn.execute("SELECT is_dir FROM entries WHERE id = ?", (entry,)).fetchone()
        points = [{"day": day_iso(r["day"]), "size": None if r["size"] == DELETED else r["size"],
                   "allocated": None if r["alloc"] == DELETED else r["alloc"],
                   "files": None if r["files"] == DELETED else r["files"],
                   "deleted": r["size"] == DELETED}
                  for r in conn.execute("SELECT day, size, alloc, files FROM changes "
                                        "WHERE entry = ? ORDER BY day", (entry,))]
        first = conn.execute("SELECT MIN(day) AS d FROM scans").fetchone()["d"]
        return {"path": path, "volume": volume, "known": True,
                "directory": bool(is_dir["is_dir"]) if is_dir else None,
                "first_scan": day_iso(first) if first else None, "points": points}
    finally:
        conn.close()


def file_changes(root, machine, volume, day=None, limit=MAX_FILE_CHANGES):
    """The files that grew, shrank, appeared or went away at one scan, biggest change first.

    Compared with the value before that day. `first: true` is a volume's first scan on this
    hub: every file in it is "new", which is not a change anybody needs listed.
    """
    conn = _open_read(root, machine, volume)
    result = {"day": None, "first": False, "files": []}
    if conn is None:
        return result
    try:
        if day is None:
            row = conn.execute("SELECT MAX(day) AS d FROM scans").fetchone()
            day = row["d"]
        if day is None:
            return result
        result["day"] = day_iso(day)
        previous = conn.execute("SELECT MAX(day) AS d FROM scans WHERE day < ?",
                                (day,)).fetchone()["d"]
        if previous is None:
            result["first"] = True
            return result
        rows = conn.execute(
            "SELECT c.entry, c.size, c.alloc FROM changes c JOIN entries e ON e.id = c.entry "
            "WHERE c.day = ? AND e.is_dir = 0", (day,)).fetchall()
        changed = []
        for row in rows:
            before = conn.execute("SELECT size, alloc FROM changes WHERE entry = ? AND day < ? "
                                  "ORDER BY day DESC LIMIT 1", (row["entry"], day)).fetchone()
            was = before is not None and before["size"] != DELETED
            now = row["size"] != DELETED
            old_alloc = before["alloc"] if was else 0
            new_alloc = row["alloc"] if now else 0
            old_size = before["size"] if was else 0
            new_size = row["size"] if now else 0
            status = "changed" if (was and now) else "new" if now else "deleted"
            changed.append((abs(new_alloc - old_alloc), abs(new_size - old_size), row["entry"],
                            status, old_size, new_size, new_alloc - old_alloc))
        changed.sort(key=lambda c: (c[0], c[1]), reverse=True)
        cache = {}
        result["total"] = len(changed)
        result["files"] = [{
            "path": _path_of(conn, c[2], volume, cache), "status": c[3], "before": c[4],
            "after": c[5], "delta": c[6],
        } for c in changed[:max(1, int(limit))]]
        return result
    finally:
        conn.close()


def browse(root, machine, volume, path, day):
    """One folder's contents as they were on `day`, with the sizes of that day.

    `exists: false` when the folder itself did not exist then. Children that did not exist on
    that day are left out; children that existed then and are gone now are included, which is
    the point of looking at a past day.
    """
    conn = _open_read(root, machine, volume)
    result = {"path": path, "volume": volume, "day": day_iso(day), "exists": False,
              "entries": [], "truncated": 0}
    if conn is None:
        return result
    try:
        resolver = _Resolver(conn, volume, create=False)
        parts = resolver.split(_normalise(path, volume))
        entry = resolver.resolve(parts, True) if parts is not None else None
        if entry is None or (entry != ROOT_ID and _state_at(conn, entry, day) is None):
            return result
        result["exists"] = True
        children = conn.execute("SELECT id, name, is_dir FROM entries WHERE parent = ? "
                                "ORDER BY is_dir DESC, name", (entry,)).fetchall()
        for child in children:
            state = _state_at(conn, child["id"], day)
            if state is None:
                continue
            if len(result["entries"]) >= MAX_BROWSE_ENTRIES:
                result["truncated"] += 1
                continue
            result["entries"].append({
                "name": child["name"], "directory": bool(child["is_dir"]),
                "size": state[0], "allocated": state[1],
                "files": state[2] if child["is_dir"] else None,
            })
        return result
    finally:
        conn.close()


def _normalise(path, volume):
    text = str(path or "").strip().replace("/", "\\")
    if len(text) == 2:
        text += "\\"
    if len(text) > 3:
        text = text.rstrip("\\")
    return text


# ================================
# RETENTION
# ================================
def prune(root, daily_days, keep_days, today=None):
    """Thin every volume's history: daily for `daily_days`, then each week folded into its
    Monday, then everything older than `keep_days` folded into one baseline day. Returns how
    many change rows went.

    Folding, not deleting: the value on a day is the newest row on or before it, so dropping an
    old row would change what every LATER day reads as for any entry that has not changed
    since. A file untouched for two years must still have a size today.
    """
    today = (today or date.today()).toordinal()
    keep_days = max(int(keep_days), int(daily_days))
    m0 = monday_on_or_before(today - int(daily_days))
    k = monday_on_or_before(today - keep_days)
    dropped = 0
    if not os.path.isdir(root):
        return 0
    for machine_name in os.listdir(root):
        directory = os.path.join(root, machine_name)
        if machine_name == "spool" or not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not name.endswith(".db"):
                continue
            with _lock:
                dropped += _prune_one(os.path.join(directory, name), m0, k)
    return dropped


def _prune_one(path, m0, k):
    conn = _connect(path, create=False)
    if conn is None:
        return 0
    dropped = 0
    try:
        with conn:
            # Weekly window: within each Monday bucket keep only the newest row per entry...
            dropped += conn.execute(
                f"DELETE FROM changes WHERE day <= ? AND EXISTS (SELECT 1 FROM changes c2 "
                f"WHERE c2.entry = changes.entry AND c2.day > changes.day "
                f"AND c2.day <= {_BUCKET.replace('day', 'changes.day')})", (m0,)).rowcount
            # ...and move it to the Monday. No collision: nothing newer is left in the bucket.
            conn.execute(f"UPDATE changes SET day = {_BUCKET} WHERE day <= ? AND (day - 1) % 7 != 0",
                         (m0,))
            conn.execute(f"DELETE FROM scans WHERE day <= ? AND EXISTS (SELECT 1 FROM scans s2 "
                         f"WHERE s2.day > scans.day AND s2.day <= {_BUCKET.replace('day', 'scans.day')})",
                         (m0,))
            conn.execute(f"UPDATE scans SET day = {_BUCKET} WHERE day <= ? AND (day - 1) % 7 != 0",
                         (m0,))
            # Past the keep window: one baseline at k holding each entry's last value before it.
            dropped += conn.execute(
                "DELETE FROM changes WHERE day < ? AND EXISTS (SELECT 1 FROM changes c2 "
                "WHERE c2.entry = changes.entry AND c2.day > changes.day AND c2.day <= ?)",
                (k, k)).rowcount
            dropped += conn.execute("DELETE FROM changes WHERE day < ? AND size = ?",
                                    (k, DELETED)).rowcount
            conn.execute("UPDATE changes SET day = ? WHERE day < ?", (k, k))
            moved = conn.execute("DELETE FROM scans WHERE day < ?", (k,)).rowcount
            if moved:
                conn.execute("INSERT OR IGNORE INTO scans(day, scanned_at, full, lines) "
                             "VALUES (?, 0, 1, 0)", (k,))
            # Entries nothing refers to any more: no history, no children.
            while True:
                gone = conn.execute(
                    "DELETE FROM entries WHERE id != 1 AND id NOT IN (SELECT entry FROM changes) "
                    "AND id NOT IN (SELECT parent FROM entries)").rowcount
                if not gone:
                    break
    finally:
        conn.close()
    return dropped


# ================================
# LIFECYCLE
# ================================
def forget_machine(root, machine):
    """Erase a deleted machine's history. Its paths name the people who used it."""
    with _lock:
        shutil.rmtree(machine_dir(root, machine), ignore_errors=True)


def rename_machine(root, old_machine, new_machine):
    """Move history during a duplicate-serial merge. The survivor's own history wins where both
    have one: its agent is the one still reporting, and the next delta is based on its scans."""
    with _lock:
        source = machine_dir(root, old_machine)
        target = machine_dir(root, new_machine)
        if not os.path.isdir(source):
            return
        if os.path.isdir(target):
            shutil.rmtree(source, ignore_errors=True)
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(source, target)


def start_worker(root, interval=5.0):
    """Apply spooled uploads in the background, for the life of the hub process."""
    global _worker
    if _worker is not None:
        return _worker

    def run():
        while True:
            _wake.wait(interval)
            _wake.clear()
            try:
                process_spool(root)
            except Exception as e:  # noqa: BLE001 -- the worker must outlive any one upload
                print(f"[disk-history] Worker pass failed: {e}")

    _worker = threading.Thread(target=run, name="disk-history", daemon=True)
    _worker.start()
    _wake.set()   # pick up anything a previous process left in the spool
    return _worker
