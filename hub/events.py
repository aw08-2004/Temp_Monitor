"""Windows Event Log mining -- what the fleet's logs say, without the fleet's logs
(roadmap #16).

The helpdesk question this answers is the PRD's own example: *"is somebody brute-forcing
that account?"* -- 4625 on the Security channel, across every machine, in one list. Today
the only way to ask it is to open Event Viewer on one PC at a time.

**The design problem is volume, and it is the whole of this module.** A busy Windows
workstation writes thousands of event records a day; a domain controller writes that in an
hour. This hub is a single SQLite file that also holds every temperature reading in the
fleet, so shipping raw logs into it is not a thing that can be made to work by tuning a
retention window afterwards. Three decisions follow from that, and each of them is a
*refusal* to do the obvious thing:

  * **Filtering happens on the machine.** The hub publishes a subscription set -- channels,
    event ids, levels -- and the agent hands only matches to its heartbeat. Nothing else is
    read, and nothing else crosses the wire. A fleet with no subscriptions costs nothing at
    all, which is the state every hub upgrades into.
  * **Repeats are rolled up rather than stored.** A password-spray against one account is
    five hundred identical 4625 records, and five hundred rows says nothing five hundred
    *on one row* does not -- see `ROLLUP_WINDOW_SECONDS` and `record_events`. This is the
    one decision here that loses information, and the docstring on that constant says
    exactly what is lost and why it was judged worth it.
  * **Retention is decided before the first row is written**, not after somebody notices
    the file is 4 GB. `data.event_retention_days` exists from the same commit as the table,
    defaults short, and the retention thread prunes on it -- plus a per-machine row cap, so
    one misconfigured subscription on one PC cannot fill the disk between two prunes.

**The subscription set is fleet-wide and versioned as one document.** Not per machine, and
not per device group -- see the roadmap entry. The agent sends back the version it holds and
the hub replies with the document only when that differs, which is `settings.agent_config`'s
shape and `policy.resolve_for`'s shape, for the same reason: this rides a ten-second
heartbeat, and re-sending an unchanged document would be most of it.

**An EMPTY document is still sent**, exactly as the device policy's is, and for exactly the
same reason: deleting the last subscription has to be able to *stop* collection. An absent
block reads to the agent as "the hub had nothing to say", and it would go on reading the
Security channel forever.

**A rule can now be written against an event**, which was the open parameter the roadmap
entry named and the reason #16 came before #17. It landed as its own change rather than
alongside collection, because growing both in one commit would have made the collection half
impossible to review. The namespace and the staleness rule live in `rules.py`; what this
file contributes is `subscribed_event_ids` and `rule_counters` at the bottom, which are
`summary`'s counters narrowed to one machine. **A rule can only count what a subscription
asked for** -- see `subscribed_event_ids` for why that bound is the honest one.

Kept free of Flask so it can be unit-tested in isolation, exactly like fleet.py and
processes.py; events_web.py wires thin HTTP endpoints on top.
"""
import hashlib
import json
import sqlite3
import time
import uuid

# ================================
# VOCABULARY
# ================================
# Windows' own severity levels, as slugs rather than the numbers the API uses. The numbers
# are what `EventRecord.Level` returns (1-5) and what a subscription is matched on inside
# the agent; the slugs are what the hub stores, filters on and captions the console with,
# because `level: 2` in a JSON payload is unreadable and `level: "error"` is not.
#
# Level 0 ("LogAlways") is deliberately absent. It is what a provider emits when it declines
# to classify a record, it is rare, and offering it as a filter choice would suggest it means
# something. The agent maps it to `information`.
LEVEL_CRITICAL = "critical"
LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LEVEL_INFORMATION = "information"
LEVEL_VERBOSE = "verbose"
LEVELS = (LEVEL_CRITICAL, LEVEL_ERROR, LEVEL_WARNING, LEVEL_INFORMATION, LEVEL_VERBOSE)

#: The label each level is shown with lives in the translation catalogs, under
#: `events.level.<slug>` -- the same discipline permissions.CAPABILITY_TEXT_KEY keeps, so a
#: level added here without catalog entries fails tests/test_i18n.py rather than captioning a
#: filter with its own slug.
LEVEL_TEXT_KEY = "events.level"

