"""Alerts store -- operator-facing conditions surfaced from the rest of the hub.

Three kinds are raised today:

* `duplicate_serial` -- the asset-inventory dedup (app.resolve_serial_group) auto-merges
  duplicate machines that share a BIOS serial whenever it can tell which record is stale,
  but it deliberately refuses to merge two machines that are BOTH online and reporting.
  That is a genuine collision only a human should resolve, so it lands here for an
  operator to pick a survivor and merge manually. Keyed on the serial; `machines` holds
  the colliding hostnames.
* `ad_unmatched` -- a machine this hub manages that the configured Active Directory has no
  computer object for (directory.sync_once, roadmap #4). Keyed on the single `machine`.
  Raised only while AD sync is enabled, and auto-resolved the moment the machine turns up
  in a later sync -- see _sync_unmatched_alerts for why resolving matters as much here as
  raising does.
* `rule` -- raised by an operator-written rule's `alert` action (rules.py). Keyed on the
  single `machine` PLUS the rule, because two different rules matching one machine are two
  separate alerts. `detail` carries the rule id, its name, and the rendered text.

* `high_temperature` is a FOURTH kind that is no longer raised. The hub used to evaluate a
  rolling temperature average itself, from two global settings, on a 30 s thread. That is
  now an ordinary rule (`metric.cpu_temp >= N` held for `for_seconds`), which is the same
  condition expressed once instead of twice and can finally be scoped per machine. The
  constant, the readers and the `overheat` rename migration all stay so that alerts an
  operator has not yet dismissed keep rendering; nothing writes the kind any more.

There is at most one OPEN duplicate_serial alert per serial: it is refreshed while the
collision persists and moved to `resolved` once it clears, or `dismissed` if an operator
waves it off.

Rule alerts instead ACCUMULATE, one row per EPISODE. An open alert stays visible after the
condition clears (operators must see that it happened), so a single open row per machine
would mean the next occurrence silently overwrote the previous one. Instead an episode is
the unit: while the rule keeps matching the same row is refreshed (`episode_ended_at` NULL,
`updated_at` tracking the latest evaluation, `detail.count` how many times it was
refreshed); once it clears the episode is ended (`episode_ended_at` set) but the alert stays
open, and the next match opens a NEW row alongside it. The partial unique index still allows
only ONE ACTIVE episode per (machine, rule), so a rule that matches for a week is one alert,
not 20 000. The retired high_temperature kind used exactly this model, which is why the
episode columns and indexes are shared rather than rule-specific.

Kept free of Flask so it can be unit-tested in isolation, exactly like fleet.py; app.py
wires thin HTTP endpoints on top of these functions.
"""
import json
import sqlite3
import time

KIND_DUPLICATE_SERIAL = "duplicate_serial"
KIND_AD_UNMATCHED = "ad_unmatched"
# Raised by an operator-written rule's `alert` action (see rules.py). Unlike the two above,
# this kind's meaning is not fixed by the hub -- the text comes from the rule -- so `detail`
# carries the rule id and name, and `rule_id` is a real column because two DIFFERENT rules
# firing on ONE machine are two separate alerts, not one being overwritten.
KIND_RULE = "rule"
# RETIRED: no code path raises this any more (see the module docstring). Kept because rows
# an operator has not dismissed are still in the table and still have to render.
KIND_HIGH_TEMP = "high_temperature"
# What KIND_HIGH_TEMP was called before the rename. Only init_alerts_db()'s migration reads
# it -- no other code path should ever match on it again.
_LEGACY_KIND_HIGH_TEMP = "overheat"

# Kinds whose subject is ONE machine, named in the `machine` column, rather than the set of
# hostnames in `machines`. The distinction is what tells a reader how to scope an alert
# against an operator's machine filter, and it lives here rather than being re-derived at
# each call site -- a kind missing from a hand-written check at one of those sites is how a
# rule alert ended up both rendered as a duplicate-serial card and returned to operators
# whose scope excluded its machine.
PER_MACHINE_KINDS = frozenset({KIND_RULE, KIND_AD_UNMATCHED, KIND_HIGH_TEMP})

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
        # `machine` is the subject of a per-machine alert (high temperature); `detail` is a JSON
        # payload for kind-specific numbers. Both nullable, so old rows read NULL.
        alert_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)")}
        for column in ("machine", "detail"):
            if column not in alert_columns:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {column} TEXT")
        # `episode_ended_at` (epoch seconds) marks a per-machine alert whose condition has
        # cleared. The alert stays OPEN and visible; ending the episode is what lets the
        # next occurrence accumulate as a new row instead of overwriting this one.
        # NULL on every pre-existing row, which reads as "still the active episode" --
        # correct, as that is exactly what those rows were.
        if "episode_ended_at" not in alert_columns:
            conn.execute("ALTER TABLE alerts ADD COLUMN episode_ended_at INTEGER")
        # The retired temperature alert used to be stored as kind='overheat'. Rename
        # the rows in place so an upgrading hub keeps showing the alerts it already raised.
        # Idempotent: matches nothing on a fresh DB or a second run. A plain UPDATE, not
        # UPDATE OR IGNORE -- a row left behind under the old kind would be one no code
        # path reads any more, i.e. an alert that silently disappeared. The mapping is
        # one-to-one and nothing writes KIND_HIGH_TEMP before this runs, so the partial
        # unique indexes below cannot be violated by it.
        conn.execute("UPDATE alerts SET kind=? WHERE kind=?",
                     (KIND_HIGH_TEMP, _LEGACY_KIND_HIGH_TEMP))
        # At most one OPEN alert per (kind, serial). A partial unique index lets the
        # conflict be upserted without piling up duplicate rows, while still keeping the
        # history of resolved/dismissed ones for the record.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open_kind_serial "
            "ON alerts(kind, serial_number) WHERE status = 'open'"
        )
        # The equivalent for per-machine alerts, scoped to the ACTIVE episode. The older
        # index (one open row per machine, dropped here) is what made a second occurrence
        # overwrite the first; uniqueness on the active episode keeps the same runaway-row
        # protection while letting ended episodes pile up beside it. Separate index so it
        # does not disturb the serial one and so a duplicate_serial row (machine IS NULL)
        # is exempt.
        conn.execute("DROP INDEX IF EXISTS idx_alerts_open_kind_machine")
        # `rule_id` widens the per-machine episode key. A machine can legitimately be inside
        # an active episode of the "uptime > 7 days" rule AND the "disk nearly full" rule at
        # once; keyed on (kind, machine) alone the second would collide with the first and
        # one of the two would silently never be raised. IFNULL(-1) makes the key behave
        # exactly as before for every non-rule kind, whose rule_id is always NULL -- so this
        # is a widening, not a change, for the three kinds that predate it.
        if "rule_id" not in alert_columns:
            conn.execute("ALTER TABLE alerts ADD COLUMN rule_id INTEGER")
        conn.execute("DROP INDEX IF EXISTS idx_alerts_open_kind_machine_active")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open_kind_machine_rule_active "
            "ON alerts(kind, machine, IFNULL(rule_id, -1)) "
            "WHERE status = 'open' AND machine IS NOT NULL AND episode_ended_at IS NULL"
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


