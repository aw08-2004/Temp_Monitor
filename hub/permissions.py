"""Permission groups -- the hub's access-control model.

Until now the perimeter was flat: any address in ALLOWED_EMAILS could see every
machine and run code as SYSTEM on any of them. That cannot serve an IT group where
the Hospital operator manages Hospital PCs and the HR operator manages HR PCs, and
nothing else on the roadmap (package deploys, backups, remote control) can be
meaningfully authorized without a finer model. This module is that model.

  * A **permission group** is {name, capabilities, machine scope}. Capabilities are
    granular toggles (see CAPABILITIES); machine scope is either an explicit list of
    machines or "every machine".
  * A **user** (by email) belongs to zero or more groups. Their effective permission
    is the UNION across their groups -- capabilities union, machine scope union.
  * **ALLOWED_EMAILS is the break-glass superuser list.** Membership grants every
    capability over every machine, bypassing groups entirely. It is both the
    bootstrap path (day one, before any group exists, someone has to be able to
    create the first one) and the safety net if group config is ever broken. That is
    why nothing here guards against "deleting the last admin group" -- such a guard
    would be protecting against a lockout that cannot happen.

"Admin" is deliberately NOT a hardcoded tier. It is just a group holding
MANAGE_PERMISSION_GROUPS, which is what lets an operator hand out a narrow slice of
admin (say, backups only) without handing out everything.

Two-layer enforcement, applied wherever a machine is touched:
  1. is there a session at all (app.py's login_required), and
  2. does the caller hold the capability, AND is the target machine in their scope.

Layer 2 gates READS as well as writes -- an HR tech should not *see* Hospital
machines in a list, not merely be blocked from acting on them. The Flask-facing half
of that (decorators, per-request caching, list filtering) lives in permissions_web.py;
this module stays Flask-free so it can be unit-tested in isolation, exactly like
fleet.py and settings.py.

Member rows carry an `ad_group_dn` column alongside `email` from day one. Nothing
reads it yet -- it is where Entra/AD group mappings will land (roadmap #4) so that
feature does not need a schema migration on a table this security-critical.
"""
import json
import sqlite3
import threading
import time
import uuid

import fleet

# ================================
# CAPABILITIES
# ================================
# Each is an independent toggle an admin sets per group. Order is the order the admin
# UI renders them in, so it runs least- to most-privileged.
VIEW = "view"
ISSUE_COMMANDS = "issue_commands"
REMOTE_CONTROL = "remote_control"
DEPLOY_PACKAGES = "deploy_packages"
MANAGE_BACKUPS = "manage_backups"
MANAGE_SETTINGS = "manage_settings"
MANAGE_USERS = "manage_users"
MANAGE_PERMISSION_GROUPS = "manage_permission_groups"

CAPABILITIES = (
    VIEW,
    ISSUE_COMMANDS,
    REMOTE_CONTROL,
    DEPLOY_PACKAGES,
    MANAGE_BACKUPS,
    MANAGE_SETTINGS,
    MANAGE_USERS,
    MANAGE_PERMISSION_GROUPS,
)

# Shown in the admin UI. Kept here, not in the template, so the API is self-describing
# and a new capability needs one edit rather than two.
CAPABILITY_LABELS = {
    VIEW: ("View", "See these machines, their history, and their command results."),
    ISSUE_COMMANDS: ("Issue commands",
                     "Run scripts and send restart/shutdown/install commands. This is "
                     "code execution as SYSTEM on the machines in scope."),
    REMOTE_CONTROL: ("Remote control", "Start a remote view/control session."),
    DEPLOY_PACKAGES: ("Deploy packages", "Schedule software deployments."),
    MANAGE_BACKUPS: ("Manage backups", "Configure backups and trigger restores."),
    MANAGE_SETTINGS: ("Manage settings",
                      "Change hub settings, and administer machine records "
                      "(delete, merge, pin a sensor, dismiss alerts)."),
    MANAGE_USERS: ("Manage users",
                   "Add, edit, and remove entries in the registered-users directory. "
                   "This is a profile directory, not access -- membership in a "
                   "permission group is what grants what someone can do."),
    MANAGE_PERMISSION_GROUPS: ("Manage permission groups",
                               "Create and edit permission groups -- i.e. grant "
                               "anyone, including themselves, any of the above."),
}

