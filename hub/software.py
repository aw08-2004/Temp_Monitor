"""Installed software on a Windows PC -- roadmap #25 phase B.

The first column of every inventory product a helpdesk has used (a Kaspersky export, a
LOGINventory sheet, a PDQ report), and the one this hub could not fill: `machine_apps` is the
Android inventory, and `winget upgrade` in the patch scanner names only what is UPGRADABLE, so
a machine with a fully patched Acrobat 2017 was invisible to it. This is what answers a licence
audit, "who still has the old VPN client", and "what did that user install".

**The silent failure this exists to prevent is a Software section that reads as "nothing
installed" when it means "nobody has told us".** Every Windows PC in the field is on an agent
older than this feature on the day the hub ships it (deploy order is hub first, VERSIONING.md),
and the report sheet renders this table. So "never reported" is stored as the ABSENCE of a
`machine_software_state` row, and "reported an empty list" is a state row with a zero count --
two different answers, and the sheet words them differently. apps.py infers `reported_at` from
its rows, which is fine on Android where no device has zero packages; a Windows machine whose
uninstall keys all failed to read is a real, if rare, report, and inferring from rows would
make it indistinguishable from an agent that has never spoken.

**Replace-semantics, with the guard apps.record_inventory and wake.record_network needed.** A
program that was uninstalled must disappear, so the set is replaced on every report. A
MALFORMED report replaces nothing: a payload whose `software` is not a list, or a non-empty
list in which not one entry was usable, is a truncated body or a future agent's different
shape, and believing it would empty a good inventory in one heartbeat.

**Every text field here is attacker-influenced.** A program's DisplayName is whatever its
installer wrote, and a local user can write their own per-user uninstall key. So everything is
capped, nothing is interpreted, and the CSV export in reports_web neutralises formula-looking
cells -- a software name is the likeliest place in the whole hub for `=HYPERLINK(...)` to turn
up.

Rejected, and recorded in ROADMAP.MD #25: `Win32_Product` (it validates every MSI on
enumeration, which triggers repairs on the machine being inventoried), a history table of
versions (an inventory is current state; what changed is patch management's and the audit
log's), and one wide `machine_inventory` table.

Kept free of Flask so it can be unit-tested standalone; software_web.py wires HTTP on top.
"""
import sqlite3
import time

# ================================
# INGEST BOUNDS
# ================================
#: A busy developer workstation carries 300-600 uninstall entries once the per-user ones are
#: counted. This is a ceiling on a pathological or hostile report, and it matches the agent's
#: own cap so the two agree about what "too many" means.
MAX_ENTRIES = 2000
MAX_ID_CHARS = 300
MAX_NAME_CHARS = 256
MAX_VERSION_CHARS = 64
MAX_PUBLISHER_CHARS = 200
MAX_PATH_CHARS = 400
MAX_ERROR_CHARS = 500
#: Only a SID lands here -- never a resolved account name, which the agent does not look up
#: (a lookup is a domain round trip per profile on the inventory loop).
MAX_USER_CHARS = 200

#: Where an entry was registered. `machine` is HKLM (either view) -- installed for everybody.
#: `user` is a loaded HKEY_USERS hive -- installed by or for one person, and invisible to every
#: other account on the PC. The distinction is what "what did that user install" asks.
SCOPE_MACHINE = "machine"
SCOPE_USER = "user"
SCOPES = frozenset({SCOPE_MACHINE, SCOPE_USER})

