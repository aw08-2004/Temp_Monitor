"""What a machine can actually do -- roadmap #23.

**A version number cannot answer the question this feature exists for.** The hub has always
inferred a machine's abilities from how new its agent is: `MIN_PTY_AGENT`, `MIN_PROCESS_AGENT`
and their friends in `hub/static/js/` hardcode the agent minor that introduced a feature, and
a machine at or above it is assumed able. That works while every managed machine is a Windows
PC running one agent train, because "newer" really does mean "can do more".

It stops working the moment a machine's limits are a fact about its PLATFORM rather than its
position in a backlog. An Android device cannot run a script, cannot be rebooted by an app,
and cannot enumerate another app's processes -- not in this version and not in any future one.
There is no version number that expresses "will never be able to", so the Android agent sits
permanently below `AGENT_TRAIN_MIN_VERSION` and every version gate reads it as ancient. That
is the correct answer for the wrong reason, and it only covers the gates that run at all.

**And for scheduled work, none of them run.** `MIN_*_AGENT` is console-side JavaScript: it
stops an operator being OFFERED a button. Nothing evaluates it for a command the backup
scheduler, the patch scheduler, the deployment scheduler or the rules engine dispatches,
because those target a machine SET rather than a console button. The first Android device to
enroll was inside a fleet-wide backup profile and was queued `backup_files` within a minute.
That is the bug this module closes, and it is why the check lives in `fleet.create_command` --
the one function every command in the system passes through -- rather than in each scheduler,
which is precisely where it was already forgotten four times over.

So: the agent REPORTS what it can do, the hub stores it, and both the console and the
schedulers consult it. Versions keep meaning "which release train", capabilities mean "which
feature set", and the two stop being conflated.

**The absent-report rule, and it is the load-bearing one.** A machine that has reported no
capabilities is UNKNOWN, never INCAPABLE. Every Windows agent in the field reports nothing here
and must keep working exactly as it did -- so absence permits everything, and only an explicit
report can refuse anything. Getting this backwards would stop every command on every machine
in the fleet on the morning this shipped, which is a far worse failure than the one being
fixed. `_missing_reads_as_capable` is where that lives, in one place, so it cannot be
re-decided differently by the next caller.

**Two lists, not one.** `commands` is what the agent's dispatcher can execute -- it maps
one-to-one onto `fleet.ALL_COMMANDS` and is what `create_command` checks. `features` is
everything that is NOT a command: on-demand location, app policy, a usage ledger. Those arrive
on the heartbeat's policy channel rather than through the command queue, so a single list would
mix two vocabularies and the console could not tell which entries it may render a button for.
The agent derives `commands` from its own executor set (see the Android agent's
AgentCapabilities), so the two cannot drift the way a hand-maintained list would.

**Own table, dropped by forget_machine.** Same discipline as wake.py and bios.py: this module
owns the whole of its storage, so a deleted machine leaves none of it behind and a reused
hostname cannot inherit another device's idea of what it can do -- which here would mean
silently refusing commands on a PC that can run them.

Kept free of Flask so it can be unit-tested in isolation.
"""
import json
import sqlite3
import time

# ================================
# VOCABULARY
# ================================
#: The platforms a machine can report. Deliberately coarse and deliberately CLOSED: this is an
#: enforcement input, so an unrecognised value is stored as "" (unknown) rather than trusted --
#: the same reasoning that keeps app.normalize_os's fuzzy caption bucketing away from every
#: decision except a Dashboard tally.
PLATFORM_WINDOWS = "windows"
PLATFORM_LINUX = "linux"
PLATFORM_ANDROID = "android"
PLATFORMS = (PLATFORM_WINDOWS, PLATFORM_LINUX, PLATFORM_ANDROID)