# Machine-scope resolution modes. "list" is the v1 explicit list; "all" is the
# fleet-wide group (a global auditor, or the group that replaces break-glass once a
# deployment stops relying on ALLOWED_EMAILS). Roadmap #4 adds "ad_ou" here, which is
# why this is a mode column rather than an is_all_machines flag.
SCOPE_LIST = "list"
SCOPE_ALL = "all"
SCOPE_MODES = (SCOPE_LIST, SCOPE_ALL)

MAX_NAME_CHARS = 80


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_permissions_db(db_path):
    """Create the permission tables if absent. Idempotent -- safe to call next to the
    other init_*_db() functions on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_groups (
                id                TEXT PRIMARY KEY,
                name              TEXT NOT NULL,
                description       TEXT,
                capabilities_json TEXT NOT NULL,   -- JSON array of CAPABILITIES members
                scope_mode        TEXT NOT NULL DEFAULT 'list',
                created_at        INTEGER NOT NULL,
                updated_at        INTEGER NOT NULL,
                updated_by        TEXT
            )
            """
        )
        # Case-insensitive: "Hospital IT" and "hospital it" being two groups is a
        # configuration accident every time, never an intent.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_permission_groups_name "
            "ON permission_groups(name COLLATE NOCASE)"
        )
        # Machines are referenced by hostname, matching machine_info's primary key. A
        # row for a machine that no longer exists is harmless (it grants access to
        # nothing) and is deliberately not foreign-keyed: deleting a machine must not
        # silently rewrite an operator's group definition.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_group_machines (
                group_id TEXT NOT NULL,
                machine  TEXT NOT NULL,
                PRIMARY KEY (group_id, machine)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pg_machines_machine "
            "ON permission_group_machines(machine)"
        )
        # Exactly one of email / ad_group_dn is set per row. SQLite treats NULLs as
        # distinct in a UNIQUE index, so the two indexes below coexist happily with
        # the other column left NULL.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_group_members (
                group_id    TEXT NOT NULL,
                email       TEXT,
                ad_group_dn TEXT,
                added_at    INTEGER NOT NULL,
                added_by    TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_members_email "
            "ON permission_group_members(group_id, email)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_members_ad "
            "ON permission_group_members(group_id, ad_group_dn)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pg_members_lookup "
            "ON permission_group_members(email)"
        )


# ================================
# NORMALISATION & VALIDATION
# ================================
def normalize_email(email):
    """Emails are identity here, so they are compared lowercased and stripped -- the
    same normalisation app.py applies to ALLOWED_EMAILS and to the OAuth claim. Doing
    it in one place is what keeps 'Ann@x.com' from being a different operator."""
    return str(email or "").strip().lower()


def normalize_machine(machine):
    return str(machine or "").strip()


def _validate_name(name):
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("A group name is required.")
    if len(cleaned) > MAX_NAME_CHARS:
        raise ValueError(f"Group name must be at most {MAX_NAME_CHARS} characters.")
    return cleaned


def _validate_capabilities(capabilities):
    """Accepts a list of capability names, or a dict of {name: bool} as the admin form
    posts it. Returns a sorted list. Unknown names are an error rather than being
    dropped: silently ignoring a typo'd capability is how a group ends up quietly
    less privileged than the admin believes it is."""
    if isinstance(capabilities, dict):
        names = [k for k, v in capabilities.items() if v]
    elif isinstance(capabilities, (list, tuple, set, frozenset)):
        names = list(capabilities)
    elif capabilities is None:
        names = []
    else:
        raise ValueError("capabilities must be a list of capability names.")

    cleaned = []
    for name in names:
        text = str(name or "").strip()
        if text not in CAPABILITIES:
            raise ValueError(f"Unknown capability: {text!r}")
        if text not in cleaned:
            cleaned.append(text)
    return sorted(cleaned, key=CAPABILITIES.index)


def _validate_scope(scope_mode, machines):
    mode = str(scope_mode or SCOPE_LIST).strip().lower()
    if mode not in SCOPE_MODES:
        raise ValueError(f"Unknown scope mode: {mode!r}")
    cleaned = []
    for machine in (machines or []):
        name = normalize_machine(machine)
        if name and name not in cleaned:
            cleaned.append(name)
    # An explicit-list group with no machines is legal (a group being built up, or one
    # whose machines were decommissioned). It simply grants access to nothing.
    return mode, sorted(cleaned)