#: Which registry view an HKLM entry came from. Reported because "the 32-bit Office" and "the
#: 64-bit Office" are different licences and different upgrade paths, and the name alone often
#: does not say which.
ARCHES = frozenset({"x64", "x86", ""})


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_software_db(db_path):
    """Create the installed-software tables. Idempotent -- safe to call on every hub start
    next to app.init_db()."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_software (
                machine           TEXT NOT NULL,
                id                TEXT NOT NULL,
                name              TEXT NOT NULL,
                version           TEXT NOT NULL DEFAULT '',
                publisher         TEXT NOT NULL DEFAULT '',
                install_date      TEXT NOT NULL DEFAULT '',
                install_location  TEXT NOT NULL DEFAULT '',
                uninstall_string  TEXT NOT NULL DEFAULT '',
                scope             TEXT NOT NULL DEFAULT 'machine',
                user_sid          TEXT NOT NULL DEFAULT '',
                arch              TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (machine, id)
            )
            """
        )
        # "Which machines have this" is the fleet-wide question -- a licence count, a hunt for
        # the old VPN client -- and it is a scan over the name, not over the machine.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_software_name "
                     "ON machine_software(name)")
        # One row per machine that has EVER reported, including a report of nothing. Its
        # absence is the "waiting for a newer agent" state -- see the module docstring for why
        # that cannot be inferred from machine_software's rows.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_software_state (
                machine      TEXT PRIMARY KEY,
                error        TEXT NOT NULL DEFAULT '',
                count        INTEGER NOT NULL DEFAULT 0,
                reported_at  INTEGER NOT NULL
            )
            """
        )


# ================================
# INGEST
# ================================
def _clean(value, limit):
    return str(value if value is not None else "").strip()[:limit]


def _clean_entry(raw):
    """Normalise one reported program, or None if it is unusable.

    An entry with no id or no name is dropped rather than stored under a placeholder: the id is
    the row's key, and a nameless entry is exactly what Programs and Features itself hides (the
    agent already filters those, so one arriving here is a malformed report).
    """
    if not isinstance(raw, dict):
        return None
    entry_id = _clean(raw.get("id"), MAX_ID_CHARS)
    name = _clean(raw.get("name"), MAX_NAME_CHARS)
    if not entry_id or not name:
        return None
    scope = _clean(raw.get("scope"), 16).lower()
    if scope not in SCOPES:
        # Unknown reads as `machine`, the scope that claims least about a person. Filing an
        # unrecognised value under `user` would attribute an install to somebody on no basis.
        scope = SCOPE_MACHINE
    arch = _clean(raw.get("arch"), 8).lower()
    if arch not in ARCHES:
        arch = ""
    return {
        "id": entry_id,
        "name": name,
        "version": _clean(raw.get("version"), MAX_VERSION_CHARS),
        "publisher": _clean(raw.get("publisher"), MAX_PUBLISHER_CHARS),
        # Kept as the text the installer wrote (usually yyyymmdd, sometimes not). Parsing it
        # would mean deciding what "3/4/2021" means on a machine whose locale we do not know.
        "install_date": _clean(raw.get("install_date"), 32),
        "install_location": _clean(raw.get("install_location"), MAX_PATH_CHARS),
        "uninstall_string": _clean(raw.get("uninstall_string"), MAX_PATH_CHARS),
        "scope": scope,
        "user_sid": _clean(raw.get("user_sid"), MAX_USER_CHARS) if scope == SCOPE_USER else "",
        "arch": arch,
    }


def record_inventory(db_path, machine, payload, now=None):
    """Store what a PC says is installed. Returns how many entries were stored, or None when
    nothing was written at all.

    Called from the heartbeat, so the whole path is bounded, type-checked and non-raising on
    payload shape: a malformed report costs a stale list, never a heartbeat.
    """
    machine = _clean(machine, 200)
    if not machine or not isinstance(payload, dict):
        return None

    raw = payload.get("software")
    if not isinstance(raw, list):
        # Not a software report at all. Nothing is written -- see the module docstring on why
        # replace-semantics makes believing this dangerous.
        return None

    entries, seen = [], set()
    for item in raw[:MAX_ENTRIES]:
        entry = _clean_entry(item)
        if entry is not None and entry["id"] not in seen:
            seen.add(entry["id"])
            entries.append(entry)

    if raw and not entries:
        # A non-empty list in which NOTHING was usable is a malformed report, not a PC that has
        # lost every program.
        return None

    now = int(time.time()) if now is None else int(now)
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_software WHERE machine = ?", (machine,))
        conn.executemany(
            "INSERT INTO machine_software(machine, id, name, version, publisher, install_date, "
            "  install_location, uninstall_string, scope, user_sid, arch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(machine, e["id"], e["name"], e["version"], e["publisher"], e["install_date"],
              e["install_location"], e["uninstall_string"], e["scope"], e["user_sid"],
              e["arch"]) for e in entries])
        conn.execute(
            "INSERT INTO machine_software_state(machine, error, count, reported_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(machine) DO UPDATE SET error = excluded.error, "
            "count = excluded.count, reported_at = excluded.reported_at",
            (machine, _clean(payload.get("error"), MAX_ERROR_CHARS), len(entries), now))
    return len(entries)


# ================================
# READ
# ================================
def list_software(db_path, machine):
    """One machine's installed programs, alphabetically, machine-wide before per-user."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, name, version, publisher, install_date, install_location, "
            "       uninstall_string, scope, user_sid, arch FROM machine_software "
            "WHERE machine = ? ORDER BY name COLLATE NOCASE ASC, version ASC, scope ASC",
            (str(machine or "").strip(),)).fetchall()
    return [dict(r) for r in rows]


