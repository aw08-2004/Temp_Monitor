"""Which apps a managed device may run -- roadmap #23 phase D.

**Its own pair rather than more of `apps.py`, and the split is the important one here.**
`apps.py` holds what the DEVICE says is installed: reported, replaced wholesale, never written
by an operator. This holds what an OPERATOR says is allowed: authored, versioned, and pushed.
Keeping them apart is what makes compliance answerable at all -- the console compares one
against the other, and a module that owned both would be one edit away from writing the
device's own report from the policy that was meant to change it.

**Policies are authored fleet-wide or against a machine list, and RESOLVED into one document
per device.** The device is never given a rule engine: it receives a flat list of packages to
suspend and applies it. Everything about precedence, overlap and mode happens here, where it
can be read, tested and previewed before anything reaches a phone.

The resolution rules, in full, because they are the whole of the feature's behaviour:

  * **Blocklists union.** Every enabled block policy targeting a machine contributes its
    packages. Two policies blocking the same app is not a conflict; it is two people agreeing.
  * **Allowlists INTERSECT.** Each allowlist says "only these may run". Two of them both apply,
    so what survives is what both permit -- the narrower reading, deliberately. A union would
    let a second allowlist quietly widen the first, which is the opposite of what an allowlist
    is for.
  * **An allowlist, once any applies, blocks everything not in it** -- everything installed,
    minus the survivors, minus the never-suspend set.
  * **The never-suspend set wins over everything.** See `NEVER_SUSPEND`.

**A machine with no policy gets an EMPTY document, not no document.** That distinction is the
release valve: an empty document means "nothing is blocked" and lifts whatever was applied
before, while sending nothing would leave a device enforcing a policy that has been deleted.
Removing a machine from a policy has to be able to un-block its apps, and this is where that
works or does not.

**The dead-man switch lives on the agent, not here**, and this module only supplies the number
(`policy.max_age_seconds`). A device that has not heard from its hub in a week lifts all
restrictions on its own. That is a deliberate asymmetry: the hub can make a device more
restricted only while it can reach it, and a phone whose hub is gone must not be a brick.

Kept free of Flask so it can be unit-tested in isolation.
"""
import hashlib
import json
import sqlite3
import time
import uuid

import usage

# ================================
# VOCABULARY
# ================================
#: The policy names packages to suspend. Everything else is untouched.
MODE_BLOCK = "block"
#: The policy names the ONLY packages that may run. Everything else installed is suspended.
MODE_ALLOW = "allow"
MODES = (MODE_BLOCK, MODE_ALLOW)

#: Packages this hub will never ask a device to suspend, whatever a policy says.
#:
#: **This is a hub-side copy of a list the agent enforces independently, and the duplication is
#: deliberate.** The agent's copy is the one that protects the device -- it cannot be edited
#: from here, so a compromised or simply mistaken hub cannot brick a phone. This copy exists so
#: the console never SHOWS an operator a policy it knows will be partly refused, and so the
#: preview is honest. If the two ever disagree, the agent's wins and the console is the thing
#: that is wrong, which is the right way round.
#:
#: The list is prefixes rather than exact names because vendors ship the same component under
#: their own package: the launcher on a Samsung is `com.sec.android.app.launcher`, on a Pixel
#: `com.google.android.apps.nexuslauncher`. Suspending the launcher is not a policy, it is a
#: device somebody has to factory reset.
NEVER_SUSPEND_PREFIXES = (
    # The agent itself. Suspending it would end management, and the device could not be told to
    # stop -- the one failure with no route back except a reset.
    "net.arkeanos.fleethub",
    # The launcher, under every vendor's name for it.
    "com.android.launcher", "com.google.android.apps.nexuslauncher",
    "com.sec.android.app.launcher", "com.miui.home", "com.huawei.android.launcher",
    "com.oneplus.launcher", "com.motorola.launcher",
    # Settings, the dialer and the emergency app. A device that cannot be configured, cannot
    # make a call, or cannot reach emergency services is not a managed device.
    "com.android.settings", "com.android.dialer", "com.google.android.dialer",
    "com.samsung.android.dialer", "com.android.emergency", "com.android.server.telecom",
    "com.android.phone",
    # The system UI and the package installer: without these the device has no status bar, no
    # notifications, and no way to install the fix.
    "com.android.systemui", "com.android.packageinstaller",
    "com.google.android.packageinstaller",
)

