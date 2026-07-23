"""Alerts store -- operator-facing conflicts surfaced from the rest of the hub.

Two kinds today:

* `duplicate_serial` -- the asset-inventory dedup (app.resolve_serial_group) auto-merges
  duplicate machines that share a BIOS serial whenever it can tell which record is stale,
  but it deliberately refuses to merge two machines that are BOTH online and reporting.
  That is a genuine collision only a human should resolve, so it lands here for an
  operator to pick a survivor and merge manually. Keyed on the serial; `machines` holds
  the colliding hostnames.
* `overheat` -- a machine whose AVERAGE temperature over the configured window is at or
  above the overheat threshold (app.evaluate_overheat_once). Keyed on the single
  `machine`; `detail` holds {avg_temp, threshold, window_seconds}.

There is at most one OPEN alert per subject -- (kind, serial) for duplicate_serial,
(kind, machine) for overheat. It is refreshed while the condition persists and moved to
`resolved` once it clears (the collision is gone; the average dropped back below
threshold), or to `dismissed` if an operator waves it off.

Kept free of Flask so it can be unit-tested in isolation, exactly like fleet.py; app.py
wires thin HTTP endpoints on top of these functions.
"""
import json
import sqlite3
import time

KIND_DUPLICATE_SERIAL = "duplicate_serial"
KIND_OVERHEAT = "overheat"

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_DISMISSED = "dismissed"


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_alerts_db(db_path):
    """Create the alerts table if absent. Idempotent -- safe to call next to
    app.init_db()/fleet.init_fleet_db() on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                kind          TEXT NOT NULL,
                serial_number TEXT,
                machines      TEXT,          -- JSON array of hostnames involved
                status        TEXT NOT NULL DEFAULT 'open',
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL
            )
            """
        )
        # Columns added after alerts first shipped. CREATE TABLE IF NOT EXISTS does
        # nothing to a table that already exists, so a hub upgrading needs these added
        # explicitly -- the same ALTER-if-missing pattern app.init_db() uses for readings.
        # `machine` is the subject of a per-machine alert (overheat); `detail` is a JSON
        # payload for kind-specific numbers. Both nullable, so old rows read NULL.
        alert_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)")}
        for column in ("machine", "detail"):
            if column not in alert_columns:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {column} TEXT")
        # At most one OPEN alert per (kind, serial). A partial unique index lets the
        # conflict be upserted without piling up duplicate rows, while still keeping the
        # history of resolved/dismissed ones for the record.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open_kind_serial "
            "ON alerts(kind, serial_number) WHERE status = 'open'"
        )
        # The equivalent for per-machine alerts (overheat). Separate index so it does not
        # disturb the serial one and so a duplicate_serial row (machine IS NULL) is exempt.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open_kind_machine "
            "ON alerts(kind, machine) WHERE status = 'open' AND machine IS NOT NULL"
        )


def _norm_serial(serial):
    return str(serial).strip() if serial else None


def _decode(row):
    d = dict(row)
    try:
        d["machines"] = json.loads(d["machines"]) if d["machines"] else []
    except (TypeError, ValueError):
        d["machines"] = []
    # `detail` is absent on rows read by a query that predates the column; guard with .get.
    raw_detail = d.get("detail")
    try:
        d["detail"] = json.loads(raw_detail) if raw_detail else None
    except (TypeError, ValueError):
        d["detail"] = None
    return d


def upsert_duplicate(db_path, serial, machines):
    """Raise or refresh the open duplicate_serial alert for `serial`. `machines` is the
    list of colliding hostnames. Returns the alert id."""
    serial = _norm_serial(serial)
    payload = json.dumps(sorted(str(m) for m in machines))
    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM alerts WHERE kind=? AND serial_number=? AND status=?",
            (KIND_DUPLICATE_SERIAL, serial, STATUS_OPEN),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE alerts SET machines=?, updated_at=? WHERE id=?",
                (payload, now, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO alerts(kind, serial_number, machines, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (KIND_DUPLICATE_SERIAL, serial, payload, STATUS_OPEN, now, now),
        )
        return cur.lastrowid


def resolve_for_serial(db_path, serial):
    """Mark any open duplicate_serial alert for `serial` resolved (the collision is gone)."""
    serial = _norm_serial(serial)
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE alerts SET status=?, updated_at=? "
            "WHERE kind=? AND serial_number=? AND status=?",
            (STATUS_RESOLVED, int(time.time()), KIND_DUPLICATE_SERIAL, serial, STATUS_OPEN),
        )


def upsert_overheat(db_path, machine, avg_temp, threshold, window_seconds):
    """Raise or refresh the open overheat alert for `machine`. Returns the alert id.

    Mirrors upsert_duplicate: at most one open row per machine, refreshed while the
    machine stays hot so `updated_at` tracks the latest evaluation and `detail` carries
    the current average. `detail` is the kind-specific payload the Alerts tab renders.
    """
    machine = str(machine).strip()
    detail = json.dumps({
        "avg_temp": round(float(avg_temp), 1),
        "threshold": int(threshold),
        "window_seconds": int(window_seconds),
    })
    now = int(time.time())
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM alerts WHERE kind=? AND machine=? AND status=?",
            (KIND_OVERHEAT, machine, STATUS_OPEN),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE alerts SET detail=?, updated_at=? WHERE id=?",
                (detail, now, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO alerts(kind, machine, detail, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (KIND_OVERHEAT, machine, detail, STATUS_OPEN, now, now),
        )
        return cur.lastrowid


def resolve_overheat(db_path, machine):
    """Mark any open overheat alert for `machine` resolved (it cooled back down)."""
    machine = str(machine).strip()
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE alerts SET status=?, updated_at=? "
            "WHERE kind=? AND machine=? AND status=?",
            (STATUS_RESOLVED, int(time.time()), KIND_OVERHEAT, machine, STATUS_OPEN),
        )


def dismiss(db_path, alert_id):
    """Operator-dismiss one open alert by id. Returns True if an open alert was dismissed."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE alerts SET status=?, updated_at=? WHERE id=? AND status=?",
            (STATUS_DISMISSED, int(time.time()), alert_id, STATUS_OPEN),
        )
        return cur.rowcount > 0


def list_open(db_path):
    """All open alerts, newest activity first. `machines` decoded back to a list."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, kind, serial_number, machines, machine, detail, status, "
            "created_at, updated_at "
            "FROM alerts WHERE status=? ORDER BY updated_at DESC, id DESC",
            (STATUS_OPEN,),
        ).fetchall()
    return [_decode(r) for r in rows]


def count_open(db_path):
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE status=?", (STATUS_OPEN,)
        ).fetchone()["c"]


def get(db_path, alert_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    return _decode(row) if row else None