def get_inventory(db_path, machine):
    """The machine's programs plus when they were reported.

    **`reported_at: None` means no agent has ever sent this** -- every PC on an agent older than
    #25 B, every Linux and Android agent, and a machine that has not checked in since the hub
    was upgraded. It is deliberately a different answer from `reported_at: <time>, software: []`
    and the console words them differently; see the module docstring.
    """
    machine = str(machine or "").strip()
    with get_conn(db_path) as conn:
        state = conn.execute(
            "SELECT error, count, reported_at FROM machine_software_state WHERE machine = ?",
            (machine,)).fetchone()
    if state is None:
        return {"reported_at": None, "error": "", "software": [], "count": 0}
    return {
        "reported_at": state["reported_at"],
        "error": state["error"],
        "software": list_software(db_path, machine),
        "count": state["count"],
    }


def reported_at_many(db_path, machines):
    """{machine: reported_at} for the machines that have reported at all. One query, for the
    fleet report, which would otherwise ask once per row."""
    names = [str(m).strip() for m in machines if str(m or "").strip()]
    if not names:
        return {}
    out = {}
    with get_conn(db_path) as conn:
        # Chunked under SQLite's default host-parameter limit.
        for start in range(0, len(names), 500):
            chunk = names[start:start + 500]
            for r in conn.execute(
                    f"SELECT machine, reported_at FROM machine_software_state "
                    f"WHERE machine IN ({','.join('?' for _ in chunk)})", chunk):
                out[r["machine"]] = r["reported_at"]
    return out


def catalog(db_path, machines=None, query=""):
    """The fleet software catalog: one row per product and version, with how many machines
    have it. The view a licence question is answered from.

    Grouped on (name, version, publisher) rather than name alone because the question is almost
    always about a version -- "who is still on 2017" -- and a name-only roll-up would answer it
    with one number that hides the split. `machines` narrows to a scope; None means the whole
    fleet, and an EMPTY list means nothing (a caller whose scope is empty sees an empty
    catalog, never the fleet). Scoping is the caller's job, as in apps.known_packages.
    """
    clauses, params = [], []
    if machines is not None:
        scope = [str(m).strip() for m in machines if str(m or "").strip()]
        if not scope:
            return []
        clauses.append(f"machine IN ({','.join('?' for _ in scope)})")
        params.extend(scope)
    query = str(query or "").strip()
    if query:
        clauses.append("(name LIKE ? ESCAPE '\\' OR publisher LIKE ? ESCAPE '\\')")
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend([like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT name, version, publisher, COUNT(DISTINCT machine) AS machines "
            f"FROM machine_software {where} GROUP BY name, version, publisher "
            f"ORDER BY name COLLATE NOCASE ASC, version ASC", params).fetchall()
    return [dict(r) for r in rows]


def machines_with(db_path, name, version=None):
    """Which machines have this program (optionally at this exact version). Unscoped -- the
    caller narrows, the same division fleet.py and apps.py keep."""
    clauses, params = ["name = ?"], [str(name or "").strip()]
    if version is not None:
        clauses.append("version = ?")
        params.append(str(version).strip())
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT machine FROM machine_software WHERE {' AND '.join(clauses)} "
            f"ORDER BY machine ASC", params).fetchall()
    return [r["machine"] for r in rows]


