"""What is installed on a managed device -- roadmap #23 phase D.

**The inventory exists so that app policy can be written by a human.** Blocking an app means
naming a package, and a package name is not something an operator knows: `com.google.android.youtube`
is guessable, `com.zhiliaoapp.musically` is TikTok. Without a list to pick from, a policy editor
is a text box that punishes typos with silence -- a package that does not exist on the device is
simply not suspended, and nothing anywhere says so.

**Replace-semantics, with the same guard wake.record_network needed.** The app set is REPLACED
on every report, because an app that has been uninstalled must disappear -- a stale row is a
policy target that no longer exists and a compliance answer that is quietly wrong. But a
MALFORMED report replaces nothing: a payload whose `apps` is not a list at all (a truncated
body, a future agent sending a different shape) would otherwise empty a good inventory in one
heartbeat. An empty LIST is a real claim and is stored; an absent or non-list `apps` leaves the
last good reading alone.

**`suspended` and `enabled` are what the DEVICE says, never what the policy asked for.** That
distinction is the whole reason they are stored. `setPackagesSuspended` returns the packages it
could not suspend, and a policy reported as applied while three of its targets are still running
is worse than no policy at all. The console compares intent against this table, so this table
must never be written from intent.

Kept free of Flask so it can be unit-tested in isolation.
"""
import sqlite3
import time

# ================================
# INGEST BOUNDS
# ================================
#: A phone has 150-400 packages. This is a ceiling on a pathological or hostile report, not a
#: limit any real device meets -- and it matches the agent's own cap so the two agree about
#: what "too many" means.
MAX_APPS = 1000
#: Android package names are bounded well below this; labels are arbitrary text chosen by the
#: app's author and are the field worth capping.
MAX_PACKAGE_CHARS = 255
MAX_LABEL_CHARS = 200
MAX_VERSION_CHARS = 64


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_apps_db(db_path):
    """Create the app-inventory table. Idempotent -- safe to call on every hub start next to
    app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_apps (
                machine     TEXT NOT NULL,
                package     TEXT NOT NULL,
                label       TEXT NOT NULL DEFAULT '',
                version     TEXT NOT NULL DEFAULT '',
                is_system   INTEGER NOT NULL DEFAULT 0,
                enabled     INTEGER NOT NULL DEFAULT 1,
                suspended   INTEGER NOT NULL DEFAULT 0,
                reported_at INTEGER NOT NULL,
                PRIMARY KEY (machine, package)
            )
            """
        )
        # "Which machines have this package" is the fleet-wide question a policy author asks
        # before blocking something -- how many devices would this actually affect. It is a
        # scan over the package column, not over the machine.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_apps_package "
                     "ON machine_apps(package)")


# ================================
# INGEST
# ================================
def _clean(value, limit):
    return str(value if value is not None else "").strip()[:limit]


def _clean_app(raw):
    """Normalise one reported package, or None if it is unusable.

    A row with no package name is dropped rather than stored: the package IS the identity here
    and the thing a policy names, so an app nobody can name is one nobody can act on.
    """
    if not isinstance(raw, dict):
        return None
    package = _clean(raw.get("package"), MAX_PACKAGE_CHARS)
    if not package:
        return None
    return {
        "package": package,
        # Falling back to the package name rather than to an empty string, matching the agent:
        # a row with no label at all is one an operator cannot recognise, and the package name
        # beats a blank.
        "label": _clean(raw.get("label"), MAX_LABEL_CHARS) or package,
        "version": _clean(raw.get("version"), MAX_VERSION_CHARS),
        "is_system": 1 if raw.get("system") else 0,
        # Default TRUE, unlike the two beside it. An agent that omits `enabled` is describing an
        # ordinary app; reading silence as "disabled" would grey out every row on a device
        # running an older agent.
        "enabled": 0 if raw.get("enabled") is False else 1,
        "suspended": 1 if raw.get("suspended") else 0,
    }