# ---------------------------------------------------------------- non-command features
#: On-demand location (roadmap #23 phase B). A feature rather than a command entry because the
#: console has to decide whether to render the map fold BEFORE anyone issues anything.
FEATURE_LOCATE = "locate"
#: App inventory and suspension policy (phase D). Arrives on the heartbeat's policy block, not
#: through the command queue, which is exactly why it cannot be expressed as a command name.
FEATURE_APP_POLICY = "app_policy"
#: Screen-time budgets and blocked hours (phase E), enforced agent-side from a persisted
#: schedule. Same channel, same reason.
FEATURE_TIME_POLICY = "time_policy"
#: Usage access has actually been granted on the device (phase E). Separate from the feature
#: above because the two fail differently: without usage access a curfew still holds and a
#: BUDGET never fires, silently, because "nothing was used" and "I was not allowed to look" are
#: the same zero. On Android it is an appop that no Device Owner can grant, so this is the only
#: way the console can say "somebody has to walk over to that device".
FEATURE_USAGE_ACCESS = "usage_access"
#: The device is enrolled as an Android Device Owner (phase A). Not a capability the agent
#: chooses -- it is a fact about how the device was provisioned, and every policy feature
#: degrades without it. Reported so the console can say "this device is not fully managed"
#: rather than letting a policy silently do nothing.
FEATURE_DEVICE_OWNER = "device_owner"
FEATURES = (FEATURE_LOCATE, FEATURE_APP_POLICY, FEATURE_TIME_POLICY, FEATURE_USAGE_ACCESS,
            FEATURE_DEVICE_OWNER)