# ---------------------------------------------------------------- bounds
MAX_NAME_CHARS = 120
MAX_PACKAGE_CHARS = 255
#: A policy naming more packages than a device has is not a policy anybody wrote by hand.
MAX_PACKAGES_PER_POLICY = 500
MAX_TARGETS_PER_POLICY = 500


class PolicyRejected(ValueError):
    """A policy the hub refuses to store, with the reason. Its own type so the web layer
    answers 400 while a genuine bug still becomes a 500 -- mirrors wake.WakeRejected."""


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_policy_db(db_path):
    """Create the policy tables. Idempotent -- safe to call on every hub start.

    It also creates the usage table, which belongs to another module and is created again from
    app.py -- the same deliberate belt-and-braces `fleet.init_fleet_db` applies to the
    capability table, and for the same reason: `compliance` reads usage to show a budget's
    progress, so a database with policies and no usage table is one where the machine policy
    view raises `no such table` in a module the reader has no reason to connect to it.
    """
    usage.init_usage_db(db_path)
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_policies (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                mode         TEXT NOT NULL,
                enabled      INTEGER NOT NULL DEFAULT 1,
                fleet_wide   INTEGER NOT NULL DEFAULT 0,
                created_by   TEXT NOT NULL DEFAULT '',
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_policy_packages (
                policy_id TEXT NOT NULL,
                package   TEXT NOT NULL,
                PRIMARY KEY (policy_id, package)
            )
            """
        )
        # Empty for a fleet-wide policy. Kept as a table rather than a JSON column so that
        # "which policies target this machine" is a join rather than a scan over every policy
        # -- the resolver asks it on every heartbeat that carries a stale version.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_policy_targets (
                policy_id TEXT NOT NULL,
                machine   TEXT NOT NULL,
                PRIMARY KEY (policy_id, machine)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_app_policy_targets_machine "
                     "ON app_policy_targets(machine)")
        # Time restrictions (roadmap #23 phase E): curfews and daily budgets. They ride the
        # same resolved document as the app blocklist, because the device enforces one flat
        # answer and a second channel would be a second thing to keep in step.
        #
        # `rules_json` is a DOCUMENT rather than a relation, and that is the one place this
        # module departs from the house pattern. A window is {days, start, end, packages} and a
        # budget is {package, minutes}; normalised, they would be two more tables that are only
        # ever read whole, reassembled on every heartbeat, and never queried by their parts. The
        # shape is consumed verbatim by the device, so it is stored the way it is consumed.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS time_policies (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                enabled    INTEGER NOT NULL DEFAULT 1,
                fleet_wide INTEGER NOT NULL DEFAULT 0,
                rules_json TEXT NOT NULL DEFAULT '{}',
                created_by TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS time_policy_targets (
                policy_id TEXT NOT NULL,
                machine   TEXT NOT NULL,
                PRIMARY KEY (policy_id, machine)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_time_policy_targets_machine "
                     "ON time_policy_targets(machine)")
        # What the DEVICE said happened, which is not the same as what was asked for. One row
        # per machine: only the latest application matters, and the history of a policy that
        # has been superseded is noise.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_policy_state (
                machine     TEXT PRIMARY KEY,
                version     TEXT NOT NULL DEFAULT '',
                applied_at  INTEGER,
                failed_json TEXT NOT NULL DEFAULT '[]',
                error       TEXT NOT NULL DEFAULT '',
                reported_at INTEGER NOT NULL
            )
            """
        )


# ================================
# NEVER-SUSPEND
# ================================
def is_protected(package):
    """Whether this package is one the hub will never ask a device to suspend.

    Prefix matching, because the same component ships under a different package on every
    vendor's build -- see NEVER_SUSPEND_PREFIXES.
    """
    name = str(package or "").strip().lower()
    return bool(name) and any(name.startswith(p) for p in NEVER_SUSPEND_PREFIXES)


# ================================
# AUTHORING
# ================================
def _clean(value, limit):
    return str(value if value is not None else "").strip()[:limit]


