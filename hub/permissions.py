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

Member rows carry an `ad_group_dn` column alongside `email`. It was added on day one
so that roadmap #4 would not need a schema migration on a table this security-critical,
and it is now live: a group can name directory groups as well as individual emails, and
membership in a named directory group grants that group exactly as email membership
does. See DIRECTORY_GROUP_CLAIMS below. The hub never queries a directory to establish
this -- it believes what the identity provider signed at sign-in, so a directory grant
is session-scoped where an email grant is stored.
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
# Reading the audit trail is two capabilities, not one. VIEW_AUDIT_LOG is the perimeter --
# it opens the tab and returns info + notice rows. VIEW_SECURITY_AUDIT is a MODIFIER on top
# of it, widening the read to security-level rows (permission changes, backup-key access,
# remote sessions, command execution, settings). On its own it grants nothing at all: a
# group holding only it still gets no tab and a 403 from the API, because the level filter
# is a widening of a read the first capability authorised.
VIEW_AUDIT_LOG = "view_audit_log"
VIEW_SECURITY_AUDIT = "view_security_audit"
ISSUE_COMMANDS = "issue_commands"
REMOTE_CONTROL = "remote_control"
DEPLOY_PACKAGES = "deploy_packages"
MANAGE_BACKUPS = "manage_backups"
MANAGE_SETTINGS = "manage_settings"
MANAGE_USERS = "manage_users"
MANAGE_PERMISSION_GROUPS = "manage_permission_groups"