def _validate_members(members):
    cleaned = []
    for member in (members or []):
        email = normalize_email(member)
        if not email:
            continue
        if "@" not in email:
            raise ValueError(f"{member!r} is not an email address.")
        if email not in cleaned:
            cleaned.append(email)
    return sorted(cleaned)


# ================================
# THE CACHE
# ================================
# Every authorized request resolves the caller's effective permissions, and a machine
# list filters per row -- so this sits in the hottest read path in the hub. Writes are
# vanishingly rare (an admin editing a group). Same copy-on-write discipline as
# settings.py, and the same caveat: it is per-process, correct under the single
# waitress process the hub runs as, and would need a version-row poll if the hub ever
# ran multiple workers. Readers take one atomic global read and never mutate what they
# get; writers build a complete new state and rebind in one assignment.

_state = None                    # dict: {"groups": {...}, "by_email": {...}}
_state_lock = threading.Lock()   # serialises writers and cold loads ONLY


def invalidate():
    """Drop the cache so the next read rebuilds from the DB. Called after every write
    here, and exposed for tests that write rows behind this module's back."""
    global _state
    with _state_lock:
        _state = None


def _build(db_path):
    groups = {}
    by_email = {}
    with get_conn(db_path) as conn:
        for row in conn.execute(
            "SELECT id, name, description, capabilities_json, scope_mode, "
            "created_at, updated_at, updated_by FROM permission_groups"
        ):
            try:
                capabilities = json.loads(row["capabilities_json"]) or []
            except (TypeError, ValueError):
                # A corrupt row must fail CLOSED (no capabilities), never open.
                capabilities = []
            groups[row["id"]] = {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                # Drop anything no longer a known capability, so removing one from
                # CAPABILITIES actually revokes it rather than leaving a live string.
                "capabilities": [c for c in capabilities if c in CAPABILITIES],
                "scope_mode": row["scope_mode"] if row["scope_mode"] in SCOPE_MODES else SCOPE_LIST,
                "machines": [],
                "members": [],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
            }
        for row in conn.execute(
            "SELECT group_id, machine FROM permission_group_machines ORDER BY machine"
        ):
            group = groups.get(row["group_id"])
            if group is not None:
                group["machines"].append(row["machine"])
        for row in conn.execute(
            "SELECT group_id, email FROM permission_group_members "
            "WHERE email IS NOT NULL ORDER BY email"
        ):
            group = groups.get(row["group_id"])
            if group is None:
                continue
            group["members"].append(row["email"])
            by_email.setdefault(row["email"], []).append(row["group_id"])
    return {"groups": groups, "by_email": by_email}


def _get_state(db_path):
    global _state
    state = _state
    if state is None:
        with _state_lock:
            if _state is None:
                _state = _build(db_path)
            state = _state
    return state


# ================================
# READS
# ================================
def list_groups(db_path):
    """Every group, newest-named-first by name. Returns copies -- callers (the API
    layer, the UI) mutate what they get, and the cache must never be one of them."""
    state = _get_state(db_path)
    groups = [dict(g, capabilities=list(g["capabilities"]),
                   machines=list(g["machines"]), members=list(g["members"]))
              for g in state["groups"].values()]
    return sorted(groups, key=lambda g: g["name"].lower())


def get_group(db_path, group_id):
    """One group, or None."""
    group = _get_state(db_path)["groups"].get(str(group_id or "").strip())
    if group is None:
        return None
    return dict(group, capabilities=list(group["capabilities"]),
                machines=list(group["machines"]), members=list(group["members"]))


def groups_for_email(db_path, email):
    """The groups this user belongs to, as full group dicts."""
    state = _get_state(db_path)
    ids = state["by_email"].get(normalize_email(email), ())
    return [dict(state["groups"][gid], capabilities=list(state["groups"][gid]["capabilities"]),
                 machines=list(state["groups"][gid]["machines"]),
                 members=list(state["groups"][gid]["members"]))
            for gid in ids if gid in state["groups"]]


def is_superuser(email, superusers):
    """Break-glass: membership in ALLOWED_EMAILS grants everything over everything."""
    return normalize_email(email) in {normalize_email(e) for e in (superusers or ())}