def _clean_packages(raw):
    """Normalise a policy's package list, or raise.

    Protected packages are DROPPED here rather than refused, and the caller is told which --
    see `validate`. Refusing the whole policy because somebody ticked the launcher would make
    the safest possible mistake the most annoying one; dropping it silently would be worse
    still, which is why the count comes back.
    """
    if not isinstance(raw, list):
        raise PolicyRejected("A policy needs a list of packages.")
    names, seen, dropped = [], set(), []
    for item in raw[:MAX_PACKAGES_PER_POLICY]:
        package = _clean(item, MAX_PACKAGE_CHARS)
        if not package or package in seen:
            continue
        seen.add(package)
        if is_protected(package):
            dropped.append(package)
            continue
        names.append(package)
    return sorted(names), sorted(dropped)


def validate(name, mode, packages, machines, fleet_wide):
    """Normalise and check a policy. Returns the cleaned parts plus what was dropped.

    Raises PolicyRejected with a sentence an operator can act on. Kept separate from `create`
    so the console can validate a draft -- and so the preview can be built from exactly the
    values that would be stored, rather than from what was typed.
    """
    name = _clean(name, MAX_NAME_CHARS)
    if not name:
        raise PolicyRejected("A policy needs a name.")
    mode = _clean(mode, 16).lower()
    if mode not in MODES:
        raise PolicyRejected(f"A policy is '{MODE_BLOCK}' or '{MODE_ALLOW}', not {mode!r}.")

    cleaned, dropped = _clean_packages(packages)
    if not cleaned and mode == MODE_BLOCK:
        # An allowlist with no packages is a real (if drastic) statement -- "nothing but the
        # protected set may run". A blocklist with none does nothing at all, and an operator
        # who saved one would be told their policy applied while it changed nothing.
        raise PolicyRejected("A block policy with no packages would do nothing. Add at least "
                             "one, or delete the policy.")

    fleet_wide = bool(fleet_wide)
    targets = []
    if not fleet_wide:
        if not isinstance(machines, list):
            raise PolicyRejected("A policy targets the whole fleet or a list of machines.")
        seen = set()
        for item in machines[:MAX_TARGETS_PER_POLICY]:
            machine = _clean(item, 200)
            if machine and machine not in seen:
                seen.add(machine)
                targets.append(machine)
        if not targets:
            raise PolicyRejected("A policy that is not fleet-wide needs at least one machine.")
    return {"name": name, "mode": mode, "packages": cleaned, "machines": targets,
            "fleet_wide": fleet_wide, "dropped": dropped}


def create_policy(db_path, *, name, mode, packages, machines=None, fleet_wide=False,
                  enabled=True, actor="", now=None):
    """Store a new policy. Returns its id."""
    parts = validate(name, mode, packages, machines, fleet_wide)
    policy_id = uuid.uuid4().hex
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO app_policies(id, name, mode, enabled, fleet_wide, created_by, "
            "                         created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (policy_id, parts["name"], parts["mode"], 1 if enabled else 0,
             1 if parts["fleet_wide"] else 0, _clean(actor, 200), now, now))
        _write_members(conn, policy_id, parts)
    return policy_id


def update_policy(db_path, policy_id, *, name, mode, packages, machines=None,
                  fleet_wide=False, enabled=True, now=None):
    """Replace a policy's contents. Returns True if it existed."""
    parts = validate(name, mode, packages, machines, fleet_wide)
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE app_policies SET name = ?, mode = ?, enabled = ?, fleet_wide = ?, "
            "updated_at = ? WHERE id = ?",
            (parts["name"], parts["mode"], 1 if enabled else 0,
             1 if parts["fleet_wide"] else 0, now, policy_id))
        if (cursor.rowcount or 0) == 0:
            return False
        conn.execute("DELETE FROM app_policy_packages WHERE policy_id = ?", (policy_id,))
        conn.execute("DELETE FROM app_policy_targets WHERE policy_id = ?", (policy_id,))
        _write_members(conn, policy_id, parts)
    return True


def _write_members(conn, policy_id, parts):
    conn.executemany("INSERT INTO app_policy_packages(policy_id, package) VALUES (?, ?)",
                     [(policy_id, p) for p in parts["packages"]])
    conn.executemany("INSERT INTO app_policy_targets(policy_id, machine) VALUES (?, ?)",
                     [(policy_id, m) for m in parts["machines"]])