#: The four channels every Windows machine has, offered as the defaults a subscription form
#: opens on. NOT an allow-list: `log` is stored as free text so a provider channel
#: ("Microsoft-Windows-TaskScheduler/Operational", "Microsoft-Windows-Windows Defender/
#: Operational") can be subscribed to as well, which is where most of the genuinely useful
#: events in a modern Windows live. Constraining it to these four would have made the feature
#: answer only the questions Windows 2003 could answer.
COMMON_LOGS = ("Application", "Security", "System", "Setup")

# ================================
# CAPS
# ================================
#: Most events one heartbeat may carry. The agent caps at the same number and reports what it
#: dropped, so a machine mid-storm sends a bounded payload every ten seconds and says so,
#: rather than either growing one heartbeat without limit or silently losing the storm --
#: which is the one occasion anybody will ever read this page.
MAX_EVENTS_PER_REPORT = 200

#: Rows one machine may hold, enforced on ingest. The retention window is the ordinary bound;
#: this is the one that holds when somebody subscribes to Information on the Security channel
#: of a domain controller and goes to lunch. Oldest rows go first.
MAX_EVENTS_PER_MACHINE = 5000

#: Field caps. The message is the only large one -- a rendered Windows event message runs to
#: a few hundred characters for the useful ones and to several kilobytes for the worst of
#: them, and the tail of those is boilerplate.
MAX_MESSAGE_CHARS = 2000
MAX_LOG_CHARS = 160
MAX_PROVIDER_CHARS = 160
MAX_NAME_CHARS = 120

#: Subscriptions one hub may hold. Not a resource limit -- it is a legibility one. The
#: document goes to every agent in the fleet on a heartbeat, and a hundred subscriptions is
#: not a filter, it is a copy of the event log.
MAX_SUBSCRIPTIONS = 40

#: Event ids one subscription may name, and the id range Windows itself allows.
MAX_EVENT_IDS = 60
MIN_EVENT_ID = 0
MAX_EVENT_ID = 65535

#: How far apart two otherwise identical records must be before the second one gets a row of
#: its own instead of incrementing the first one's `count`.
#:
#: **This is the one place in this module where information is deliberately lost**, so it is
#: worth being exact about what: within a five-minute window, the individual arrival times of
#: repeats of the same event are not kept -- only the first, the last, and how many. What is
#: NOT lost is the occurrence itself, the count, or the window it spans, which is what every
#: question anybody asks of a repeated event actually needs ("how many failed logons, from
#: when to when").
#:
#: The rejected alternative was storing every record and aggregating at read time. It is
#: strictly more faithful and it was turned down on arithmetic: one account being sprayed
#: writes a 4625 every few hundred milliseconds, so a single machine having a bad afternoon
#: would put six figures of rows into a SQLite file that is also serving the console's live
#: charts -- and produce a page nobody can read, because the useful answer is a count.
ROLLUP_WINDOW_SECONDS = 300

#: The counting window `rule_counters` uses when its caller passes none. **It must equal the
#: default of the `events.summary_window_seconds` setting**, which is the knob the console's
#: event summary and the rules engine both read: a rule has to fire on the number the
#: operator was looking at when they picked the threshold. Two plausible-but-different
#: numbers is not a visible failure, so tests/test_event_rules.py asserts they agree.
DEFAULT_RULE_WINDOW_SECONDS = 86400

#: A machine that has not reported for longer than this has a stale event view rather than a
#: quiet one. The console says so instead of rendering "nothing since Tuesday" as good news,
#: which is the failure mode of every log console: a collector that stopped and a fleet that
#: is behaving look identical.
STALE_AFTER_SECONDS = 3600