def upsert_rule(db_path, machine, rule_id, rule_name, text, now=None):
    """Raise or refresh the ACTIVE episode of rule `rule_id` on `machine`. Returns the id.

    Same episode model as high temperature, and for the same reason: a rule that stays
    matched for a week is one alert, not one per evaluation tick, but the NEXT time it
    matches after clearing it is a new alert beside the old one rather than an overwrite of
    it. `count` in the detail records how many times the episode was refreshed, which is the
    cheapest honest answer to "has this been going on all week or did it just start".
    """
    machine = str(machine).strip()
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, detail FROM alerts "
            "WHERE kind=? AND machine=? AND rule_id=? AND status=? AND episode_ended_at IS NULL",
            (KIND_RULE, machine, rule_id, STATUS_OPEN),
        ).fetchone()
        count = 1
        if row:
            try:
                previous = json.loads(row["detail"]) if row["detail"] else {}
            except (TypeError, ValueError):
                previous = {}
            count = int(previous.get("count") or 1) + 1
        detail = json.dumps({"rule_id": rule_id, "rule_name": str(rule_name or ""),
                             "text": str(text or ""), "count": count})
        if row:
            conn.execute("UPDATE alerts SET detail=?, updated_at=? WHERE id=?",
                         (detail, now, row["id"]))
            return row["id"]
        cur = conn.execute(
            "INSERT INTO alerts(kind, machine, rule_id, detail, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (KIND_RULE, machine, rule_id, detail, STATUS_OPEN, now, now),
        )
        return cur.lastrowid


def end_rule_episode(db_path, machine, rule_id, now=None):
    """The rule stopped matching this machine: close the active episode, leave the alert
    open and visible. The next match raises a fresh alert beside this one."""
    machine = str(machine).strip()
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE alerts SET episode_ended_at=? "
            "WHERE kind=? AND machine=? AND rule_id=? AND status=? AND episode_ended_at IS NULL",
            (int(time.time() if now is None else now), KIND_RULE, machine, rule_id, STATUS_OPEN),
        )
        return cur.rowcount > 0


def resolve_for_rule(db_path, rule_id):
    """Resolve every open alert a rule raised -- called when the rule is deleted or
    disabled, because an alert whose rule no longer exists cannot be explained or acted on."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE alerts SET status=?, updated_at=? WHERE kind=? AND rule_id=? AND status=?",
            (STATUS_RESOLVED, int(time.time()), KIND_RULE, rule_id, STATUS_OPEN),
        )


def raise_ad_unmatched(db_path, machine, now=None):
    """Raise the open `ad_unmatched` alert for `machine` if it has none. Returns the id.

    Deliberately does NOT refresh an existing row's timestamp on every pass. An hourly
    sync would otherwise re-stamp `updated_at` sixty times a day and float a months-old
    problem to the top of a newest-first list every hour, which is how an alert tab stops
    being read. `created_at` is when AD first stopped knowing about this machine, and that
    is the number an operator actually wants.

    A dismissed alert stays dismissed for the same reason: this fires on every sync, so
    re-raising what an operator has waved off would make dismissal useless. It comes back
    only if the machine reappears in AD (resolving the row) and then goes missing again.
    """
    machine = str(machine).strip()
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM alerts WHERE kind=? AND machine=? AND status<>?",
            (KIND_AD_UNMATCHED, machine, STATUS_RESOLVED),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO alerts(kind, machine, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (KIND_AD_UNMATCHED, machine, STATUS_OPEN, now, now),
        )
        return cur.lastrowid


def resolve_ad_unmatched(db_path, machine, now=None):
    """The machine has a computer object again (or the check was turned off): resolve it.

    Resolves DISMISSED rows too, not just open ones -- otherwise a dismissed alert would
    sit in the table forever blocking raise_ad_unmatched's "already exists" check, and the
    machine could never raise a fresh alert if it fell out of AD a second time.
    """
    machine = str(machine).strip()
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE alerts SET status=?, updated_at=? WHERE kind=? AND machine=? AND status<>?",
            (STATUS_RESOLVED, int(time.time() if now is None else now),
             KIND_AD_UNMATCHED, machine, STATUS_RESOLVED),
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
            "episode_ended_at, created_at, updated_at "
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