def delete_policy(db_path, policy_id):
    """Remove a policy entirely. Returns True if it existed.

    The devices it covered get an emptier document on their next heartbeat, which LIFTS what it
    applied -- see `resolve_for` on why an empty document is sent rather than nothing.
    """
    with get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM app_policies WHERE id = ?", (policy_id,))
        conn.execute("DELETE FROM app_policy_packages WHERE policy_id = ?", (policy_id,))
        conn.execute("DELETE FROM app_policy_targets WHERE policy_id = ?", (policy_id,))
    return (cursor.rowcount or 0) > 0


def get_policy(db_path, policy_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM app_policies WHERE id = ?", (policy_id,)).fetchone()
        if row is None:
            return None
        packages = [r["package"] for r in conn.execute(
            "SELECT package FROM app_policy_packages WHERE policy_id = ? ORDER BY package",
            (policy_id,))]
        machines = [r["machine"] for r in conn.execute(
            "SELECT machine FROM app_policy_targets WHERE policy_id = ? ORDER BY machine",
            (policy_id,))]
    policy = dict(row)
    policy["enabled"] = bool(policy["enabled"])
    policy["fleet_wide"] = bool(policy["fleet_wide"])
    policy["packages"] = packages
    policy["machines"] = machines
    return policy


def list_policies(db_path):
    with get_conn(db_path) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM app_policies ORDER BY updated_at DESC, name ASC")]
    return [get_policy(db_path, policy_id) for policy_id in ids]


# ================================
# TIME RESTRICTIONS
# ================================
#: A curfew window's days, as indexes into a week that starts on MONDAY.
#:
#: Monday rather than Sunday because the rules people write are "school nights" and "weekends",
#: and a week that starts on Sunday splits the weekend across both ends of the list. The device
#: converts from its own locale's numbering; the wire format is fixed so that a device in a
#: different locale cannot read the same window differently.
DAYS_IN_WEEK = 7
MINUTES_IN_DAY = 24 * 60
#: A budget of more than a day is not a budget. Zero is meaningful and allowed: "this app may
#: not be used at all today", which is a blocklist expressed as a schedule and is a thing
#: somebody will write.
MAX_BUDGET_MINUTES = MINUTES_IN_DAY
MAX_WINDOWS_PER_POLICY = 32
MAX_BUDGETS_PER_POLICY = 64

#: A budget or window naming this package applies to the DEVICE rather than to one app: the
#: whole-screen curfew, and the total-screen-time budget. A sentinel rather than an empty
#: string, so it survives a round trip through JSON and a form field without becoming a package
#: nobody named.
EVERY_PACKAGE = "*"


def _minute_of_day(value, field):
    """Parse "HH:MM" or a plain minute count into minutes past midnight."""
    text = str(value if value is not None else "").strip()
    if ":" in text:
        hours, _, minutes = text.partition(":")
        try:
            total = int(hours) * 60 + int(minutes)
        except ValueError:
            raise PolicyRejected(f"{field} must be a time like 22:00.") from None
    else:
        try:
            total = int(text)
        except ValueError:
            raise PolicyRejected(f"{field} must be a time like 22:00.") from None
    if not 0 <= total < MINUTES_IN_DAY:
        raise PolicyRejected(f"{field} must be between 00:00 and 23:59.")
    return total


def _clean_window(raw):
    """One curfew window: which days, from when to when, and over which apps."""
    if not isinstance(raw, dict):
        raise PolicyRejected("A curfew window must be an object.")
    days = raw.get("days")
    if not isinstance(days, list) or not days:
        raise PolicyRejected("A curfew window needs at least one day.")
    parsed = sorted({int(d) for d in days
                     if isinstance(d, (int, float)) and 0 <= int(d) < DAYS_IN_WEEK})
    if not parsed:
        raise PolicyRejected("A curfew window's days must be 0 (Monday) to 6 (Sunday).")

    start = _minute_of_day(raw.get("start"), "The start of a curfew")
    end = _minute_of_day(raw.get("end"), "The end of a curfew")
    if start == end:
        # Ambiguous rather than empty: it could mean "no time at all" or "the whole day", and a
        # curfew nobody can read the meaning of is worse than one that is refused.
        raise PolicyRejected("A curfew that starts and ends at the same minute is ambiguous. "
                             "Use 00:00 to 23:59 for a whole day.")

    packages, _ = _clean_packages(raw.get("packages") or [])
    return {"days": parsed, "start": start, "end": end,
            # An empty list means the whole device, expressed as the sentinel so the agent has
            # one rule to read rather than a special case for absence.
            "packages": packages or [EVERY_PACKAGE]}


def _clean_budget(raw):
    """One daily budget: how many minutes of one app, or of the device."""
    if not isinstance(raw, dict):
        raise PolicyRejected("A budget must be an object.")
    package = _clean(raw.get("package"), MAX_PACKAGE_CHARS) or EVERY_PACKAGE
    if package != EVERY_PACKAGE and is_protected(package):
        raise PolicyRejected(f"{package} can never be suspended, so a budget on it would do "
                             f"nothing.")
    try:
        minutes = int(raw.get("minutes"))
    except (TypeError, ValueError):
        raise PolicyRejected("A budget needs a number of minutes.") from None
    if not 0 <= minutes <= MAX_BUDGET_MINUTES:
        raise PolicyRejected(f"A budget must be between 0 and {MAX_BUDGET_MINUTES} minutes.")
    return {"package": package, "minutes": minutes}


def validate_time_rules(name, rules, machines, fleet_wide):
    """Normalise and check a time policy. Raises PolicyRejected with a usable sentence."""
    name = _clean(name, MAX_NAME_CHARS)
    if not name:
        raise PolicyRejected("A policy needs a name.")
    if not isinstance(rules, dict):
        raise PolicyRejected("A time policy needs windows, budgets, or both.")

    windows = [_clean_window(w) for w in (rules.get("windows") or [])[:MAX_WINDOWS_PER_POLICY]]
    budgets = [_clean_budget(b) for b in (rules.get("budgets") or [])[:MAX_BUDGETS_PER_POLICY]]
    if not windows and not budgets:
        # A policy that restricts nothing would be reported as applied while changing nothing,
        # which is the same refusal a blocklist with no packages gets and for the same reason.
        raise PolicyRejected("A time policy with no curfew and no budget would do nothing.")

    fleet_wide = bool(fleet_wide)
    targets = []
    if not fleet_wide:
        if not isinstance(machines, list):
            raise PolicyRejected("A policy targets the whole fleet or a list of machines.")
        seen = set()
        for item in machines[:MAX_TARGETS_PER_POLICY]:
            machine = _clean(item, 200)
            if machine and machine not in seen:
                seen.add(machine)
                targets.append(machine)
        if not targets:
            raise PolicyRejected("A policy that is not fleet-wide needs at least one machine.")
    return {"name": name, "windows": windows, "budgets": budgets,
            "machines": targets, "fleet_wide": fleet_wide}


def create_time_policy(db_path, *, name, rules, machines=None, fleet_wide=False,
                       enabled=True, actor="", now=None):
    """Store a new time policy. Returns its id."""
    parts = validate_time_rules(name, rules, machines, fleet_wide)
    policy_id = uuid.uuid4().hex
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO time_policies(id, name, enabled, fleet_wide, rules_json, created_by, "
            "                          created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (policy_id, parts["name"], 1 if enabled else 0,
             1 if parts["fleet_wide"] else 0,
             json.dumps({"windows": parts["windows"], "budgets": parts["budgets"]},
                        sort_keys=True),
             _clean(actor, 200), now, now))
        conn.executemany("INSERT INTO time_policy_targets(policy_id, machine) VALUES (?, ?)",
                         [(policy_id, m) for m in parts["machines"]])
    return policy_id


