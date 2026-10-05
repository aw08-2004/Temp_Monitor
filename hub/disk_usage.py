"""Disk usage over time: daily volume totals, a fill forecast, and what changed -- roadmap #27.

**What this answers.** "Which folder is filling this PC's disk, and when will it be full." The
live storage cards already say how full a volume is now. Neither question can be answered from
one reading, and until this existed the hub kept no per-volume history at all: `readings` holds
one percentage for one disk, for thirty days.

**Where the data comes from, in two strengths.**

  * `source='sensor'` -- one point per volume per day, taken from the `/volume/<letter>` sensors
    every Windows agent since 3.10.0 already sends on `/api/report` (`app._disk_volumes`).
    **This is what makes the forecast work on day one, for every machine, including agents
    that will never scan.** No agent upgrade is needed for "full in about 40 days".
  * `source='scan'` -- the daily MFT scan from agent 3.40.0 (`DiskUsageReporter.cs`). It adds
    what sensors cannot: the folder changes since the last scan and the new large files. **A scan point always wins over a sensor point for the same day**, and a sensor point
    never overwrites a scan point. A scan is a deliberate measurement; a sensor point is
    whatever the last 10-second reading before the hourly throttle said.

**Every folder and file is in disk_history.py, not here.** This module keeps the small,
volume-level rows the forecast and the summary need: one row per volume per day and one change
list per scan. The size history of every path on the disk is a different scale of data -- a
million and a half paths per system drive -- and lives in its own per-machine files, written
from the agent's daily delta upload. A first version tracked a few thousand chosen folders
here instead; it was replaced by full depth at the operator's request (ROADMAP #27).

**Retention is a rolling window that never thins fresh data** (`prune`). The newest
`data.disk_usage_daily_days` (90) keep every daily point. Older than that, only Mondays
survive, until `data.disk_usage_keep_days` (365). The forecast reads only the last
`FORECAST_WINDOW_DAYS`, which is always inside the daily window. Change lists are kept for the
daily window only, because a thinned list would show one day's diff and read like a week's.

**Rejected:** computing the folder change list on the hub (only the agent holds both full
trees, so only it can name the deepest folder that explains a change); a trend over raw
10-second readings
(the `readings` blob is throttled, kept for thirty days, and read back only as "the newest").

Kept free of Flask so it can be unit-tested in isolation.
"""
import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

# ================================
# INGEST BOUNDS
# ================================
#: A to Z. More volumes than drive letters is not a real report.
MAX_VOLUMES = 26
MAX_CHANGES = 200
MAX_LARGE_FILES = 50
MAX_PATH_CHARS = 1024
MAX_ERROR_CHARS = 500
#: The largest sane byte count: 1 EiB. Anything above is a parsing bug on some machine, and
#: storing it would put an absurd point on a chart that then scales everything else to zero.
MAX_BYTES = 1 << 60

#: How many daily points the forecast fits. A month is long enough to see past one big
#: download and short enough that a cleanup three months ago does not flatten today's trend.
FORECAST_WINDOW_DAYS = 30
#: Fewer points than this and the forecast says "not enough data" rather than guessing. A
#: week covers the weekday/weekend cycle of an office PC at least once.
FORECAST_MIN_POINTS = 7
#: The short-term rate is shown beside the 30-day one, because "it was filling at 2 GB a day
#: this week" is the thing an operator acts on when the long trend still looks calm.
SHORT_RATE_DAYS = 7
#: A fill date further away than this is reported as "not filling", not as a date in 2041.
MAX_FORECAST_DAYS = 3650

SOURCE_SCAN = "scan"
SOURCE_SENSOR = "sensor"