def effective_permissions(db_path, email, superusers=()):
    """What this user may actually do, as one dict:

        {"email", "superuser", "capabilities": set, "machines": set|None, "groups": [...]}

    `machines` is None when the user's scope is EVERY machine (a superuser, or a
    member of any scope_mode="all" group) -- deliberately None rather than a set of
    every hostname, so callers cannot accidentally freeze a snapshot of the fleet and
    then miss a machine that enrolled a second later. Callers must treat None as
    "unrestricted"; machine_in_scope() and visible_machine_filter() do.

    Note the union semantics: capabilities from one group apply to machines from
    another. That is the documented model (effective permission = union across
    groups), and it is what makes "give Ann command rights on the Hospital PCs" a
    matter of adding her to one group rather than editing a matrix. Where that is too
    coarse, the answer is a narrower group, not a per-group intersection -- an
    intersection model makes the effect of adding someone to a group depend on every
    other group they are in, which no one can reason about.
    """
    email = normalize_email(email)
    if is_superuser(email, superusers):
        return {
            "email": email,
            "superuser": True,
            "capabilities": set(CAPABILITIES),
            "machines": None,
            "groups": [],
        }

    capabilities = set()
    machines = set()
    all_machines = False
    groups = groups_for_email(db_path, email)
    for group in groups:
        capabilities.update(group["capabilities"])
        if group["scope_mode"] == SCOPE_ALL:
            all_machines = True
        else:
            machines.update(group["machines"])
    return {
        "email": email,
        "superuser": False,
        "capabilities": capabilities,
        "machines": None if all_machines else machines,
        "groups": groups,
    }


def has_capability(permissions, capability):
    return capability in (permissions or {}).get("capabilities", ())


def machine_in_scope(permissions, machine):
    """Is this one machine inside the caller's scope? None scope means unrestricted."""
    scope = (permissions or {}).get("machines", set())
    if scope is None:
        return True
    return normalize_machine(machine) in scope


def visible_machine_filter(permissions):
    """A predicate for filtering a list of machine names down to the visible ones.
    Returns None when the caller is unrestricted, so hot paths can skip filtering
    entirely rather than running a no-op test per row."""
    scope = (permissions or {}).get("machines", set())
    if scope is None:
        return None
    return lambda machine: normalize_machine(machine) in scope


def members_of_machine(db_path, machine):
    """Every email that can reach `machine` through a group. Excludes superusers --
    they are not in the group tables at all. Used by the admin UI to answer "who has
    access to this box?"."""
    machine = normalize_machine(machine)
    emails = set()
    for group in _get_state(db_path)["groups"].values():
        if group["scope_mode"] == SCOPE_ALL or machine in group["machines"]:
            emails.update(group["members"])
    return sorted(emails)


# ================================
# WRITES
# ================================
# Auditing lives here rather than in the HTTP layer (where settings_web.py puts it)
# because a permission change is the one edit whose record must exist no matter which
# caller made it -- a future CLI, an AD sync, a migration. fleet.audit never raises.

def _replace_machines(conn, group_id, machines):
    conn.execute("DELETE FROM permission_group_machines WHERE group_id = ?", (group_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO permission_group_machines(group_id, machine) VALUES (?, ?)",
        [(group_id, m) for m in machines],
    )


def _replace_members(conn, group_id, members, actor, now):
    conn.execute(
        "DELETE FROM permission_group_members WHERE group_id = ? AND email IS NOT NULL",
        (group_id,),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO permission_group_members(group_id, email, added_at, added_by) "
        "VALUES (?, ?, ?, ?)",
        [(group_id, e, now, actor) for e in members],
    )


def create_group(db_path, name, capabilities=(), machines=(), members=(),
                 scope_mode=SCOPE_LIST, description=None, actor="unknown"):
    """Create a group and return its id. Raises ValueError on invalid input or a
    duplicate name -- everything is validated before anything is written."""
    name = _validate_name(name)
    capabilities = _validate_capabilities(capabilities)
    scope_mode, machines = _validate_scope(scope_mode, machines)
    members = _validate_members(members)
    description = (str(description).strip() or None) if description else None

    group_id = uuid.uuid4().hex
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO permission_groups(id, name, description, capabilities_json, "
                "scope_mode, created_at, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (group_id, name, description, json.dumps(capabilities), scope_mode,
                 now, now, actor),
            )
            _replace_machines(conn, group_id, machines)
            _replace_members(conn, group_id, members, actor, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"A permission group named {name!r} already exists.")
    invalidate()
    fleet.audit(db_path, actor, "permission_group.create", name, {
        "group_id": group_id, "capabilities": capabilities,
        "scope_mode": scope_mode, "machines": machines, "members": members,
    })
    return group_id