def update_time_policy(db_path, policy_id, *, name, rules, machines=None, fleet_wide=False,
                       enabled=True, now=None):
    """Replace a time policy's contents. Returns True if it existed."""
    parts = validate_time_rules(name, rules, machines, fleet_wide)
    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE time_policies SET name = ?, enabled = ?, fleet_wide = ?, rules_json = ?, "
            "updated_at = ? WHERE id = ?",
            (parts["name"], 1 if enabled else 0, 1 if parts["fleet_wide"] else 0,
             json.dumps({"windows": parts["windows"], "budgets": parts["budgets"]},
                        sort_keys=True), now, policy_id))
        if (cursor.rowcount or 0) == 0:
            return False
        conn.execute("DELETE FROM time_policy_targets WHERE policy_id = ?", (policy_id,))
        conn.executemany("INSERT INTO time_policy_targets(policy_id, machine) VALUES (?, ?)",
                         [(policy_id, m) for m in parts["machines"]])
    return True


def delete_time_policy(db_path, policy_id):
    """Remove a time policy. Its devices get a lighter schedule on the next heartbeat."""
    with get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM time_policies WHERE id = ?", (policy_id,))
        conn.execute("DELETE FROM time_policy_targets WHERE policy_id = ?", (policy_id,))
    return (cursor.rowcount or 0) > 0