# ---------------------------------------------------------------- ingest caps
#: Bounds a misbehaving or hostile agent, not a real one. The largest honest report is the
#: Windows agent's, which would be the whole of fleet.ALL_COMMANDS -- comfortably under this.
MAX_COMMANDS = 128
MAX_FEATURES = 64
#: Long enough for the longest real command type with room to spare; short enough that a
#: megabyte of junk cannot be stored one name at a time.
MAX_NAME_CHARS = 64


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_capabilities_db(db_path):
    """Create the capability table. Idempotent -- safe to call on every hub start next to
    app.init_db().

    `commands_json` and `features_json` are NULLABLE, and that is the absent-report rule in
    schema form: SQL NULL means "this agent said nothing about its commands", an empty JSON
    array means "it said it has none". Collapsing the two into one empty list would make a
    machine that reported only its platform look like one that had refused everything.
    """
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_capabilities (
                machine       TEXT PRIMARY KEY,
                platform      TEXT NOT NULL DEFAULT '',
                commands_json TEXT,
                features_json TEXT,
                reported_at   INTEGER NOT NULL
            )
            """
        )
        # The Dashboard tally and the Inventory filter both ask "which machines are on
        # platform X", which is a scan over a column with three distinct values -- cheap on a
        # fleet of hundreds, and the index costs one page.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_capabilities_platform "
                     "ON machine_capabilities(platform)")


# ================================
# INGEST
# ================================
def _name(value):
    """One reported identifier, trimmed and bounded, or "" if it is not usable.

    Command types and feature slugs are ASCII identifiers everywhere else in the hub, and this
    value is compared against fleet.ALL_COMMANDS. Anything else is a report we could not act on
    even if we stored it, so it is dropped rather than kept as a name that can never match.
    """
    text = str(value if value is not None else "").strip()[:MAX_NAME_CHARS]
    return text if text and text.replace("_", "").isalnum() else ""


def _name_list(raw, cap):
    """A reported list of identifiers, or None if the key was absent or not a list.

    None is not "empty" -- see init_capabilities_db. A caller that cannot tell the two apart
    turns silence into a refusal.
    """
    if not isinstance(raw, list):
        return None
    names, seen = [], set()
    for item in raw[:cap]:
        name = _name(item)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def clean_report(payload):
    """Normalise a reported capability block, or None if it is not one.

    Returns `{"platform", "commands", "features"}` where either list may be None. Kept separate
    from `record_capabilities` so a test can assert on the parse without a database, and so the
    web layer can echo back exactly what was understood.
    """
    if not isinstance(payload, dict):
        return None
    platform = str(payload.get("platform") or "").strip().lower()
    if platform not in PLATFORMS:
        # An unrecognised platform is stored as unknown rather than verbatim. This value gates
        # what the console offers, and a machine free to name its own platform could name one
        # the hub has no rules for and so be treated as an exception to all of them.
        platform = ""
    commands = _name_list(payload.get("commands"), MAX_COMMANDS)
    features = _name_list(payload.get("features"), MAX_FEATURES)
    if not platform and commands is None and features is None:
        # Nothing usable in it at all. Returning a hollow report would stamp a row that then
        # reads as "this machine HAS reported", which is the one thing the absent-report rule
        # needs to be able to tell apart.
        return None
    return {"platform": platform, "commands": commands, "features": features}


def record_capabilities(db_path, machine, payload, now=None):
    """Store what a machine says it can do. Returns True if anything was written.

    **Written only when the report DIFFERS from the stored one.** The agent sends this on every
    heartbeat rather than change-only, which is deliberate: the block is a hundred bytes, it
    never changes in normal operation, and an agent carrying its own "have I sent this yet"
    flag would stop re-sending after a hub restored its database from a backup -- leaving a
    machine silently un-gated until somebody reinstalled the agent. Making the HUB do the
    comparison keeps the agent stateless and self-healing, at the cost of one indexed SELECT
    per heartbeat, which is the cost the two watch flags beside it already pay.

    `reported_at` is therefore "when this capability set first appeared", not "when we last
    heard from this machine". Liveness already has an answer (`machine_info.updated_at`), and a
    second, subtly different one is how a page ends up showing two disagreeing "last seen"
    values for one machine.

    Called from the heartbeat, so like wake.record_network the whole path is bounded,
    type-checked and never fatal at the caller: a malformed report costs a stale capability
    set, never a heartbeat. A heartbeat that 500s takes the machine offline fleet-wide.
    """
    machine = str(machine or "").strip()
    report = clean_report(payload)
    if not machine or report is None:
        return False

    commands_json = None if report["commands"] is None else json.dumps(report["commands"])
    features_json = None if report["features"] is None else json.dumps(report["features"])
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT platform, commands_json, features_json FROM machine_capabilities "
            "WHERE machine = ?", (machine,)).fetchone()
        if (row is not None and row["platform"] == report["platform"]
                and row["commands_json"] == commands_json
                and row["features_json"] == features_json):
            return False
        conn.execute(
            "INSERT INTO machine_capabilities(machine, platform, commands_json, "
            "                                 features_json, reported_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(machine) DO UPDATE SET "
            "platform = excluded.platform, commands_json = excluded.commands_json, "
            "features_json = excluded.features_json, reported_at = excluded.reported_at",
            (machine, report["platform"], commands_json, features_json,
             int(time.time()) if now is None else int(now)))
    return True


# ================================
# READ
# ================================
def _decode(raw):
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def get_capabilities(db_path, machine):
    """What one machine has reported.

    A machine that has never reported comes back as `reported_at: None` with both lists None --
    deliberately distinct from a machine that reported an empty list. The first is every
    Windows agent in the field and permits everything; the second is a claim, and is enforced.
    Same distinction wake.get_network draws with a null `reported_at`.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT platform, commands_json, features_json, reported_at "
            "FROM machine_capabilities WHERE machine = ?", (str(machine or "").strip(),)
        ).fetchone()
    if row is None:
        return {"platform": "", "commands": None, "features": None, "reported_at": None}
    return {
        "platform": row["platform"] or "",
        "commands": _decode(row["commands_json"]),
        "features": _decode(row["features_json"]),
        "reported_at": row["reported_at"],
    }


def platforms_for(db_path, machines=None):
    """`{machine: platform}` for the machines that have reported one.

    A machine absent from the mapping has not said, and the caller must render that as unknown
    rather than as Windows. The fleet is mostly Windows, so assuming it would be right almost
    every time -- which is exactly what would make the wrong answer impossible to notice.
    """
    clauses, params = ["platform != ''"], []
    if machines is not None:
        scope = [str(m).strip() for m in machines if str(m or "").strip()]
        if not scope:
            return {}
        clauses.append(f"machine IN ({','.join('?' for _ in scope)})")
        params.extend(scope)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT machine, platform FROM machine_capabilities WHERE "
            + " AND ".join(clauses), params).fetchall()
    return {row["machine"]: row["platform"] for row in rows}


def platform_of(db_path, machine):
    """This machine's reported platform, or "" if it has not said."""
    return get_capabilities(db_path, machine)["platform"]