class SubscriptionRejected(ValueError):
    """A subscription the hub will not store, with the reason in the message.

    Its own class rather than a bare ValueError so events_web can answer it as a 400 without
    catching every ValueError a sqlite driver might raise underneath it -- same shape as
    wake.WakeRejected and firmware.PayloadRejected.
    """


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_events_db(db_path):
    """Create the event tables if absent. Idempotent -- safe to call next to the other
    init_*_db() functions on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        # WHAT TO COLLECT. Fleet-wide: no machine column, on purpose (see the module
        # docstring). `event_ids` and `levels` are JSON arrays rather than child tables
        # because nothing ever queries across them -- this document is written whole, read
        # whole, hashed whole and shipped whole.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_subscriptions (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                log         TEXT NOT NULL,   -- channel: Security, System, a provider channel
                event_ids   TEXT NOT NULL,   -- JSON array of ints; [] means "any id"
                levels      TEXT NOT NULL,   -- JSON array of LEVELS; [] means "any level"
                provider    TEXT,            -- optional provider-name substring, case-folded
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  INTEGER NOT NULL,
                created_by  TEXT,
                updated_at  INTEGER NOT NULL
            )
            """
        )

        # WHAT CAME BACK. Append-only, rolled up (see ROLLUP_WINDOW_SECONDS), pruned by the
        # retention thread and capped per machine on ingest.
        #
        # `rollup_key` is the identity two records must share to be counted as the same
        # thing; it is indexed together with the machine because that pair is the only
        # lookup ingest makes, and ingest runs on a heartbeat from every PC in the fleet.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_events (
                id           TEXT PRIMARY KEY,
                machine      TEXT NOT NULL,
                log          TEXT NOT NULL,
                provider     TEXT NOT NULL,
                event_id     INTEGER NOT NULL,
                level        TEXT NOT NULL,
                message      TEXT NOT NULL,
                rollup_key   TEXT NOT NULL,
                count        INTEGER NOT NULL DEFAULT 1,
                first_seen   INTEGER NOT NULL,   -- the event's own time, not arrival
                last_seen    INTEGER NOT NULL,
                recorded_at  INTEGER NOT NULL    -- when the hub stored it
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_events_rollup "
                     "ON machine_events(machine, rollup_key, last_seen)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_events_recent "
                     "ON machine_events(last_seen)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_events_machine "
                     "ON machine_events(machine, last_seen)")

        # WHEN EACH MACHINE LAST REPORTED, and what it dropped doing so. One row per machine.
        #
        # Separate from machine_events rather than derived from it, because the two answer
        # different questions and the difference is the whole point: a machine with no rows
        # may be a machine with nothing to report (good) or a machine whose agent is too old
        # to know what a subscription is (not good, and invisible without this table).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_event_state (
                machine       TEXT PRIMARY KEY,
                reported_at   INTEGER NOT NULL,
                dropped       INTEGER NOT NULL DEFAULT 0,  -- cumulative, over the cap
                error         TEXT
            )
            """
        )


# ================================
# SUBSCRIPTIONS
# ================================
def _clean_text(value, cap, field):
    text = str(value or "").strip()
    if not text:
        raise SubscriptionRejected(f"{field} is required.")
    return text[:cap]


def _clean_event_ids(value):
    """A list of Windows event ids, deduplicated and ordered. `[]` means "any id".

    "Any id" is a real subscription rather than an oversight -- "every Critical on System"
    is exactly how somebody starts -- so an empty list is accepted and a *missing* one means
    the same thing. What is refused is a non-integer or an out-of-range id, because Windows
    would never emit one and accepting it would build a filter that silently matches nothing.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SubscriptionRejected("Event ids must be a list.")
    ids = []
    for raw in value:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise SubscriptionRejected(f"{raw!r} is not an event id.")
        if number < MIN_EVENT_ID or number > MAX_EVENT_ID:
            raise SubscriptionRejected(
                f"Event id {number} is outside {MIN_EVENT_ID}-{MAX_EVENT_ID}.")
        if number not in ids:
            ids.append(number)
    if len(ids) > MAX_EVENT_IDS:
        raise SubscriptionRejected(
            f"A subscription may name at most {MAX_EVENT_IDS} event ids.")
    return sorted(ids)


def _clean_levels(value):
    """A list of LEVELS slugs. `[]` means "any level", for the same reason `[]` ids does."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SubscriptionRejected("Levels must be a list.")
    levels = []
    for raw in value:
        slug = str(raw or "").strip().lower()
        if slug not in LEVELS:
            raise SubscriptionRejected(f"{raw!r} is not an event level.")
        if slug not in levels:
            levels.append(slug)
    # Ordered by severity rather than by the order they were typed, so two subscriptions
    # naming the same levels hash identically -- see document_version.
    return [level for level in LEVELS if level in levels]


def _row_to_subscription(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "log": row["log"],
        "event_ids": json.loads(row["event_ids"]),
        "levels": json.loads(row["levels"]),
        "provider": row["provider"] or "",
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
    }


def list_subscriptions(db_path):
    """Every subscription, enabled or not, newest first. The console's list."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM event_subscriptions ORDER BY created_at DESC, name ASC"
        ).fetchall()
    return [_row_to_subscription(row) for row in rows]


def get_subscription(db_path, subscription_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM event_subscriptions WHERE id = ?",
                           (str(subscription_id),)).fetchone()
    return _row_to_subscription(row) if row else None