def get_time_policy(db_path, policy_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM time_policies WHERE id = ?",
                           (policy_id,)).fetchone()
        if row is None:
            return None
        machines = [r["machine"] for r in conn.execute(
            "SELECT machine FROM time_policy_targets WHERE policy_id = ? ORDER BY machine",
            (policy_id,))]
    found = dict(row)
    found["enabled"] = bool(found["enabled"])
    found["fleet_wide"] = bool(found["fleet_wide"])
    found["machines"] = machines
    try:
        rules = json.loads(found.pop("rules_json"))
    except (TypeError, ValueError):
        rules = {}
    found["windows"] = rules.get("windows") or []
    found["budgets"] = rules.get("budgets") or []
    return found


def list_time_policies(db_path):
    with get_conn(db_path) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM time_policies ORDER BY updated_at DESC, name ASC")]
    return [get_time_policy(db_path, policy_id) for policy_id in ids]


def time_policies_for(db_path, machine):
    """Every ENABLED time policy that applies to this machine."""
    machine = str(machine or "").strip()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.id FROM time_policies p "
            "LEFT JOIN time_policy_targets t ON t.policy_id = p.id "
            "WHERE p.enabled = 1 AND (p.fleet_wide = 1 OR t.machine = ?) "
            "ORDER BY p.id", (machine,)).fetchall()
    return [get_time_policy(db_path, r["id"]) for r in rows]


def resolve_schedule(db_path, machine):
    """The `schedule` block of the device's document: every window and budget that applies.

    **Merged, never reconciled.** Two policies that both restrict a device both apply, and the
    device enforces the union -- if one says "no TikTok after 22:00" and another says "60
    minutes of TikTok a day", both hold, and whichever bites first is the answer. Trying to
    combine them into a single rule per package would mean inventing a precedence nobody asked
    for, and the strictest reading is the one an operator expects from a restriction.

    A duplicate budget on the same package keeps the SMALLER number, which is that same rule
    applied to the one case where a union has no meaning.

    **No timezone travels with this.** The device evaluates against its own local clock, because
    a curfew is about the evening of the person holding it -- a hub-imposed zone would put a
    traveller's curfew at the wrong hour, and a fleet spread over two zones would need a policy
    each. The device-owner baseline enforces automatic time so the clock is not the operator's
    to move; see the agent's DeviceOwner.
    """
    windows, budgets = [], {}
    for found in time_policies_for(db_path, machine):
        windows.extend(found["windows"])
        for budget in found["budgets"]:
            package = budget["package"]
            if package not in budgets or budget["minutes"] < budgets[package]["minutes"]:
                budgets[package] = budget
    return {
        "windows": windows,
        "budgets": [budgets[k] for k in sorted(budgets)],
    }


# ================================
# RESOLUTION
# ================================
def policies_for(db_path, machine):
    """Every ENABLED policy that applies to this machine -- fleet-wide ones plus the ones
    naming it."""
    machine = str(machine or "").strip()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.id FROM app_policies p "
            "LEFT JOIN app_policy_targets t ON t.policy_id = p.id "
            "WHERE p.enabled = 1 AND (p.fleet_wide = 1 OR t.machine = ?) "
            "ORDER BY p.id", (machine,)).fetchall()
    return [get_policy(db_path, r["id"]) for r in rows]


