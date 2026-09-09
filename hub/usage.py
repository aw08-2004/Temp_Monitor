"""How long each app was in the foreground -- roadmap #23 phase E.

**This is the most personal thing the product stores, and the second entry in SECURITY.MD's
personal-data inventory.** A location fix says where a device was when somebody asked. This says
what the person carrying it spent their day doing, in six-minute resolution, every day. It
exists because screen-time budgets cannot be enforced without counting, and because an operator
setting a budget has to be able to see what a reasonable one would be -- but nothing about that
makes the record less personal, and the controls are sized accordingly:

  * Reading is `view` **plus machine scope**, like location. There is no fleet-wide "who uses
    what" endpoint, deliberately: the question this data can answer at fleet scale is not one
    the product should make easy to ask.
  * `data.usage_retention_days` defaults to **14**, which is half of what the location history
    keeps and a fifth of what readings keep. It is meant to be turned down.
  * `forget_machine` erases all of it, immediately.

**Days, not events.** The device reports a per-day total per package, never a session log. That
is a deliberate loss of resolution: a session log would say when somebody opened an app, which
is a minute-by-minute record of a person's evening, and the budgets this feature exists for need
only the total. What cannot be reconstructed from what is stored cannot leak from it.

**The day is the DEVICE's local day**, and it arrives as a `YYYY-MM-DD` string rather than a
timestamp. A budget resetting at midnight has to reset at midnight where the person is, not
where the hub is -- a fleet spread over two time zones would otherwise have half its curfews at
the wrong hour. The hub therefore never converts these: it stores what the device called the
day, and the device is the only thing that decides.

Kept free of Flask so it can be unit-tested in isolation.
"""
import re
import sqlite3
import time

# ================================
# INGEST BOUNDS
# ================================
#: A device reports today plus a few days of catch-up after being offline. More than this in one
#: report is not a real device.
MAX_DAYS_PER_REPORT = 14
#: A phone has 150-400 packages and only a fraction see foreground time in a day. Matches the
#: app inventory's cap so the two agree about what "too many" means.
MAX_PACKAGES_PER_DAY = 1000
MAX_PACKAGE_CHARS = 255
#: 24 hours. A package reporting more foreground time than exists in a day is a bad reading, not
#: a heavy user -- clamped rather than refused, because one absurd figure should not cost the
#: whole day's report.
MAX_SECONDS_PER_DAY = 86_400

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_usage_db(db_path):
    """Create the usage table. Idempotent -- safe to call on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_usage (
                machine     TEXT NOT NULL,
                day         TEXT NOT NULL,
                package     TEXT NOT NULL,
                seconds     INTEGER NOT NULL DEFAULT 0,
                reported_at INTEGER NOT NULL,
                PRIMARY KEY (machine, day, package)
            )
            """
        )
        # The pruner sweeps by day across every machine, and it is the only query that does not
        # start from a machine. Without this it is a full scan on a table that gains a few
        # hundred rows per device per day.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_usage_day "
                     "ON machine_usage(day)")


# ================================
# INGEST
# ================================
def _clean(value, limit):
    return str(value if value is not None else "").strip()[:limit]