def create_subscription(db_path, *, name, log, event_ids=None, levels=None,
                        provider="", enabled=True, created_by=None):
    """Store a subscription and return it. Raises SubscriptionRejected on anything invalid.

    Validation happens here rather than in the web layer so that it is the same validation
    however the row arrives -- the console, a future API token, a seeded default.
    """
    name = _clean_text(name, MAX_NAME_CHARS, "A name")
    log = _clean_text(log, MAX_LOG_CHARS, "A log channel")
    ids = _clean_event_ids(event_ids)
    slugs = _clean_levels(levels)
    provider = str(provider or "").strip()[:MAX_PROVIDER_CHARS]
    now = int(time.time())

    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT COUNT(*) FROM event_subscriptions").fetchone()[0]
        if existing >= MAX_SUBSCRIPTIONS:
            raise SubscriptionRejected(
                f"This hub already holds {MAX_SUBSCRIPTIONS} subscriptions, which is the "
                "maximum. Delete one before adding another.")
        subscription_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO event_subscriptions
                (id, name, log, event_ids, levels, provider, enabled,
                 created_at, created_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (subscription_id, name, log, json.dumps(ids), json.dumps(slugs), provider,
             1 if enabled else 0, now, created_by, now),
        )
    return get_subscription(db_path, subscription_id)


def update_subscription(db_path, subscription_id, **fields):
    """Change one subscription. Only the fields present are touched.

    `enabled` is the field this exists for: switching a noisy subscription off has to be one
    click, because the moment anybody needs it is the moment the fleet is filling the table.
    """
    current = get_subscription(db_path, subscription_id)
    if not current:
        raise SubscriptionRejected("That subscription no longer exists.")

    name = _clean_text(fields.get("name", current["name"]), MAX_NAME_CHARS, "A name")
    log = _clean_text(fields.get("log", current["log"]), MAX_LOG_CHARS, "A log channel")
    ids = _clean_event_ids(fields["event_ids"]) if "event_ids" in fields \
        else current["event_ids"]
    slugs = _clean_levels(fields["levels"]) if "levels" in fields else current["levels"]
    provider = str(fields.get("provider", current["provider"]) or "").strip()
    enabled = bool(fields.get("enabled", current["enabled"]))

    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE event_subscriptions
               SET name = ?, log = ?, event_ids = ?, levels = ?, provider = ?,
                   enabled = ?, updated_at = ?
             WHERE id = ?
            """,
            (name, log, json.dumps(ids), json.dumps(slugs), provider[:MAX_PROVIDER_CHARS],
             1 if enabled else 0, int(time.time()), str(subscription_id)),
        )
    return get_subscription(db_path, subscription_id)


def delete_subscription(db_path, subscription_id):
    """Remove a subscription. Collected events are deliberately NOT removed with it.

    They are the record of something that happened on a machine; the subscription is only
    the reason we heard about it. Deleting the evidence along with the filter is how somebody
    loses the answer to "what was that burst last Tuesday" by tidying up a form.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM event_subscriptions WHERE id = ?",
                           (str(subscription_id),))
        return cur.rowcount > 0