_VOLUME_RE = re.compile(r"^[A-Za-z]:$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_disk_usage_db(db_path):
    """Create the disk-usage tables. Idempotent -- safe to call on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disk_volume_daily (
                machine     TEXT NOT NULL,
                volume      TEXT NOT NULL,
                day         TEXT NOT NULL,
                total_bytes INTEGER NOT NULL,
                used_bytes  INTEGER NOT NULL,
                source      TEXT NOT NULL,
                recorded_at INTEGER NOT NULL,
                PRIMARY KEY (machine, volume, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disk_changes (
                machine             TEXT NOT NULL,
                volume              TEXT NOT NULL,
                day                 TEXT NOT NULL,
                scanned_at          INTEGER NOT NULL,
                previous_scanned_at INTEGER,
                payload_json        TEXT NOT NULL,
                PRIMARY KEY (machine, volume, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disk_scan_state (
                machine     TEXT NOT NULL,
                volume      TEXT NOT NULL,
                status      TEXT NOT NULL,
                reason      TEXT,
                scanned_at  INTEGER NOT NULL,
                duration_ms INTEGER,
                folders     INTEGER,
                files       INTEGER,
                PRIMARY KEY (machine, volume)
            )
            """
        )
        # The pruner sweeps by day across every machine, the one query that does not start
        # from a machine.
        for table in ("disk_volume_daily", "disk_changes"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_day ON {table}(day)")


# ================================
# HELPERS
# ================================
def day_of(epoch):
    """The hub-local calendar day of a timestamp, as YYYY-MM-DD.

    Hub-local, unlike usage.py's device-local day, and deliberately: these points are
    measurements the hub lines up on one axis for a forecast, and a sensor point and a scan
    point for the same machine must fall on the same day whichever arrived first.
    """
    return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d")


def _int(value, maximum=MAX_BYTES):
    """A non-negative int no larger than `maximum`, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > maximum:
        return None
    return number


def clean_volume(value):
    """`"c:"` / `"C:\\"` / `"C"` -> `"C:"`, or None."""
    text = str(value or "").strip().rstrip("\\/")
    if len(text) == 1 and text.isalpha():
        text += ":"
    return text.upper() if _VOLUME_RE.match(text) else None


def clean_path(value, volume):
    """A reported folder path that belongs to `volume`, or None.

    Remote text that ends up in a table, a chart caption and a link that opens the browser at
    that folder. So it must name the volume it was reported for (a C: report cannot plant a
    D: path), carry no control characters, and stay inside the length the explorer accepts.
    """
    text = str(value or "").strip().replace("/", "\\")
    if not text or len(text) > MAX_PATH_CHARS:
        return None
    if any(ord(ch) < 32 for ch in text):
        return None
    if not text[:2].upper() == volume:
        return None
    if len(text) == 2:
        text += "\\"
    if len(text) > 3:
        text = text.rstrip("\\")
    return text[:2].upper() + text[2:]


def _bytes_from_gb(gb):
    try:
        return int(round(float(gb) * 1024 ** 3))
    except (TypeError, ValueError):
        return None


# ================================
# INGEST
# ================================
def record_sensor_volumes(db_path, machine, volumes, now=None):
    """Store today's sensor point for each volume. Returns how many rows were written.

    `volumes` is `app._disk_volumes(...)`'s shape, restricted to entries that carry a
    `letter` (only the `/volume/<letter>` sensors do; the LHM percentage fallback has no
    size and cannot be charted in bytes, so it is ignored here).

    **Never overwrites a scan point.** The upsert's WHERE clause is the whole rule.
    """
    machine = str(machine or "").strip()
    if not machine or not isinstance(volumes, list):
        return 0
    now = int(time.time()) if now is None else int(now)
    day = day_of(now)
    rows = []
    for vol in volumes[:MAX_VOLUMES]:
        if not isinstance(vol, dict):
            continue
        letter = clean_volume(vol.get("letter"))
        total = _int(_bytes_from_gb(vol.get("total_gb")))
        used = _int(_bytes_from_gb(vol.get("used_gb")))
        if not letter or not total or used is None:
            continue
        rows.append((machine, letter, day, total, min(used, total), SOURCE_SENSOR, now))
    if not rows:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO disk_volume_daily(machine, volume, day, total_bytes, used_bytes, "
            "source, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(machine, volume, day) DO UPDATE SET "
            "total_bytes = excluded.total_bytes, used_bytes = excluded.used_bytes, "
            "recorded_at = excluded.recorded_at "
            "WHERE disk_volume_daily.source = 'sensor'", rows)
    return len(rows)


def record_scan(db_path, machine, payload, now=None):
    """Store one agent disk-usage report. Returns the number of volumes stored.

    Malformed parts are DROPPED rather than refused, the same rule files.record_listing and
    usage.record_usage follow: the agent did minutes of work, and answering 400 because one
    folder name was odd would make it retry a report that is otherwise fine. The caller still
    gets a 400 for a payload that is not a report at all.
    """
    machine = str(machine or "").strip()
    if not machine or not isinstance(payload, dict):
        return 0
    now = int(time.time()) if now is None else int(now)
    volumes = payload.get("volumes")
    skipped = payload.get("skipped")
    stored = 0

    with get_conn(db_path) as conn:
        for raw in (volumes if isinstance(volumes, list) else [])[:MAX_VOLUMES]:
            if _record_volume(conn, machine, raw, now):
                stored += 1
        for raw in (skipped if isinstance(skipped, list) else [])[:MAX_VOLUMES]:
            if not isinstance(raw, dict):
                continue
            letter = clean_volume(raw.get("volume"))
            if not letter:
                continue
            reason = str(raw.get("reason") or "error").strip()[:80]
            error = str(raw.get("error") or "").strip()[:MAX_ERROR_CHARS]
            conn.execute(
                "INSERT INTO disk_scan_state(machine, volume, status, reason, scanned_at) "
                "VALUES (?, ?, 'skipped', ?, ?) ON CONFLICT(machine, volume) DO UPDATE SET "
                "status = 'skipped', reason = excluded.reason, scanned_at = excluded.scanned_at",
                (machine, letter, f"{reason}: {error}" if error else reason, now))
    return stored


def _record_volume(conn, machine, raw, now):
    if not isinstance(raw, dict):
        return False
    letter = clean_volume(raw.get("volume"))
    total = _int(raw.get("total_bytes"))
    free = _int(raw.get("free_bytes"))
    if not letter or not total or free is None:
        return False
    # A scan time in the future, or more than a week old, is a machine with a broken clock.
    # The point is filed under the day it ARRIVED rather than dropped.
    scanned_at = _int(raw.get("scanned_at"), maximum=now + 300)
    if scanned_at is None or scanned_at < now - 7 * 86400:
        scanned_at = now
    day = day_of(scanned_at)
    used = max(0, total - min(free, total))

    conn.execute(
        "INSERT INTO disk_volume_daily(machine, volume, day, total_bytes, used_bytes, source, "
        "recorded_at) VALUES (?, ?, ?, ?, ?, 'scan', ?) ON CONFLICT(machine, volume, day) "
        "DO UPDATE SET total_bytes = excluded.total_bytes, used_bytes = excluded.used_bytes, "
        "source = 'scan', recorded_at = excluded.recorded_at",
        (machine, letter, day, total, used, now))

    changes = []
    for item in (raw.get("changes") if isinstance(raw.get("changes"), list) else [])[:MAX_CHANGES]:
        if not isinstance(item, dict):
            continue
        path = clean_path(item.get("path"), letter)
        before, after = _int(item.get("before")), _int(item.get("after"))
        if path is None or before is None or after is None:
            continue
        changes.append({"path": path, "before": before, "after": after, "delta": after - before})
    large = []
    for item in (raw.get("new_large_files") if isinstance(raw.get("new_large_files"), list)
                 else [])[:MAX_LARGE_FILES]:
        if not isinstance(item, dict):
            continue
        path = clean_path(item.get("path"), letter)
        size, allocated = _int(item.get("size")), _int(item.get("allocated"))
        if path is None or size is None:
            continue
        large.append({"path": path, "size": size, "allocated": allocated or 0})

    previous = _int(raw.get("previous_scanned_at"), maximum=now + 300)
    conn.execute(
        "INSERT INTO disk_changes(machine, volume, day, scanned_at, previous_scanned_at, "
        "payload_json) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(machine, volume, day) DO UPDATE SET "
        "scanned_at = excluded.scanned_at, previous_scanned_at = excluded.previous_scanned_at, "
        "payload_json = excluded.payload_json",
        (machine, letter, day, scanned_at, previous,
         json.dumps({"changes": changes, "new_large_files": large})))

    conn.execute(
        "INSERT INTO disk_scan_state(machine, volume, status, reason, scanned_at, duration_ms, "
        "folders, files) VALUES (?, ?, 'ok', NULL, ?, ?, ?, ?) "
        "ON CONFLICT(machine, volume) DO UPDATE SET status = 'ok', reason = NULL, "
        "scanned_at = excluded.scanned_at, duration_ms = excluded.duration_ms, "
        "folders = excluded.folders, files = excluded.files",
        (machine, letter, scanned_at, _int(raw.get("duration_ms"), maximum=86_400_000),
         _int(raw.get("folders"), maximum=1 << 40), _int(raw.get("files"), maximum=1 << 40)))
    return True


# ================================
# FORECAST
# ================================
def _slope(points):
    """Least-squares slope and r-squared of (x, y) points, or (None, None) with fewer than two
    distinct x values."""
    n = len(points)
    if n < 2:
        return None, None
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mean_x) ** 2 for p in points)
    if sxx == 0:
        return None, None
    sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    slope = sxy / sxx
    syy = sum((p[1] - mean_y) ** 2 for p in points)
    r2 = 1.0 if syy == 0 else (sxy * sxy) / (sxx * syy)
    return slope, r2


def forecast(points, today=None):
    """When will this volume be full, from its daily points.

    `points` is `[{"day": "YYYY-MM-DD", "used_bytes": n, "total_bytes": n}, ...]` in any order.
    Returns a dict whose `status` is one of:

      * `insufficient` -- fewer than FORECAST_MIN_POINTS days in the window. **No date is
        claimed from three points**; an operator acting on that would be acting on noise.
      * `not_filling`  -- flat or shrinking, or so slow the date is over ten years out.
      * `filling`      -- with `days_to_full` and `full_on`.

    `bytes_per_day` is the 30-day fitted rate and `bytes_per_day_7d` the last week's, both
    present whenever they can be computed. `confidence` comes from r-squared: a volume that grows
    steadily earns "high", one that jumps around earns "low" -- the date is still shown,
    but the console says how much to trust it.

    x is the calendar day, not the point index: a PC switched off over a holiday has a gap,
    and spacing its points evenly would make the growth either side look twice as fast.
    """
    today = today or date.today()
    cutoff = today - timedelta(days=FORECAST_WINDOW_DAYS)
    usable = []
    for p in points or []:
        try:
            d = date.fromisoformat(p["day"])
        except (KeyError, TypeError, ValueError):
            continue
        if d <= cutoff or d > today:
            continue
        usable.append((d, int(p["used_bytes"]), int(p["total_bytes"])))
    usable.sort()

    result = {"status": "insufficient", "points": len(usable), "window_days": FORECAST_WINDOW_DAYS,
              "bytes_per_day": None, "bytes_per_day_7d": None, "days_to_full": None,
              "full_on": None, "r2": None, "confidence": None}
    if not usable:
        return result

    xs = [((d - usable[0][0]).days, used) for d, used, _total in usable]
    slope, r2 = _slope(xs)
    week = [((d - usable[0][0]).days, used) for d, used, _total in usable
            if d > today - timedelta(days=SHORT_RATE_DAYS)]
    short, _ = _slope(week) if len(week) >= 3 else (None, None)
    result["bytes_per_day_7d"] = round(short) if short is not None else None

    if len(usable) < FORECAST_MIN_POINTS or slope is None:
        return result

    result["bytes_per_day"] = round(slope)
    result["r2"] = round(r2, 3)
    result["confidence"] = ("high" if r2 >= 0.8 and len(usable) >= 14
                            else "medium" if r2 >= 0.5 else "low")

    last_day, last_used, last_total = usable[-1]
    free = max(0, last_total - last_used)
    if slope <= 0:
        result["status"] = "not_filling"
        return result
    days = free / slope
    if days > MAX_FORECAST_DAYS:
        result["status"] = "not_filling"
        return result
    # Counted from TODAY, not from the last point: a PC last seen five days ago with ten
    # days of room left has five days left, not ten.
    remaining = max(0.0, (last_day - today).days + days)
    result.update({
        "status": "filling",
        "days_to_full": round(remaining, 1),
        "full_on": (today + timedelta(days=int(remaining))).isoformat(),
    })
    return result


# ================================
# READ
# ================================
def volume_points(db_path, machine, volume, days=365, today=None):
    """Daily points for one volume, oldest first."""
    since = ((today or date.today()) - timedelta(days=max(1, int(days)))).isoformat()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT day, total_bytes, used_bytes, source FROM disk_volume_daily "
            "WHERE machine = ? AND volume = ? AND day > ? ORDER BY day",
            (str(machine or "").strip(), volume, since)).fetchall()
    return [dict(r) for r in rows]


def get_summary(db_path, machine, today=None):
    """Every volume this hub knows about for this machine, with its latest point, forecast
    and last scan. What the file browser's drive rows and the History panel open with.

    A volume with only sensor points has `scan: null` -- an agent older than 3.40.0, or one
    that has not reached its first scan yet. That is a normal state, not an error.
    """
    machine = str(machine or "").strip()
    with get_conn(db_path) as conn:
        letters = {r["volume"] for r in conn.execute(
            "SELECT DISTINCT volume FROM disk_volume_daily WHERE machine = ?", (machine,))}
        states = {r["volume"]: dict(r) for r in conn.execute(
            "SELECT volume, status, reason, scanned_at, duration_ms, folders, files "
            "FROM disk_scan_state WHERE machine = ?", (machine,))}
    volumes = []
    for letter in sorted(letters | set(states)):
        points = volume_points(db_path, machine, letter, days=FORECAST_WINDOW_DAYS + 1,
                               today=today)
        latest = points[-1] if points else None
        state = states.get(letter)
        volumes.append({
            "volume": letter,
            "latest": latest,
            "forecast": forecast(points, today=today),
            "scan": {k: v for k, v in state.items() if k != "volume"} if state else None,
        })
    return {"machine": machine, "volumes": volumes}


def get_history(db_path, machine, volume, days=365, today=None):
    """One volume's daily points plus its forecast, for the History chart."""
    points = volume_points(db_path, machine, volume, days=days, today=today)
    return {"machine": str(machine or "").strip(), "volume": volume, "points": points,
            "forecast": forecast(points, today=today)}


def change_days(db_path, machine, volume):
    """The days with a stored change list, newest first."""
    with get_conn(db_path) as conn:
        return [r["day"] for r in conn.execute(
            "SELECT day FROM disk_changes WHERE machine = ? AND volume = ? ORDER BY day DESC",
            (str(machine or "").strip(), volume))]


def get_changes(db_path, machine, volume, day=None):
    """The change list for one day (default: the newest), or an empty shell when none.

    `previous_scanned_at: null` with an empty list is a machine's FIRST scan: there was
    nothing to compare against, and the console says so rather than "nothing changed".
    """
    machine = str(machine or "").strip()
    days = change_days(db_path, machine, volume)
    if day is not None and not _DAY_RE.match(str(day)):
        day = None
    wanted = day or (days[0] if days else None)
    shell = {"machine": machine, "volume": volume, "day": wanted, "days": days,
             "scanned_at": None, "previous_scanned_at": None, "changes": [],
             "new_large_files": []}
    if wanted is None:
        return shell
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT scanned_at, previous_scanned_at, payload_json FROM disk_changes "
            "WHERE machine = ? AND volume = ? AND day = ?", (machine, volume, wanted)).fetchone()
    if row is None:
        return shell
    try:
        stored = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        stored = {}
    shell.update({
        "scanned_at": row["scanned_at"],
        "previous_scanned_at": row["previous_scanned_at"],
        "changes": stored.get("changes") or [],
        "new_large_files": stored.get("new_large_files") or [],
    })
    return shell