def _seconds(value):
    """A foreground total, or None if it is not one.

    Zero is kept rather than dropped: "this app was open for no time today" is a different
    report from "this app was not mentioned", and the first is what a device says about an app
    somebody stopped using.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return min(number, MAX_SECONDS_PER_DAY)


def record_usage(db_path, machine, payload, now=None):
    """Store a device's foreground totals. Returns how many rows were written.

    **Merged by day, not replaced wholesale**, which is the opposite of what the app inventory
    does and for a concrete reason: a device reports TODAY on every pass, and today's figure
    only ever grows. Replacing the machine's whole history on each report would erase every
    earlier day the moment a device came back from a week offline with only today in hand.

    Within a day the newest figure wins, because usage is cumulative: a later report for the
    same day is a larger number, and a smaller one means the device's own accounting reset --
    which the agent already guards with a high-water mark before it ever reaches here.

    Called from the heartbeat, so it is bounded, type-checked and never fatal at the caller.
    """
    machine = _clean(machine, 200)
    if not machine or not isinstance(payload, dict):
        return 0
    days = payload.get("days")
    if not isinstance(days, dict):
        return 0

    now = int(time.time()) if now is None else int(now)
    rows = []
    for day, packages in list(days.items())[:MAX_DAYS_PER_REPORT]:
        day = _clean(day, 10)
        # A day that is not a date is dropped rather than stored: it becomes a primary key, an
        # ordering, and a retention decision, and none of those work on "yesterday".
        if not _DAY_RE.match(day) or not isinstance(packages, dict):
            continue
        for package, seconds in list(packages.items())[:MAX_PACKAGES_PER_DAY]:
            package = _clean(package, MAX_PACKAGE_CHARS)
            value = _seconds(seconds)
            if package and value is not None:
                rows.append((machine, day, package, value, now))

    if not rows:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO machine_usage(machine, day, package, seconds, reported_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(machine, day, package) DO UPDATE SET "
            "seconds = excluded.seconds, reported_at = excluded.reported_at", rows)
    return len(rows)


# ================================
# READ
# ================================
def days_for(db_path, machine, limit=14):
    """Per-day totals for this machine, newest day first.

    The shape the console charts: one row per day with the total and the top packages. Assembled
    here rather than in JavaScript because the "top" cut is a decision -- a day has a few hundred
    packages with a second each, and sending all of them to draw five bars is most of the payload.
    """
    machine = str(machine or "").strip()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT day, package, seconds FROM machine_usage WHERE machine = ? "
            "ORDER BY day DESC, seconds DESC",
            (machine,)).fetchall()

    by_day = {}
    for row in rows:
        entry = by_day.setdefault(row["day"], {"day": row["day"], "total": 0, "packages": []})
        entry["total"] += row["seconds"]
        entry["packages"].append({"package": row["package"], "seconds": row["seconds"]})
    ordered = sorted(by_day.values(), key=lambda d: d["day"], reverse=True)
    return ordered[:max(1, min(365, int(limit or 14)))]


def totals_for(db_path, machine, days=7):
    """Foreground seconds per package over the last `days` reported days, biggest first.

    "Reported days" rather than calendar days on purpose: a device that was switched off for a
    week should show its last seven days of use, not seven mostly-empty rows. Which is also the
    honest answer to "how much does this person use TikTok" -- it is a rate over days the device
    was actually in use.
    """
    recent = [d["day"] for d in days_for(db_path, machine, limit=days)]
    if not recent:
        return []
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT package, SUM(seconds) AS seconds FROM machine_usage "
            f"WHERE machine = ? AND day IN ({','.join('?' for _ in recent)}) "
            f"GROUP BY package ORDER BY seconds DESC",
            [str(machine or "").strip(), *recent]).fetchall()
    return [{"package": r["package"], "seconds": r["seconds"]} for r in rows]


def day_totals(db_path, machine, day):
    """`{package: seconds}` for one day. What compliance compares a budget against."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT package, seconds FROM machine_usage WHERE machine = ? AND day = ?",
            (str(machine or "").strip(), str(day or "").strip())).fetchall()
    return {r["package"]: r["seconds"] for r in rows}


def get_usage(db_path, machine, days=14):
    """Everything the machine page's Usage fold renders.

    `reported_at: None` with no days is a device that has never reported -- every Windows PC,
    every Android device on an older agent, and every device where usage access was never
    granted. The console must render that as "we have not been told", and for the last of those
    it is the console's only chance to say the permission is missing.
    """
    rows = days_for(db_path, machine, limit=days)
    with get_conn(db_path) as conn:
        latest = conn.execute("SELECT MAX(reported_at) AS at FROM machine_usage "
                              "WHERE machine = ?", (str(machine or "").strip(),)).fetchone()
    return {
        "days": rows,
        "totals": totals_for(db_path, machine, days=7),
        "reported_at": latest["at"] if latest else None,
    }


# ================================
# RETENTION
# ================================
def prune(db_path, cutoff_day):
    """Drop usage older than `cutoff_day` (a YYYY-MM-DD string). Returns how many rows went.

    Compared as a STRING, which is exact for ISO dates and needs no timezone: the device decided
    what day it was, and the hub is not entitled to a second opinion about it. The alternative --
    converting a device's local day to a hub timestamp -- would prune a day early or late for
    every device not in the hub's own zone.
    """
    with get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM machine_usage WHERE day < ?",
                              (str(cutoff_day),))
    return cursor.rowcount or 0


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's usage history.

    The strongest case in the product for erasing on deletion: this is a record of what a person
    did with their evenings, and it has no argument at all for surviving the device.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_usage WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move usage history during a duplicate-serial merge. Rows MERGE, and a collision keeps the
    LARGER figure: both rows describe one device on one day, and usage is cumulative, so the
    bigger number is the one that was reported later."""
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO machine_usage(machine, day, package, seconds, reported_at) "
            "SELECT ?, day, package, seconds, reported_at FROM machine_usage WHERE machine = ? "
            "ON CONFLICT(machine, day, package) DO UPDATE SET "
            "seconds = MAX(seconds, excluded.seconds), "
            "reported_at = MAX(reported_at, excluded.reported_at)",
            (new_machine, old_machine))
        conn.execute("DELETE FROM machine_usage WHERE machine = ?", (old_machine,))