def document(db_path):
    """The subscription set as the agent receives it, plus its version.

    **Only enabled subscriptions travel**, and `subscriptions: []` is a real document rather
    than an absent one -- see the module docstring. Disabling the last subscription must
    reach the agent as an instruction to stop, not as silence.

    Fields are trimmed to what the agent actually matches on: the console's name, timestamps
    and author have no business changing a version hash that decides whether every machine in
    the fleet re-reads its configuration.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM event_subscriptions WHERE enabled = 1 ORDER BY id ASC"
        ).fetchall()
    subscriptions = [
        {
            "id": row["id"],
            "log": row["log"],
            "event_ids": json.loads(row["event_ids"]),
            "levels": json.loads(row["levels"]),
            "provider": row["provider"] or "",
        }
        for row in rows
    ]
    return {"version": document_version(subscriptions),
            "subscriptions": subscriptions,
            "max_events": MAX_EVENTS_PER_REPORT}


def document_version(subscriptions):
    """Short content hash of the subscription set.

    Content-derived rather than a counter, for the reason settings.agent_config_version
    gives: it survives a hub restart and a DB restore with nothing to keep in sync, and a
    change that is made and then reverted hashes back to where it started, so agents that
    never saw the intermediate state do not re-apply anything. A counter would tick twice and
    churn the whole fleet for a subscription somebody added and removed.
    """
    blob = json.dumps(subscriptions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ================================
# INGEST
# ================================
def _clean_level(value):
    """A reported level, defaulting to `information` rather than refusing.

    An event whose level we cannot read is still an event that happened, and dropping it
    would make the console quietly disagree with Event Viewer. The default is the least
    alarming of the five on purpose: guessing `critical` from a parse failure would put a red
    row on the page for a bug in this function.
    """
    slug = str(value or "").strip().lower()
    return slug if slug in LEVELS else LEVEL_INFORMATION


def rollup_key(log, provider, event_id, level, message):
    """The identity two records must share to count as the same thing.

    The message is *hashed into* the key rather than compared loosely, and that is the
    conservative direction: two 4625s naming different accounts have different messages, so
    they stay separate rows and the page still answers "which account". Only genuinely
    identical repeats collapse.
    """
    blob = "\x1f".join([
        str(log or "").casefold(),
        str(provider or "").casefold(),
        str(event_id),
        str(level),
        str(message or ""),
    ])
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:32]


def _normalize(entry, now):
    """One reported record, as a dict this module can store, or None to skip it.

    Skipping rather than raising: one malformed entry in a batch of two hundred must not cost
    the other hundred and ninety-nine. The agent is the same code everywhere, so a shape
    problem here means a version skew, and losing the whole heartbeat over it would take the
    machine's liveness with it.
    """
    if not isinstance(entry, dict):
        return None
    try:
        event_id = int(entry.get("event_id"))
    except (TypeError, ValueError):
        return None
    if event_id < MIN_EVENT_ID or event_id > MAX_EVENT_ID:
        return None

    log = str(entry.get("log") or "").strip()[:MAX_LOG_CHARS]
    if not log:
        return None
    provider = str(entry.get("provider") or "").strip()[:MAX_PROVIDER_CHARS]
    level = _clean_level(entry.get("level"))
    message = str(entry.get("message") or "").strip()[:MAX_MESSAGE_CHARS]

    # The event's OWN time, not the moment it reached us. A machine that was offline for a
    # day reports yesterday's events today, and stamping them with arrival would put a
    # day-old logon failure at the top of the page as though it had just happened.
    #
    # Bounded in both directions, because an unsynchronised clock is common on exactly the
    # machines this feature is pointed at: a timestamp in the future, or older than the
    # retention window could possibly keep, falls back to arrival rather than being stored as
    # read. Out-of-range is silently corrected rather than refused -- the event is real
    # whatever the clock said.
    try:
        occurred = int(entry.get("occurred_at"))
    except (TypeError, ValueError):
        occurred = now
    if occurred > now + 300 or occurred < now - 3650 * 86400:
        occurred = now

    return {
        "log": log,
        "provider": provider,
        "event_id": event_id,
        "level": level,
        "message": message,
        "occurred_at": occurred,
        "rollup_key": rollup_key(log, provider, event_id, level, message),
    }


def record_events(db_path, machine, payload):
    """Store what one machine's heartbeat carried. Returns (stored, rolled_up).

    `payload` is the agent's object -- `{"events": [...], "dropped": N, "error": "..."}` --
    and it is an OBJECT rather than a bare list for the reason patches' is: the useful report
    from a quiet machine is an EMPTY list, and every truthiness check along the path would
    discard exactly that one. A machine reporting nothing is how the console knows collection
    is alive, which is the difference between "your fleet is fine" and "your collector died"
    -- the failure every log console gets wrong.

    Never raises on content: see `_normalize`. It raises only on a database that is not
    there, which is a hub problem rather than a report problem.
    """
    if not isinstance(payload, dict):
        payload = {"events": payload if isinstance(payload, list) else []}
    entries = payload.get("events")
    entries = entries if isinstance(entries, list) else []
    try:
        dropped = max(0, int(payload.get("dropped") or 0))
    except (TypeError, ValueError):
        dropped = 0
    error = str(payload.get("error") or "").strip()[:500] or None

    now = int(time.time())
    # The agent caps too, and the hub caps again rather than trusting it. Nothing here is
    # authenticated beyond the bearer token, and "a machine may not put more than this on one
    # heartbeat" is a hub invariant, not an agent courtesy.
    if len(entries) > MAX_EVENTS_PER_REPORT:
        dropped += len(entries) - MAX_EVENTS_PER_REPORT
        entries = entries[:MAX_EVENTS_PER_REPORT]

    stored = 0
    rolled = 0
    with get_conn(db_path) as conn:
        for entry in entries:
            record = _normalize(entry, now)
            if record is None:
                continue
            # Proximity is tested in BOTH directions, not just backwards. The obvious
            # version -- "is there a row whose last occurrence is within the window before
            # this one" -- is right for records arriving in order and wrong for the case
            # this feature has to survive: a machine back from a day offline reports a day
            # of events, and a record from yesterday morning would otherwise be rolled into
            # a row from this afternoon and drag its `first_seen` back twenty hours. The
            # count would still be right and the window would be a lie.
            window = ROLLUP_WINDOW_SECONDS
            existing = conn.execute(
                """
                SELECT id, count, first_seen, last_seen FROM machine_events
                 WHERE machine = ? AND rollup_key = ?
                   AND last_seen >= ? AND first_seen <= ?
                 ORDER BY last_seen DESC LIMIT 1
                """,
                (machine, record["rollup_key"],
                 record["occurred_at"] - window, record["occurred_at"] + window),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE machine_events
                       SET count = count + 1,
                           first_seen = MIN(first_seen, ?),
                           last_seen = MAX(last_seen, ?)
                     WHERE id = ?
                    """,
                    (record["occurred_at"], record["occurred_at"], existing["id"]),
                )
                rolled += 1
                continue
            conn.execute(
                """
                INSERT INTO machine_events
                    (id, machine, log, provider, event_id, level, message,
                     rollup_key, count, first_seen, last_seen, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (uuid.uuid4().hex, machine, record["log"], record["provider"],
                 record["event_id"], record["level"], record["message"],
                 record["rollup_key"], record["occurred_at"], record["occurred_at"], now),
            )
            stored += 1

        # The state row is written on EVERY report, including the empty ones. That is the
        # whole reason it exists -- see init_events_db.
        conn.execute(
            """
            INSERT INTO machine_event_state (machine, reported_at, dropped, error)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(machine) DO UPDATE SET
                reported_at = excluded.reported_at,
                dropped = machine_event_state.dropped + excluded.dropped,
                error = excluded.error
            """,
            (machine, now, dropped, error),
        )

    if stored:
        enforce_machine_cap(db_path, machine)
    return stored, rolled


def enforce_machine_cap(db_path, machine, cap=MAX_EVENTS_PER_MACHINE):
    """Drop this machine's oldest rows past the cap. Returns how many went.

    Runs on ingest rather than only on the retention tick, because the retention tick is a
    daily thing by default and the case this guards against fills a disk in an afternoon.
    Ordered by `last_seen` and not by `recorded_at`: what an operator loses should be the
    oldest EVENTS, not the ones that happened to be backfilled last.
    """
    with get_conn(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM machine_events WHERE machine = ?",
                             (machine,)).fetchone()[0]
        if total <= cap:
            return 0
        cur = conn.execute(
            """
            DELETE FROM machine_events
             WHERE id IN (SELECT id FROM machine_events WHERE machine = ?
                           ORDER BY last_seen ASC LIMIT ?)
            """,
            (machine, total - cap),
        )
        return cur.rowcount


def prune(db_path, cutoff_epoch):
    """Delete events whose last occurrence is older than `cutoff_epoch`. Returns the count.

    Compared on `last_seen` rather than `first_seen`, so a rolled-up row lives from its last
    repeat rather than its first -- a burst that started five weeks ago and is still going is
    not stale.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM machine_events WHERE last_seen < ?",
                           (int(cutoff_epoch),))
        return cur.rowcount