def resolve_for(db_path, machine, installed=None):
    """The flat document this device should enforce.

    Returns `{"version", "blocked", "policies"}`. `blocked` is the complete list of packages to
    suspend -- the device is never given a rule engine, because a rule engine on a phone is a
    second implementation of these rules that nobody can preview.

    `installed` is the machine's package list (apps.list_apps). It is only needed to expand an
    ALLOWLIST, which by definition names what may run and therefore has to be subtracted from
    something. A blocklist needs none, which is why this argument is optional: a device whose
    inventory has not arrived yet can still be given its blocklist rather than waiting.

    **An allowlist with no inventory yields nothing rather than everything.** Getting that
    backwards would suspend every app on a device the hub has not finished learning about --
    which is the single most destructive thing this feature could do, and it would happen on
    exactly the devices that had just been enrolled.
    """
    policies = policies_for(db_path, machine)
    blocked = set()
    allowed = None

    for policy in policies:
        if policy["mode"] == MODE_BLOCK:
            blocked.update(policy["packages"])
        else:
            # INTERSECT, not union -- see the module docstring. Each allowlist says "only
            # these", and a union would let a second one quietly widen the first.
            names = set(policy["packages"])
            allowed = names if allowed is None else (allowed & names)

    if allowed is not None:
        packages = [a["package"] for a in (installed or [])]
        # No inventory yet: nothing is added. See the docstring -- the other branch would
        # suspend every app on a freshly enrolled device.
        blocked.update(p for p in packages if p not in allowed)

    # The protected set wins over everything, including an allowlist that did not mention it.
    # The agent enforces its own copy of this independently; this is here so the console never
    # shows an operator a policy it already knows will be partly refused.
    final = sorted(p for p in blocked if not is_protected(p))

    # Time restrictions ride the same document (roadmap #23 phase E). They are NOT resolved
    # into `blocked` here, and that is the whole design: a curfew depends on what time it is
    # where the device is, and a budget on how much the device has been used today. Both are
    # facts the hub does not have and cannot have promptly -- so the hub sends the RULES and
    # the device evaluates them, every minute, whether or not the hub is reachable. A device
    # offline at 22:00 still has a curfew.
    schedule = resolve_schedule(db_path, machine)
    time_policies = time_policies_for(db_path, machine)
    return {
        "version": document_version(final, schedule),
        "blocked": final,
        "schedule": schedule,
        # For the console: which policies produced this, so "why is this app blocked" has an
        # answer that is not "read all of them".
        "policies": ([{"id": p["id"], "name": p["name"], "mode": p["mode"]} for p in policies]
                     + [{"id": p["id"], "name": p["name"], "mode": "schedule"}
                        for p in time_policies]),
    }


def document_version(blocked, schedule=None):
    """A stable id for a resolved document, so the heartbeat can skip sending an unchanged one.

    A hash of the CONTENT rather than a counter: two hubs, a restore from backup, or a policy
    edited and edited back all produce the same document and should not cost every device a
    re-application. Sorted before hashing for the same reason -- see `resolve_for`.

    **The schedule is part of the hash.** It has to be: editing a curfew changes nothing about
    the blocked list, so a version over `blocked` alone would leave every device holding the old
    schedule until something unrelated changed. That is the one way this feature could silently
    stop updating, and it would look exactly like a curfew somebody set and nobody noticed
    working.
    """
    body = {"blocked": sorted(blocked), "schedule": schedule or {}}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ================================
# WHAT THE DEVICE ACTUALLY DID
# ================================
def record_state(db_path, machine, payload, now=None):
    """Store a device's report of applying a policy. Returns True if anything was stored.

    Called from the heartbeat, so it is bounded, type-checked and never fatal.

    **`failed` is the field this exists for.** `setPackagesSuspended` returns the packages it
    could not suspend, and a policy reported as applied while three of its targets are still
    running is worse than no policy at all. The console shows that difference; without this
    report it could only be inferred from the next inventory, minutes later.
    """
    machine = _clean(machine, 200)
    if not machine or not isinstance(payload, dict):
        return False
    raw_failed = payload.get("failed")
    failed = sorted({_clean(p, MAX_PACKAGE_CHARS) for p in raw_failed
                     if _clean(p, MAX_PACKAGE_CHARS)}) if isinstance(raw_failed, list) else []
    applied_at = payload.get("applied_at")
    try:
        applied_at = int(applied_at)
    except (TypeError, ValueError):
        applied_at = None

    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO machine_policy_state(machine, version, applied_at, failed_json, "
            "                                 error, reported_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(machine) DO UPDATE SET version = excluded.version, "
            "applied_at = excluded.applied_at, failed_json = excluded.failed_json, "
            "error = excluded.error, reported_at = excluded.reported_at",
            (machine, _clean(payload.get("version"), 64), applied_at, json.dumps(failed),
             _clean(payload.get("error"), 500), now))
    return True