def record_inventory(db_path, machine, payload, now=None):
    """Store what a device says is installed. Returns how many packages were stored.

    Called from the heartbeat, so the whole path is bounded, type-checked and non-raising: a
    malformed report costs a stale list, never a heartbeat. A heartbeat that 500s takes the
    machine offline fleet-wide, which is a far worse outcome than an out-of-date app list.
    """
    machine = _clean(machine, 200)
    if not machine or not isinstance(payload, dict):
        return 0

    raw_apps = payload.get("apps")
    if not isinstance(raw_apps, list):
        # Not an app report at all. Nothing is written -- see the module docstring on why
        # replace-semantics makes believing this dangerous.
        return 0

    apps, seen = [], set()
    for raw in raw_apps[:MAX_APPS]:
        app = _clean_app(raw)
        if app is not None and app["package"] not in seen:
            seen.add(app["package"])
            apps.append(app)

    if raw_apps and not apps:
        # A non-empty list in which NOTHING was usable is a malformed report, not a device that
        # has lost every app. Believing it would empty a good inventory and leave a policy
        # author with nothing to pick from, with nothing on screen to say why.
        return 0

    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_apps WHERE machine = ?", (machine,))
        conn.executemany(
            "INSERT INTO machine_apps(machine, package, label, version, is_system, enabled, "
            "                         suspended, reported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(machine, a["package"], a["label"], a["version"], a["is_system"], a["enabled"],
              a["suspended"], now) for a in apps])
    return len(apps)


# ================================
# READ
# ================================
def _row(row):
    app = dict(row)
    app.pop("machine", None)
    for flag in ("is_system", "enabled", "suspended"):
        app[flag] = bool(app[flag])
    return app


def list_apps(db_path, machine, include_system=True):
    """This machine's installed packages, user apps first then alphabetically.

    User apps first because that is the order somebody writing a policy reads in: the app they
    are looking for is almost always one somebody installed, and a device's two hundred system
    packages would otherwise bury it.
    """
    clauses = ["machine = ?"]
    params = [str(machine or "").strip()]
    if not include_system:
        clauses.append("is_system = 0")
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM machine_apps WHERE {' AND '.join(clauses)} "
            f"ORDER BY is_system ASC, label COLLATE NOCASE ASC, package ASC", params).fetchall()
    return [_row(r) for r in rows]


def get_inventory(db_path, machine):
    """The machine's apps plus when they were reported.

    A machine that has never reported is `reported_at: None` with an empty list -- deliberately
    distinct from a device that reported having none. The first is every Windows PC in the fleet
    and every Android device on an older agent; the second would be a device with nothing
    installed, which does not happen. Same distinction wake.get_network draws.
    """
    apps = list_apps(db_path, machine)
    return {
        "apps": apps,
        "reported_at": max((a["reported_at"] for a in apps), default=None),
        "counts": {
            "total": len(apps),
            "user": sum(1 for a in apps if not a["is_system"]),
            "suspended": sum(1 for a in apps if a["suspended"]),
            "disabled": sum(1 for a in apps if not a["enabled"]),
        },
    }


def machines_with(db_path, package):
    """Which machines have this package installed.

    The question a policy author asks before blocking something: how many devices would this
    actually affect. Scoping is the caller's job -- this module knows nothing about permission
    groups, the same division fleet.py and wake.py keep.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT machine FROM machine_apps WHERE package = ? ORDER BY machine ASC",
            (str(package or "").strip(),)).fetchall()
    return [r["machine"] for r in rows]


def known_packages(db_path, machines=None):
    """Every distinct package across the fleet, with a label and how many devices have it.

    What a fleet-wide policy editor picks from. The label is one device's answer rather than a
    consensus -- they agree in practice, and picking the alphabetically first is a stable rule
    that needs no tie-break nobody would understand.
    """
    clauses, params = [], []
    if machines is not None:
        scope = [str(m).strip() for m in machines if str(m or "").strip()]
        if not scope:
            return []
        clauses.append(f"machine IN ({','.join('?' for _ in scope)})")
        params.extend(scope)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT package, MIN(label) AS label, MIN(is_system) AS is_system, "
            f"       COUNT(*) AS machines FROM machine_apps {where} "
            f"GROUP BY package ORDER BY is_system ASC, label COLLATE NOCASE ASC", params
        ).fetchall()
    return [{"package": r["package"], "label": r["label"],
             "is_system": bool(r["is_system"]), "machines": r["machines"]} for r in rows]


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's app inventory.

    No exception for history here, unlike patch outcomes: a list of what somebody had installed
    is a fact about that person's device rather than about an update, and it is the second
    payload in this product (after location) where keeping it past the machine is liability
    rather than usefulness.
    """
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_apps WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move an app inventory during a duplicate-serial merge.

    The SURVIVOR wins, exactly as bios.rename_machine does: both rows describe one physical
    device, and the survivor is the one still reporting. A union would claim the device has apps
    that were uninstalled before the merge.
    """
    with get_conn(db_path) as conn:
        existing = conn.execute("SELECT 1 FROM machine_apps WHERE machine = ? LIMIT 1",
                                (new_machine,)).fetchone()
        if existing is not None:
            conn.execute("DELETE FROM machine_apps WHERE machine = ?", (old_machine,))
        else:
            conn.execute("UPDATE machine_apps SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