# ================================
# READS
# ================================
def _row_to_event(row):
    return {
        "id": row["id"],
        "machine": row["machine"],
        "log": row["log"],
        "provider": row["provider"],
        "event_id": row["event_id"],
        "level": row["level"],
        "message": row["message"],
        "count": row["count"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    }


def list_events(db_path, *, machines=None, machine=None, levels=None, log=None,
                event_id=None, search=None, since=None, limit=200):
    """Collected events, newest last-occurrence first.

    `machines` is an allow-list of names -- the caller's scope, applied in SQL rather than
    after the fact so a scoped operator's page is not built by fetching the fleet's rows and
    throwing most of them away. `machines=None` means unfiltered, and `machines=[]` means
    "this operator can see nothing", which must return nothing rather than everything: an
    empty allow-list read as "no filter" is the classic way a scope check inverts itself.
    """
    clauses = []
    params = []
    if machines is not None:
        names = [str(name) for name in machines]
        if not names:
            return []
        clauses.append("machine IN (%s)" % ",".join("?" * len(names)))
        params.extend(names)
    if machine:
        clauses.append("machine = ?")
        params.append(str(machine))
    if levels:
        slugs = [slug for slug in (str(s).strip().lower() for s in levels) if slug in LEVELS]
        if not slugs:
            return []
        clauses.append("level IN (%s)" % ",".join("?" * len(slugs)))
        params.extend(slugs)
    if log:
        clauses.append("log = ?")
        params.append(str(log))
    if event_id is not None:
        try:
            params.append(int(event_id))
            clauses.append("event_id = ?")
        except (TypeError, ValueError):
            return []
    if since is not None:
        clauses.append("last_seen >= ?")
        params.append(int(since))
    if search:
        # Matched against the message and the provider, which is where an operator's words
        # ("defender", an account name out of a 4625) actually live. LIKE with escaped
        # wildcards rather than a bare substring: a search for "100%" must not match
        # everything.
        needle = str(search).strip().lower()
        needle = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(LOWER(message) LIKE ? ESCAPE '\\' "
                       "OR LOWER(provider) LIKE ? ESCAPE '\\')")
        params.extend([f"%{needle}%", f"%{needle}%"])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        capped = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        capped = 200
    params.append(capped)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM machine_events{where} ORDER BY last_seen DESC, id ASC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def known_machines(db_path):
    """Every machine this module has heard from -- the set a scoped read is narrowed against.

    Drawn from the state table rather than from `machine_events`, so a machine that reported
    and had nothing to say is still in scope. Narrowing to machines that produced rows would
    make "nothing from that PC" and "that PC is not yours" the same answer.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT machine FROM machine_event_state").fetchall()
    return [row["machine"] for row in rows]


def machine_state(db_path, machine):
    """When this machine last reported events, and what it could not carry.

    `reported_at: None` is every machine on an agent too old to know what a subscription is,
    and it must stay distinct from a machine that reported having nothing -- the first means
    "we have not been told" and the second is the normal, healthy answer.
    """
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM machine_event_state WHERE machine = ?",
                           (machine,)).fetchone()
    if not row:
        return {"machine": machine, "reported_at": None, "dropped": 0, "error": None,
                "stale": False}
    return {
        "machine": machine,
        "reported_at": row["reported_at"],
        "dropped": row["dropped"],
        "error": row["error"],
        "stale": (int(time.time()) - row["reported_at"]) > STALE_AFTER_SECONDS,
    }


def summary(db_path, machines=None, window_seconds=86400):
    """Counts over the last `window_seconds`: by level, by event id, and per machine.

    **This is the shape a rule condition will read** when #17 grows one -- "more than N of
    event 4625 on one machine within a window" is this query with a threshold on it. Written
    now, as the console's summary, so that the correlation work inherits a counter that has
    been exercised rather than inventing one.

    `occurrences` is the sum of `count`, not the number of rows: rolled-up repeats are the
    thing being counted, and a page saying "3 failed logons" over three rows holding four
    hundred between them would be worse than no number.
    """
    since = int(time.time()) - int(window_seconds)
    clauses = ["last_seen >= ?"]
    params = [since]
    if machines is not None:
        names = [str(name) for name in machines]
        if not names:
            return {"window_seconds": int(window_seconds), "occurrences": 0, "rows": 0,
                    "by_level": {}, "top_events": [], "machines": 0}
        clauses.append("machine IN (%s)" % ",".join("?" * len(names)))
        params.extend(names)
    where = " WHERE " + " AND ".join(clauses)

    with get_conn(db_path) as conn:
        totals = conn.execute(
            f"SELECT COALESCE(SUM(count), 0) AS occurrences, COUNT(*) AS rows_, "
            f"COUNT(DISTINCT machine) AS machines FROM machine_events{where}", params
        ).fetchone()
        by_level = conn.execute(
            f"SELECT level, COALESCE(SUM(count), 0) AS n FROM machine_events{where} "
            f"GROUP BY level", params
        ).fetchall()
        top = conn.execute(
            f"SELECT log, event_id, COALESCE(SUM(count), 0) AS n, "
            f"COUNT(DISTINCT machine) AS machines FROM machine_events{where} "
            f"GROUP BY log, event_id ORDER BY n DESC LIMIT 10", params
        ).fetchall()

    return {
        "window_seconds": int(window_seconds),
        "occurrences": totals["occurrences"],
        "rows": totals["rows_"],
        "machines": totals["machines"],
        "by_level": {row["level"]: row["n"] for row in by_level},
        "top_events": [{"log": row["log"], "event_id": row["event_id"],
                        "occurrences": row["n"], "machines": row["machines"]}
                       for row in top],
    }


# ================================
# WHAT A RULE READS
# ================================
# The counters behind rules.py's `event.*` namespace (roadmap #17). The split is deliberate:
# the rules engine owns the namespace, the staleness rule and what a missing answer means,
# because those are properties of a rule rather than of an event. This file owns what the
# numbers themselves mean, which belongs next to `record_events` and `ROLLUP_WINDOW_SECONDS`
# -- counting a rolled-up row as one occurrence would be a bug about the roll-up, not a bug
# about rules, and it would be found here.


def subscribed_event_ids(db_path):
    """Every event id an ENABLED subscription names, sorted, deduplicated.

    **The rules namespace is built from what the hub asked for, not from what arrived**, and
    the difference is the whole reason this function exists rather than a `SELECT DISTINCT
    event_id FROM machine_events`.

    An operator sits down to write "alert me on twenty failed logons" on a quiet morning. If
    the variable came from collected rows, `event.id_4625.count` would not exist yet -- it
    would appear only once the brute-force was already under way, which is to say the
    variable would be missing at exactly the moment it is needed and present only when it is
    too late. Subscribing is also the only way such a row can ever exist, so the subscription
    set is both the honest bound and the one an operator can see and change.

    A subscription naming no ids (`[]`, "any id on this channel") contributes nothing here:
    "any" is not a number a counter can be keyed by. Its records still count toward the
    level totals, which is where a channel-wide subscription belongs.
    """
    out = set()
    for subscription in list_subscriptions(db_path):
        if not subscription.get("enabled"):
            continue
        for event_id in subscription.get("event_ids") or []:
            out.add(int(event_id))
    return tuple(sorted(out))


def rule_counters(db_path, machine, *, event_ids=(),
                  window_seconds=DEFAULT_RULE_WINDOW_SECONDS, now=None):
    """One machine's windowed occurrence counts, for the rules engine.

    Returns `{"reported_at", "total", "by_level", "by_event_id"}`. `reported_at` is None for
    a machine this module has never been told about, and **that is not the same as zero**:
    it is an agent too old to know what a subscription is, or one that has never completed a
    heartbeat. The caller turns it into UNKNOWN. Collapsing the two would let
    `event.error_count == 0` report every un-upgraded machine in the fleet as healthy, which
    is the silent failure this whole distinction exists to prevent -- `machine_state` above
    makes the same one for the console.

    `occurrences`, not rows, exactly as `summary` counts: a rolled-up run of four hundred
    4625s is four hundred, not one. A run whose `last_seen` falls inside the window counts in
    full even when it started before it. Prorating across the roll-up was rejected because
    the arrival times inside a roll-up are precisely what `ROLLUP_WINDOW_SECONDS` threw away
    -- a share computed from `first_seen`/`last_seen` would be an invented distribution
    presented to an operator as a measurement.

    An id named by two subscriptions is one entry counted once; the subscriptions overlap,
    the event did not happen twice.
    """
    now = int(now if now is not None else time.time())
    since = now - int(window_seconds)
    wanted = sorted({int(event_id) for event_id in event_ids or ()})

    with get_conn(db_path) as conn:
        state = conn.execute("SELECT reported_at FROM machine_event_state WHERE machine = ?",
                             (machine,)).fetchone()
        totals = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS n FROM machine_events "
            "WHERE machine = ? AND last_seen >= ?", (machine, since)).fetchone()
        by_level = conn.execute(
            "SELECT level, COALESCE(SUM(count), 0) AS n FROM machine_events "
            "WHERE machine = ? AND last_seen >= ? GROUP BY level",
            (machine, since)).fetchall()
        by_event_id = {}
        if wanted:
            placeholders = ",".join("?" * len(wanted))
            rows = conn.execute(
                f"SELECT event_id, COALESCE(SUM(count), 0) AS n FROM machine_events "
                f"WHERE machine = ? AND last_seen >= ? AND event_id IN ({placeholders}) "
                f"GROUP BY event_id", [machine, since] + wanted).fetchall()
            by_event_id = {int(row["event_id"]): int(row["n"]) for row in rows}

    return {
        "reported_at": state["reported_at"] if state else None,
        "total": int(totals["n"]),
        "by_level": {row["level"]: int(row["n"]) for row in by_level},
        # A subscribed id with no records in the window is 0, not absent. The machine did
        # report, and "it did not happen" is the answer a threshold rule needs; leaving the
        # key out would make the variable UNKNOWN and the rule would never settle.
        "by_event_id": {event_id: by_event_id.get(event_id, 0) for event_id in wanted},
    }