CAPABILITIES = (
    VIEW,
    VIEW_AUDIT_LOG,
    VIEW_SECURITY_AUDIT,
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
    VIEW_AUDIT_LOG: ("View audit log",
                     "Read the record of who did what on this hub. Security-sensitive "
                     "entries are withheld unless the next capability is held too. "
                     "The audit log is not machine-scoped."),
    VIEW_SECURITY_AUDIT: ("View security audit entries",
                          "Additionally see security-level audit entries: permission "
                          "changes, backup-key access, remote sessions, commands run, "
                          "and settings changes. Does nothing without 'View audit log'."),
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
# DIRECTORY GROUP MAPPING (roadmap #4)
# ================================
# A permission group can name directory groups as well as individual emails. At sign-in
# the issuer tells us which directory groups the user is in; any permission group that
# names one of them applies, exactly as though the user had been added by email.
#
# The claims below are checked in order and their values UNIONED, because "which claim
# carries group membership" is per-issuer and not something an admin should have to know:
# Entra puts security-group object ids in `groups` and app roles in `roles`; Okta,
# Keycloak and Authentik all use `groups`; `wids` carries Entra's built-in directory
# roles (the tenant-wide "Global Administrator" and friends), which is the one token an
# admin is likely to reach for on day one.
DIRECTORY_GROUP_CLAIMS = ("groups", "roles", "wids")

# What a token may look like is deliberately NOT constrained beyond this. Entra sends
# GUIDs, ADFS and on-prem issuers send distinguished names, others send plain names --
# and the whole point of the `ad_group_dn` column being opaque is that it works for all
# three without a mode flag. Comparison is casefolded because every one of those forms is
# case-insensitive at its source (hex GUIDs, LDAP DNs, and group names alike), so a
# mapping typed as `CN=Hospital IT,OU=Groups,DC=x` must match a claim spelling it
# `cn=hospital it,ou=groups,dc=x`.
MAX_DIRECTORY_GROUP_CHARS = 512


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


def email_from_claims(user_info):
    """Pick the email out of an OIDC userinfo / ID-token payload, or "" if there isn't one.

    Lives here rather than in app.py because it is an AUTHORIZATION decision, not a bit of
    sign-in plumbing: whatever this returns is the identity every permission group, every
    audit row and every terminal session is then keyed on. It belongs next to
    normalize_email for the same reason that does -- one place decides who someone is.

    `email` is the standard claim and what Google always sends, but it is not guaranteed.
    Microsoft Entra ID omits it in plenty of tenants and puts the sign-in name in
    `preferred_username` or `upn` instead, so falling back is the difference between "Entra
    works" and "Entra signs in and is then refused for having no email".

    The "@" test is the guard that makes those fallbacks safe. On some issuers
    `preferred_username` is a bare username ("administrator"), and accepting one of those
    as an identity would let it collide with a granted address -- so anything that isn't
    shaped like an address is ignored rather than trusted.
    """
    for claim in ("email", "preferred_username", "upn"):
        value = normalize_email((user_info or {}).get(claim))
        if "@" in value:
            return value
    return ""


def normalize_directory_group(token):
    """Fold a directory-group token to its comparison form. See the note beside
    DIRECTORY_GROUP_CLAIMS for why this is casefold-and-strip and nothing more."""
    return str(token or "").strip().casefold()


def has_group_claim_overage(user_info):
    """Did the issuer decline to send the group list because it was too long?

    Entra stops emitting `groups` once a user is in more than ~200 of them and sends
    `_claim_names`/`_claim_sources` pointing at a Graph endpoint instead. Resolving that
    needs a Graph token this hub does not hold, so we do not pretend to -- but the caller
    must be able to tell "this user is in no mapped group" from "the issuer never told us
    which groups this user is in". Those two look identical downstream and only one of
    them is a configuration error, so an unreported overage is an admin staring at a
    correct mapping that appears to do nothing.
    """
    names = (user_info or {}).get("_claim_names") or {}
    if not isinstance(names, dict):
        return False
    return any(claim in names for claim in DIRECTORY_GROUP_CLAIMS)


def directory_groups_from_claims(user_info):
    """Every directory-group token an issuer asserted, normalised and deduplicated.

    Lives here beside email_from_claims for the same reason that does: this is an
    AUTHORIZATION input, not sign-in plumbing, and one place should decide what the hub
    believes about who someone is. Non-string entries are dropped rather than coerced --
    a claim shaped unexpectedly must grant nothing, not stringify into a token that might
    collide with a real mapping.
    """
    seen = []
    for claim in DIRECTORY_GROUP_CLAIMS:
        values = (user_info or {}).get(claim)
        # A single-valued claim is a bare string on several issuers, a list on the rest.
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            token = normalize_directory_group(value)
            if token and token not in seen:
                seen.append(token)
    return seen


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


def _validate_directory_groups(tokens):
    """Normalise the directory-group tokens a group maps, refusing the shapes that
    would silently grant more than the admin meant.

    The only real hazard here is a token that normalises to empty or to something a
    claim can never carry: stored, it would sit in the UI looking like a live grant
    while matching nothing forever. Length is capped because an LDAP DN is the longest
    legitimate form and 512 characters is well past the longest real one.
    """
    cleaned = []
    for token in (tokens or []):
        if not isinstance(token, str):
            raise ValueError(f"{token!r} is not a directory group identifier.")
        normalized = normalize_directory_group(token)
        if not normalized:
            continue
        if len(normalized) > MAX_DIRECTORY_GROUP_CHARS:
            raise ValueError(
                f"Directory group identifiers are limited to "
                f"{MAX_DIRECTORY_GROUP_CHARS} characters.")
        if normalized not in cleaned:
            cleaned.append(normalized)
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
    by_directory_group = {}
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
                "directory_groups": [],
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
        # Directory-group members (roadmap #4). Rows are written already normalised, but
        # normalise on read too: the column predates this feature by several releases, so
        # a row could have been put there by hand or by an older migration.
        for row in conn.execute(
            "SELECT group_id, ad_group_dn FROM permission_group_members "
            "WHERE ad_group_dn IS NOT NULL ORDER BY ad_group_dn"
        ):
            group = groups.get(row["group_id"])
            if group is None:
                continue
            token = normalize_directory_group(row["ad_group_dn"])
            if not token:
                continue
            group["directory_groups"].append(token)
            by_directory_group.setdefault(token, []).append(row["group_id"])
    return {"groups": groups, "by_email": by_email,
            "by_directory_group": by_directory_group}


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
def _copy_group(group):
    """A caller-owned copy of a cached group. Every list is copied too: callers (the API
    layer, the UI) mutate what they get, and the cache must never be one of them. One
    helper rather than three inline dict() calls, so a new list-valued field cannot be
    deep-copied on one read path and shared on another."""
    return dict(group,
                capabilities=list(group["capabilities"]),
                machines=list(group["machines"]),
                members=list(group["members"]),
                directory_groups=list(group["directory_groups"]))


def list_groups(db_path):
    """Every group, ordered by name. Returns copies."""
    groups = [_copy_group(g) for g in _get_state(db_path)["groups"].values()]
    return sorted(groups, key=lambda g: g["name"].lower())


def get_group(db_path, group_id):
    """One group, or None."""
    group = _get_state(db_path)["groups"].get(str(group_id or "").strip())
    return None if group is None else _copy_group(group)


def groups_for_email(db_path, email):
    """The groups this user belongs to by email, as full group dicts."""
    state = _get_state(db_path)
    ids = state["by_email"].get(normalize_email(email), ())
    return [_copy_group(state["groups"][gid]) for gid in ids if gid in state["groups"]]


def groups_for_directory_groups(db_path, directory_groups):
    """The groups granted by the directory groups an issuer asserted for this session.

    Order is by permission-group name so the result is stable regardless of the order
    the issuer happened to list the user's groups in -- that ordering is not meaningful
    and letting it leak into the audit trail or the UI makes two identical sessions look
    different.
    """
    state = _get_state(db_path)
    ids = []
    for token in (directory_groups or []):
        for gid in state["by_directory_group"].get(normalize_directory_group(token), ()):
            if gid not in ids:
                ids.append(gid)
    found = [_copy_group(state["groups"][gid]) for gid in ids if gid in state["groups"]]
    return sorted(found, key=lambda g: g["name"].lower())


def mapped_directory_groups(db_path):
    """Every directory-group token any permission group maps, as a set.

    This is what lets sign-in store only the INTERSECTION of what the issuer claimed and
    what this hub actually maps. That matters: Flask sessions are signed cookies with a
    ~4 KB budget, and a user in 200 Entra groups carries 7 KB of GUIDs -- enough to break
    the cookie, and therefore the login, for exactly the users who are in the most groups.
    A claimed group nothing maps can never affect authorization, so dropping it at the
    door loses nothing and bounds the session by the admin's own configuration.
    """
    return set(_get_state(db_path)["by_directory_group"].keys())


def is_superuser(email, superusers):
    """Break-glass: membership in ALLOWED_EMAILS grants everything over everything."""
    return normalize_email(email) in {normalize_email(e) for e in (superusers or ())}


def effective_permissions(db_path, email, superusers=(), directory_groups=()):
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

    `directory_groups` are the tokens the identity provider asserted for THIS SESSION
    (roadmap #4). They union in exactly like email membership -- a group reached by
    directory mapping is not a lesser grant, it is the same grant reached another way,
    which is what makes "map the Hospital IT Entra group once" a replacement for
    maintaining an email list by hand. They are a session input rather than a stored
    one because that is what they are: the hub never queries the directory, it believes
    what the issuer signed at sign-in, and that belief expires with the session.
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
    # Deduplicate by id: someone can be both an explicit member and in a mapped
    # directory group, and counting that group twice would double every list it appears
    # in on the "my permissions" page.
    groups = groups_for_email(db_path, email)
    seen_ids = {group["id"] for group in groups}
    for group in groups_for_directory_groups(db_path, directory_groups):
        if group["id"] not in seen_ids:
            seen_ids.add(group["id"])
            groups.append(group)
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
    access to this box?".

    Also excludes anyone who reaches it through a mapped DIRECTORY group, and cannot do
    otherwise: the hub never queries the directory, so it has no way to enumerate that
    group's members -- it only ever learns that one person is in it, at their sign-in.
    Read this as "who has access by name", not as the complete list.
    """
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


def _replace_directory_groups(conn, group_id, tokens, actor, now):
    """The ad_group_dn half of the member table. Scoped to `ad_group_dn IS NOT NULL` so
    saving a group's directory mappings never touches its email members, and vice versa
    -- the two halves share a table but are edited by different fields of the form."""
    conn.execute(
        "DELETE FROM permission_group_members WHERE group_id = ? AND ad_group_dn IS NOT NULL",
        (group_id,),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO permission_group_members"
        "(group_id, ad_group_dn, added_at, added_by) VALUES (?, ?, ?, ?)",
        [(group_id, t, now, actor) for t in tokens],
    )


def create_group(db_path, name, capabilities=(), machines=(), members=(),
                 scope_mode=SCOPE_LIST, description=None, directory_groups=(),
                 actor="unknown"):
    """Create a group and return its id. Raises ValueError on invalid input or a
    duplicate name -- everything is validated before anything is written."""
    name = _validate_name(name)
    capabilities = _validate_capabilities(capabilities)
    scope_mode, machines = _validate_scope(scope_mode, machines)
    members = _validate_members(members)
    directory_groups = _validate_directory_groups(directory_groups)
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
            _replace_directory_groups(conn, group_id, directory_groups, actor, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"A permission group named {name!r} already exists.")
    invalidate()
    fleet.audit(db_path, actor, "permission_group.create", name, {
        "group_id": group_id, "capabilities": capabilities,
        "scope_mode": scope_mode, "machines": machines, "members": members,
        "directory_groups": directory_groups,
    }, level=fleet.LEVEL_SECURITY)
    return group_id


def update_group(db_path, group_id, name=None, capabilities=None, machines=None,
                 members=None, scope_mode=None, description=None,
                 directory_groups=None, actor="unknown"):
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
    new_directory_groups = (_validate_directory_groups(directory_groups)
                            if directory_groups is not None
                            else list(before["directory_groups"]))
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
            if directory_groups is not None:
                _replace_directory_groups(conn, group_id, new_directory_groups,
                                          actor, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"A permission group named {new_name!r} already exists.")
    invalidate()

    after = get_group(db_path, group_id)
    # Record only what actually moved -- a full before/after on every save buries the
    # one edit that mattered under six unchanged fields.
    changes = {}
    for field in ("name", "description", "capabilities", "scope_mode", "machines",
                  "members", "directory_groups"):
        if before.get(field) != after.get(field):
            changes[field] = {"from": before.get(field), "to": after.get(field)}
    if changes:
        fleet.audit(db_path, actor, "permission_group.update", after["name"],
                    {"group_id": group_id, "changes": changes},
                    level=fleet.LEVEL_SECURITY)
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
        "directory_groups": before["directory_groups"],
    }, level=fleet.LEVEL_SECURITY)
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