# ================================
# RETENTION
# ================================
def prune(db_path, daily_days, keep_days, today=None):
    """Thin and expire disk-usage history. Returns how many rows went.

    A rolling window, so fresh data is never thinned:

      * newer than `daily_days`: every day is kept
      * between `daily_days` and `keep_days`: only Mondays are kept
      * older than `keep_days`: deleted
      * change lists: kept for `daily_days` only (see the module docstring)

    Day strings compare exactly as ISO dates. The weekday comes from SQLite's strftime('%w'),
    where Monday is '1'.
    """
    today = today or date.today()
    keep_days = max(int(keep_days), int(daily_days))
    daily_cutoff = (today - timedelta(days=int(daily_days))).isoformat()
    keep_cutoff = (today - timedelta(days=keep_days)).isoformat()
    dropped = 0
    with get_conn(db_path) as conn:
        for table in ("disk_volume_daily",):
            dropped += conn.execute(f"DELETE FROM {table} WHERE day < ?",
                                    (keep_cutoff,)).rowcount or 0
            dropped += conn.execute(
                f"DELETE FROM {table} WHERE day < ? AND strftime('%w', day) != '1'",
                (daily_cutoff,)).rowcount or 0
        dropped += conn.execute("DELETE FROM disk_changes WHERE day < ?",
                                (daily_cutoff,)).rowcount or 0
    return dropped


# ================================
# LIFECYCLE HOOKS
# ================================
_TABLES = ("disk_volume_daily", "disk_changes", "disk_scan_state")


def forget_machine(db_path, machine):
    """Drop a deleted machine's disk history. Folder paths name the people who used it."""
    with get_conn(db_path) as conn:
        for table in _TABLES:
            conn.execute(f"DELETE FROM {table} WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move disk history during a duplicate-serial merge. On a collision the SURVIVOR's row
    wins: both describe one disk on one day, and the survivor is the identity that is
    reporting now."""
    with get_conn(db_path) as conn:
        for table in _TABLES:
            conn.execute(f"UPDATE OR IGNORE {table} SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
            conn.execute(f"DELETE FROM {table} WHERE machine = ?", (old_machine,))
