"""Device groups -- saved, named target filters (hub 1.114.0).

The last open item of the console redesign (ROADMAP note at the top of ROADMAP.MD). Aiming a
rule, a package deployment or a bulk action at "the Sales laptops" meant typing the same OU,
or the same list of forty names, every single time -- and the forty-name list was out of date
the week somebody's laptop was replaced.

**A group is a stored rules target, not a new vocabulary.** It is exactly the include/exclude
spec rules.py already validates and resolves: named PCs, an AD OU, a custom field value, or
every PC. Reusing it is what makes the security story short. rules_web checks a target against
its author's scope at save time and rules.scoped_targets narrows it again at evaluation; a
rule aimed at a group goes through both unchanged, because to them a group selector is just
another way of naming machines. A second selector grammar would have needed a second copy of
both checks, and the gap between two copies is where a scope escalation would live.

**Resolved when used, never stored as a member list.** A group that snapshotted its members
would be the forty-name list again. The cost is that editing a group changes what everything
aimed at it reaches -- which is why writing one is its own capability (permissions.py) and why
every save is audited.

**A group cannot contain a group.** Nesting buys little ("Sales OR Finance" is two include
selectors already) and costs cycle detection plus a resolver that recurses on every rule tick.
Refused at validation, so no stored group can ever reference another.

**Deleting a group a rule aims at is refused**, naming the rules, exactly as deleting a script
a rule runs is (rules.rules_using_script). A rule whose group vanished would silently target
nothing, which reads as a rule that works and never matches. Deployments are not a reason to
refuse: a deployment copies its machine list when it is created.

Authorization lives upstream in device_groups_web.py -- VIEW to read and use,
MANAGE_DEVICE_GROUPS to write, and the writer's scope must cover every machine a group
resolves to when it is saved. Nothing here checks a session. Kept free of Flask so it can be
unit-tested in isolation.
"""
import json
import sqlite3
import time

import rules

NAME_MAX = 80
DESCRIPTION_MAX = 500


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_device_groups_db(db_path):
    """Create the table if absent. Idempotent, like every other init_*_db."""
    with get_conn(db_path) as conn:
        # COLLATE NOCASE on the UNIQUE name: "Sales laptops" and "sales laptops" appearing as
        # two entries in a dropdown is how a rule ends up aimed at the stale one.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_groups (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL COLLATE NOCASE UNIQUE,
                description  TEXT NOT NULL DEFAULT '',
                target_json  TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL,
                updated_by   TEXT
            )
            """
        )


def _decode(row):
    group = dict(row)
    try:
        group["target"] = json.loads(group.pop("target_json"))
    except (TypeError, ValueError):
        # A corrupt row must fail CLOSED: an empty include resolves to no machines, never to
        # every machine.
        group["target"] = {"include": [], "exclude": []}
    return group


def list_groups(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM device_groups ORDER BY name COLLATE NOCASE, id").fetchall()
    return [_decode(r) for r in rows]


def get_group(db_path, group_id):
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        return None
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM device_groups WHERE id = ?", (group_id,)).fetchone()
    return _decode(row) if row else None


def _contains_group_selector(target):
    return any(s.get("kind") == rules.TARGET_GROUP
               for side in ("include", "exclude") for s in (target or {}).get(side) or [])


def validate(payload, extra=None):
    """Check a group payload. Returns (error, clean) -- the error is English, like every other
    model-layer refusal here; the web layer passes it through."""
    if not isinstance(payload, dict):
        return "a group must be an object", None
    name = str(payload.get("name") or "").strip()
    if not name:
        return "a group needs a name", None
    if len(name) > NAME_MAX:
        return f"the name is too long (limit {NAME_MAX})", None
    description = str(payload.get("description") or "").strip()
    if len(description) > DESCRIPTION_MAX:
        return f"the description is too long (limit {DESCRIPTION_MAX})", None
    error, target = rules.validate_target(payload.get("target"), extra)
    if error:
        return error, None
    if _contains_group_selector(target):
        return "a device group cannot include or exclude another device group", None
    return None, {"name": name, "description": description, "target": target}


def save_group(db_path, payload, group_id=None, actor=None, extra=None):
    """Create (group_id None) or replace a group. Returns (error, group)."""
    error, clean = validate(payload, extra)
    if error:
        return error, None
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            if group_id is None:
                cur = conn.execute(
                    "INSERT INTO device_groups (name, description, target_json, created_at,"
                    " updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                    (clean["name"], clean["description"], json.dumps(clean["target"]),
                     now, now, actor))
                group_id = cur.lastrowid
            else:
                cur = conn.execute(
                    "UPDATE device_groups SET name = ?, description = ?, target_json = ?,"
                    " updated_at = ?, updated_by = ? WHERE id = ?",
                    (clean["name"], clean["description"], json.dumps(clean["target"]),
                     now, actor, int(group_id)))
                if cur.rowcount == 0:
                    return "no such group", None
    except sqlite3.IntegrityError:
        return "a device group with that name already exists", None
    return None, get_group(db_path, group_id)


def delete_group(db_path, group_id, in_use=None):
    """Delete a group unless `in_use` (the rules aiming at it) is non-empty.

    Returns (error, deleted_group). The caller supplies `in_use` from rules.rules_using_group so
    this stays a single query and the refusal can name the rules.
    """
    group = get_group(db_path, group_id)
    if group is None:
        return "no such group", None
    if in_use:
        names = ", ".join(r["name"] for r in in_use[:5])
        more = f" (and {len(in_use) - 5} more)" if len(in_use) > 5 else ""
        return f"{group['name']} is used by these rules: {names}{more}", None
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM device_groups WHERE id = ?", (group["id"],))
    return None, group


def resolve(db_path, group, machines=None):
    """The machine names a group addresses right now, sorted. NOT scope-filtered -- callers
    narrow to their caller's scope, as they do for any rules target."""
    return rules.resolve_targets(db_path, group["target"], machines)


def member_sets(db_path, group_ids, machines=None):
    """{group_id: {lowercased machine names}} for the ids asked about.

    Used by rules.resolve_targets to answer a `group` selector. A group that no longer exists
    maps to an empty set, so a stale selector matches nothing rather than raising inside a rule
    tick. Resolved over the SAME machine rows the caller is resolving, so a rule preview and the
    group it aims at cannot disagree about which PCs exist.
    """
    wanted = {int(g) for g in group_ids if str(g).isdigit()}
    if not wanted:
        return {}
    by_id = {g["id"]: g for g in list_groups(db_path) if g["id"] in wanted}
    out = {}
    for group_id in wanted:
        group = by_id.get(group_id)
        out[group_id] = ({m.lower() for m in resolve(db_path, group, machines)}
                         if group else set())
    return out