def get_state(db_path, machine):
    """What this device last said about applying its policy, or None if it never has."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM machine_policy_state WHERE machine = ?",
                           (str(machine or "").strip(),)).fetchone()
    if row is None:
        return None
    state = dict(row)
    state.pop("machine", None)
    try:
        state["failed"] = json.loads(state.pop("failed_json"))
    except (TypeError, ValueError):
        state["failed"] = []
    return state


def compliance(db_path, machine, installed=None):
    """What was asked for, what the device says it did, and what its inventory shows.

    Three sources rather than two on purpose. The policy is intent; the device's own report is
    what it believes it did; the inventory is what the framework says is true. An operator
    needs all three, because the interesting failures are exactly where they disagree -- a
    package the agent thought it suspended and the framework did not, or one suspended by
    something other than this policy.
    """
    document = resolve_for(db_path, machine, installed)
    state = get_state(db_path, machine)
    by_package = {a["package"]: a for a in (installed or [])}

    # Today's usage against each budget, from the device's own most recent reported day. Shown
    # because a budget is the one restriction whose effect an operator cannot see from the
    # blocked list -- an app is not suspended until somebody has used it enough, and "45 of 60
    # minutes" is the difference between a policy that is working and one nobody has hit yet.
    days = usage.days_for(db_path, machine, limit=1)
    today = days[0]["day"] if days else ""
    spent = usage.day_totals(db_path, machine, today) if today else {}
    budgets = []
    for budget in document["schedule"]["budgets"]:
        package = budget["package"]
        used = sum(spent.values()) if package == EVERY_PACKAGE else spent.get(package, 0)
        budgets.append({
            "package": package,
            "minutes": budget["minutes"],
            "used_minutes": used // 60,
            "day": today,
            "over": used >= budget["minutes"] * 60,
        })

    blocked = document["blocked"]
    # Only packages the device actually HAS can be enforced. A policy naming an app that is not
    # installed is not a failure -- it is a policy written for a fleet rather than for one
    # device, which is the ordinary case.
    present = [p for p in blocked if p in by_package]
    enforced = [p for p in present if by_package[p].get("suspended")]
    return {
        "version": document["version"],
        "blocked": blocked,
        "policies": document["policies"],
        "applied_version": (state or {}).get("version", ""),
        "applied_at": (state or {}).get("applied_at"),
        "failed": (state or {}).get("failed", []),
        "error": (state or {}).get("error", ""),
        # "Has the device caught up with the current document" -- a version mismatch is the
        # ordinary state for the minute after an edit, not an error.
        "current": bool(state) and state.get("version") == document["version"],
        "schedule": document["schedule"],
        "budgets": budgets,
        "counts": {
            "blocked": len(blocked),
            "installed": len(present),
            "enforced": len(enforced),
            "not_enforced": len(present) - len(enforced),
            "windows": len(document["schedule"]["windows"]),
        },
    }


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine from every policy, and its application state.

    A stale target is worse here than elsewhere: if the hostname is reused, a different device
    silently inherits a policy nobody aimed at it -- and the symptom is apps that will not open
    on a machine whose page shows no reason why.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM app_policy_targets WHERE machine = ?", (machine,))
        # Schedules too, for the same reason and with a sharper version of it: a reused
        # hostname inheriting a curfew produces a phone that stops working every evening, with
        # nothing on its page saying why.
        conn.execute("DELETE FROM time_policy_targets WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM machine_policy_state WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Follow a machine through a duplicate-serial merge. Policy targets MOVE -- the survivor
    is the same physical device and must keep being covered by whatever covered it."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE OR IGNORE app_policy_targets SET machine = ? WHERE machine = ?",
            (new_machine, old_machine))
        conn.execute("DELETE FROM app_policy_targets WHERE machine = ?", (old_machine,))
        conn.execute(
            "UPDATE OR IGNORE time_policy_targets SET machine = ? WHERE machine = ?",
            (new_machine, old_machine))
        conn.execute("DELETE FROM time_policy_targets WHERE machine = ?", (old_machine,))
        existing = conn.execute("SELECT 1 FROM machine_policy_state WHERE machine = ?",
                                (new_machine,)).fetchone()
        if existing is not None:
            conn.execute("DELETE FROM machine_policy_state WHERE machine = ?", (old_machine,))
        else:
            conn.execute("UPDATE machine_policy_state SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