def update_group(db_path, group_id, name=None, capabilities=None, machines=None,
                 members=None, scope_mode=None, description=None, actor="unknown"):
    """Patch a group in place. Every argument left as None is left untouched -- pass
    an empty list to actually clear machines or members. Raises KeyError if the group
    is gone, ValueError on invalid input or a duplicate name."""
    group_id = str(group_id or "").strip()
    before = get_group(db_path, group_id)
    if before is None:
        raise KeyError(group_id)

    new_name = _validate_name(name) if name is not None else before["name"]
    new_caps = (_validate_capabilities(capabilities) if capabilities is not None
                else list(before["capabilities"]))
    new_mode, new_machines = _validate_scope(
        before["scope_mode"] if scope_mode is None else scope_mode,
        before["machines"] if machines is None else machines,
    )
    new_members = (_validate_members(members) if members is not None
                   else list(before["members"]))
    if description is None:
        new_description = before["description"]
    else:
        new_description = str(description).strip() or None

    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE permission_groups SET name = ?, description = ?, "
                "capabilities_json = ?, scope_mode = ?, updated_at = ?, updated_by = ? "
                "WHERE id = ?",
                (new_name, new_description, json.dumps(new_caps), new_mode, now,
                 actor, group_id),
            )
            if machines is not None or scope_mode is not None:
                _replace_machines(conn, group_id, new_machines)
            if members is not None:
                _replace_members(conn, group_id, new_members, actor, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"A permission group named {new_name!r} already exists.")
    invalidate()

    after = get_group(db_path, group_id)
    # Record only what actually moved -- a full before/after on every save buries the
    # one edit that mattered under six unchanged fields.
    changes = {}
    for field in ("name", "description", "capabilities", "scope_mode", "machines", "members"):
        if before.get(field) != after.get(field):
            changes[field] = {"from": before.get(field), "to": after.get(field)}
    if changes:
        fleet.audit(db_path, actor, "permission_group.update", after["name"],
                    {"group_id": group_id, "changes": changes})
    return after


def delete_group(db_path, group_id, actor="unknown"):
    """Remove a group and its machine/member rows. Raises KeyError if unknown.

    No "you can't delete the last admin group" guard, deliberately: ALLOWED_EMAILS is
    break-glass and always retains every capability, so there is no lockout to
    prevent. See the module docstring.
    """
    group_id = str(group_id or "").strip()
    before = get_group(db_path, group_id)
    if before is None:
        raise KeyError(group_id)
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM permission_group_machines WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM permission_group_members WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM permission_groups WHERE id = ?", (group_id,))
    invalidate()
    fleet.audit(db_path, actor, "permission_group.delete", before["name"], {
        "group_id": group_id, "capabilities": before["capabilities"],
        "machines": before["machines"], "members": before["members"],
    })
    return True


def known_emails(db_path):
    """Every email that is a member of any group, for the admin UI's picker."""
    return sorted(_get_state(db_path)["by_email"].keys())


def forget_machine(db_path, machine):
    """Drop a machine from every group's scope. Called when a machine is hard-deleted,
    so a hostname later reused by a different box doesn't silently inherit the old
    box's access grants."""
    machine = normalize_machine(machine)
    if not machine:
        return 0
    with get_conn(db_path) as conn:
        removed = conn.execute(
            "DELETE FROM permission_group_machines WHERE machine = ?", (machine,)
        ).rowcount or 0
    if removed:
        invalidate()
    return removed


def rename_machine(db_path, old_machine, new_machine):
    """Re-point group scopes from `old_machine` to `new_machine`. Called on a
    duplicate-serial merge: the survivor is the same physical box, so a group that
    granted access to the old hostname must keep granting it -- otherwise a merge
    silently removes machines from operators' scopes."""
    old_machine = normalize_machine(old_machine)
    new_machine = normalize_machine(new_machine)
    if not old_machine or not new_machine or old_machine == new_machine:
        return 0
    with get_conn(db_path) as conn:
        # OR IGNORE, then DELETE: a group already scoped to both names would otherwise
        # collide on the (group_id, machine) primary key.
        moved = conn.execute(
            "UPDATE OR IGNORE permission_group_machines SET machine = ? WHERE machine = ?",
            (new_machine, old_machine),
        ).rowcount or 0
        conn.execute("DELETE FROM permission_group_machines WHERE machine = ?", (old_machine,))
    invalidate()
    return moved