# ================================
# SHADOW AI (roadmap #19)
# ================================
def genai_matches(db_path, patterns, machines=None):
    """Installed programs and Android apps whose name matches a watch-list fragment.

    **A query over what the inventories already hold, and nothing else** -- the #19 entry's own
    constraint: no new channel, no agent change. Windows rows come from machine_software, Android
    rows from machine_apps (label and package both, because `com.openai.chatgpt` is how a
    policy author knows the app and "ChatGPT" is how everybody else does).

    Matched case-insensitively as a substring, in Python rather than SQL LIKE: the list is a
    dozen fragments and an operator's fragment may contain `%` or `_`, which LIKE would read as
    wildcards and quietly match everything.

    `machines` narrows to a scope; None is the whole fleet and an EMPTY list is nothing, the
    same contract as catalog(). Returns one row per (machine, program), naming the first
    fragment that matched -- enough to tell an operator which list entry to remove if the
    finding is a tool they have sanctioned.
    """
    needles = [str(p).strip().lower() for p in (patterns or []) if str(p or "").strip()]
    if not needles:
        return []
    scope = None
    if machines is not None:
        scope = {str(m).strip() for m in machines if str(m or "").strip()}
        if not scope:
            return []

    def first_match(*texts):
        hay = " ".join(t.lower() for t in texts if t)
        return next((n for n in needles if n in hay), None)

    found = []
    with get_conn(db_path) as conn:
        for r in conn.execute("SELECT machine, name, version, publisher, scope, user_sid "
                              "FROM machine_software"):
            if scope is not None and r["machine"] not in scope:
                continue
            hit = first_match(r["name"])
            if hit:
                found.append({"machine": r["machine"], "platform": "windows", "name": r["name"],
                              "version": r["version"], "publisher": r["publisher"],
                              "scope": r["scope"], "matched": hit})
        try:
            android = conn.execute(
                "SELECT machine, package, label, version FROM machine_apps").fetchall()
        except sqlite3.OperationalError:
            # A hub where apps.init_apps_db has not run (a test DB, a mid-upgrade start) has
            # no Android inventory to search, which is not an error.
            android = []
        for r in android:
            if scope is not None and r["machine"] not in scope:
                continue
            hit = first_match(r["label"], r["package"])
            if hit:
                found.append({"machine": r["machine"], "platform": "android",
                              "name": r["label"] or r["package"], "version": r["version"],
                              "publisher": r["package"], "scope": SCOPE_MACHINE, "matched": hit})
    found.sort(key=lambda f: (f["machine"].casefold(), f["name"].casefold()))
    return found


# ================================
# LIFECYCLE HOOKS
# ================================
def forget_machine(db_path, machine):
    """Drop a deleted machine's software inventory, state row included. What somebody had
    installed is a fact about their PC, and keeping it past the machine is liability."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_software WHERE machine = ?", (machine,))
        conn.execute("DELETE FROM machine_software_state WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Move an inventory during a duplicate-serial merge.

    The SURVIVOR wins, exactly as apps.rename_machine and bios.rename_machine do: both rows
    describe one physical PC, and the survivor is the one still reporting. A union would claim
    the PC has programs that were uninstalled before the merge.
    """
    with get_conn(db_path) as conn:
        survivor = conn.execute(
            "SELECT 1 FROM machine_software_state WHERE machine = ?", (new_machine,)).fetchone()
        if survivor is not None:
            conn.execute("DELETE FROM machine_software WHERE machine = ?", (old_machine,))
            conn.execute("DELETE FROM machine_software_state WHERE machine = ?", (old_machine,))
        else:
            conn.execute("DELETE FROM machine_software WHERE machine = ?", (new_machine,))
            conn.execute("UPDATE machine_software SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
            conn.execute("UPDATE machine_software_state SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