# ================================
# ENFORCEMENT
# ================================
def _missing_reads_as_capable(reported):
    """The absent-report rule, in one place so it cannot be re-decided differently elsewhere.

    A machine that has told us nothing about its commands can be sent anything. Every Windows
    agent in the field is in that state and must stay usable; a hub that defaulted to "refuse"
    would, on the morning this shipped, stop every command on every machine in the fleet while
    reporting nothing wrong anywhere.
    """
    return reported is None


def can_run(db_path, machine, command_type):
    """Whether this machine can be sent this command type.

    True unless the machine has EXPLICITLY reported a command list that leaves it out. See
    `_missing_reads_as_capable` for why that asymmetry is the whole design.
    """
    reported = get_capabilities(db_path, machine)["commands"]
    if _missing_reads_as_capable(reported):
        return True
    return str(command_type or "").strip() in reported


def refusal_for(db_path, machine, command_type):
    """The sentence to refuse this command with, or None if it is allowed.

    Phrased for the operator reading a scheduler's log or a 400, so it names the machine, its
    platform and the command. "Unsupported" on its own sends somebody looking for a version of
    the agent that will do it, and there is not going to be one -- the same mistake the Android
    dispatcher's own message was rewritten to avoid.
    """
    capabilities = get_capabilities(db_path, machine)
    if _missing_reads_as_capable(capabilities["commands"]):
        return None
    command_type = str(command_type or "").strip()
    if command_type in capabilities["commands"]:
        return None
    platform = capabilities["platform"] or "this platform"
    return (f"{machine} reports that it cannot run '{command_type}'. It is a {platform} "
            f"machine, and that is a limit of the platform rather than of the agent's "
            f"version, so a newer agent will not change it.")


def supports(db_path, machine, feature):
    """Whether this machine reported a non-command feature (location, app policy, ...).

    Note the asymmetry with `can_run`, and it is deliberate. An unreported COMMAND list permits
    everything, because the fleet is full of agents that will never report one. An unreported
    FEATURE is False, because a feature exists only on an agent new enough to report it -- so
    "unknown" and "no" really are the same answer here, and defaulting the other way would put
    a Locate button on every Windows PC in the fleet.
    """
    reported = get_capabilities(db_path, machine)["features"]
    return bool(reported) and str(feature or "").strip() in reported


def filter_machines(db_path, machines, command_type):
    """The subset of `machines` that can be sent this command type, in the order given.

    For the schedulers, which choose a machine SET and then queue one command each. They call
    this so a device that cannot answer is never targeted at all; `create_command` refuses as
    a backstop for the ones that forget. Both exist on purpose -- the backstop is what makes
    the guarantee real, and the filter is what stops a nightly backup profile recording a
    failed target against a phone every night for the rest of its life.
    """
    names = [str(m).strip() for m in (machines or []) if str(m or "").strip()]
    if not names:
        return []
    command_type = str(command_type or "").strip()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT machine, commands_json FROM machine_capabilities "
            f"WHERE machine IN ({','.join('?' for _ in names)})", names).fetchall()
    reported = {row["machine"]: _decode(row["commands_json"]) for row in rows}
    return [name for name in names
            if _missing_reads_as_capable(reported.get(name))
            or command_type in reported[name]]


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's capability report.

    Same lifecycle discipline as wake.forget_machine, with a hazard of its own: this row is an
    ENFORCEMENT input, so one left behind under a reused hostname would silently refuse
    commands on a PC that can run them perfectly well -- and the refusal would name a platform
    that machine is not.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_capabilities WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move a capability report during a duplicate-serial merge.

    The SURVIVOR wins a collision, exactly as bios.rename_machine does and for the same reason:
    both rows describe one physical machine, and the survivor is the one still reporting. The
    merged-away row is dropped rather than merged, because a union of two command lists would
    claim an ability neither agent had reported on its own.
    """
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM machine_capabilities WHERE machine = ? LIMIT 1",
            (new_machine,)).fetchone()
        if existing is not None:
            conn.execute("DELETE FROM machine_capabilities WHERE machine = ?", (old_machine,))
        else:
            conn.execute("UPDATE machine_capabilities SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
