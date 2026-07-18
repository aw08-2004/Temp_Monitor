"""Alerts store -- operator-facing conflicts surfaced from the rest of the hub.

Today it holds exactly one kind: `duplicate_serial`. The asset-inventory dedup
(app.resolve_serial_group) auto-merges duplicate machines that share a BIOS serial
whenever it can tell which record is stale, but it deliberately refuses to merge two
machines that are BOTH online and reporting -- that is a genuine collision only a human
should resolve. Those land here so an operator can pick a survivor and merge manually
from the Alerts tab.

There is at most one OPEN alert per (kind, serial): it is refreshed while the conflict
persists and moved to `resolved` once the collision is gone (one went offline and got
auto-merged, or the operator merged them), or to `dismissed` if an operator waves it off.

Kept free of Flask so it can be unit-tested in isolation, exactly like fleet.py; app.py
wires thin HTTP endpoints on top of these functions.
"""
import json
import sqlite3
import time

KIND_DUPLICATE_SERIAL = "duplicate_serial"

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
        # At most one OPEN alert per (kind, serial). A partial unique index lets the
        # conflict be upserted without piling up duplicate rows, while still keeping the
        # history of resolved/dismissed ones for the record.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open_kind_serial "
            "ON alerts(kind, serial_number) WHERE status = 'open'"
        )


def _norm_serial(serial):
    return str(serial).strip() if serial else None


def _decode(row):
    d = dict(row)
    try:
        d["machines"] = json.loads(d["machines"]) if d["machines"] else []
    except (TypeError, ValueError):
        d["machines"] = []
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
            "SELECT id, kind, serial_number, machines, status, created_at, updated_at "
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
