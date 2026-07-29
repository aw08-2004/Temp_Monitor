"""On-prem Active Directory sync -- computer objects, OUs and ownership (roadmap #4).

Sign-in and group mapping (permissions.py) come from the identity provider over OIDC.
This module is the OTHER half: the hub binds to a domain controller over LDAPS with a
read-only service account and reads **computer objects**, so a machine record gains the
things only AD knows -- its distinguished name, the OU it lives in, its object GUID, and
who manages it.

That in turn is what makes `scope_mode = "ad_ou"` possible: a permission group scoped to
an OU, rather than to a hand-maintained list of hostnames that goes stale the moment
somebody re-images a PC.

**Entirely opt-in.** With `directory.enabled` off -- the default -- nothing here runs, no
LDAP library is imported, and the extra machine_info columns simply stay NULL. A hub with
no AD is not a degraded hub; it is the ordinary case.

Structure, and the reason for it:

  * `fetch_computers()` is the ONLY function that talks to a network. It is thin on
    purpose.
  * `reconcile()` is pure: (what AD returned, what the DB holds) -> the writes to make
    and the machines AD did not know about. Everything with a decision in it lives here,
    so the interesting cases -- a machine that vanished from AD, two AD objects claiming
    one hostname, an OU rename moving fifty machines at once -- are unit-testable against
    literals, with no directory anywhere.

`sync_once()` is the composition of the two plus the DB write, and is what the scheduler
calls.

Kept free of Flask, like fleet.py and permissions.py; directory_web.py wires the HTTP
surface on top.

**ldap3 is imported lazily**, inside the connect path. It is the one dependency the hub
takes purely for this feature, and a hub that never turns AD on must neither require it
to be installed nor pay its import cost. A missing library with the feature ON is
reported as a configuration error, not a crash loop -- see LDAP_IMPORT_HINT.
"""
import os
import re
import sqlite3
import time

import alerts
import settings as settings_module

# The bind password never goes in the settings table -- it is a credential, and the
# settings table is readable by anyone holding `manage_settings` and is dumped into the
# hub-database backup. Same rule backups.py applies to destination credentials.
BIND_PASSWORD_ENV = "DIRECTORY_BIND_PASSWORD"

def bind_password():
    """The service-account password, from the environment. One accessor so there is
    exactly one place that knows where this credential comes from."""
    return os.environ.get(BIND_PASSWORD_ENV) or ""


def config_from_settings(db_path):
    """Assemble a sync config from the settings table plus the .env credential.

    Lives here, next to the code that consumes each key, so the scheduler and the
    "sync now" button cannot drift into passing different configurations -- which would
    make a manual test pass while the scheduled pass kept failing.
    """
    get = settings_module.get
    return {
        "server": get(db_path, "directory.server"),
        "base_dn": get(db_path, "directory.base_dn"),
        "bind_dn": get(db_path, "directory.bind_dn"),
        "password": bind_password(),
        "computer_filter": get(db_path, "directory.computer_filter"),
        "page_size": settings_module.get_int(db_path, "directory.page_size"),
        "timeout_seconds": settings_module.get_int(db_path, "directory.timeout_seconds"),
        "alert_on_unmatched": settings_module.get_bool(
            db_path, "directory.alert_on_unmatched"),
        "tls_verify": settings_module.get_bool(db_path, "directory.tls_verify"),
        "allow_insecure": settings_module.get_bool(db_path, "directory.allow_insecure"),
    }


def ldap3_installed():
    try:
        import ldap3  # noqa: F401
        return True
    except ImportError:
        return False


LDAP_IMPORT_HINT = (
    "Active Directory sync needs the 'ldap3' package, which is not installed on this "
    "hub. Install it with:  pip install ldap3   (or  pip install -r hub/requirements.txt) "
    "and restart the hub. Until then AD sync stays off; nothing else is affected."
)

# AD attributes we ask for. Deliberately a short, explicit list rather than "*": a
# computer object carries a hundred attributes, most of them large, and pulling them all
# for every machine on every sync is the difference between a quick pass and one that
# strains the DC an admin let us bind to.
COMPUTER_ATTRIBUTES = (
    "distinguishedName",
    "dNSHostName",
    "name",
    "objectGUID",
    "operatingSystem",
    "managedBy",
    "lastLogonTimestamp",
    "userAccountControl",
    "whenChanged",
)

# Bit 2 of userAccountControl. A disabled computer account is still a real object in the
# OU, so it must not be mistaken for "no AD match" -- but an operator should be able to
# see that the machine still reporting telemetry has had its account disabled, which is
# usually somebody half-decommissioning it.
UAC_ACCOUNTDISABLE = 0x0002


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_directory_db(db_path):
    """Add the AD columns to machine_info and create the sync-run log. Idempotent.

    The columns live on machine_info rather than in a table of their own because they are
    per-machine facts exactly like asset_tag and model, and every consumer (the machine
    page, Inventory, the OU scope resolver) already reads that row. A join table would buy
    nothing and cost a LEFT JOIN in the hub's hottest read path.
    """
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(machine_info)")}
        # Same ALTER-if-missing pattern app.init_db() uses. All nullable: a hub that never
        # enables AD, and a machine AD has never heard of, both read back NULL throughout.
        for column, decl in (
            ("ad_dn", "TEXT"),                # full distinguished name
            ("ad_ou", "TEXT"),                # the OU part of the DN -- what ad_ou scoping matches
            ("ad_object_guid", "TEXT"),       # stable across rename/move; the real identity
            ("ad_owner", "TEXT"),             # managedBy, as a display name
            ("ad_os", "TEXT"),                # operatingSystem, as AD records it
            ("ad_disabled", "INTEGER"),       # 1 when the computer ACCOUNT is disabled
            ("ad_last_logon", "INTEGER"),     # lastLogonTimestamp, epoch seconds
            ("ad_synced_at", "INTEGER"),      # when we last matched this row to AD
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE machine_info ADD COLUMN {column} {decl}")
        # Scoping resolves machines by OU on every permissions-cache build, so this index
        # is what keeps that from being a table scan per rebuild.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_info_ad_ou ON machine_info(ad_ou)")
        # One row per sync attempt, successful or not. The failures are the point: an AD
        # sync that silently stopped working looks exactly like an AD nobody changed, and
        # the console needs to be able to say "last succeeded 9 days ago".
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS directory_sync_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at    INTEGER NOT NULL,
                finished_at   INTEGER,
                status        TEXT NOT NULL,     -- running | succeeded | failed
                objects_found INTEGER,           -- computer objects AD returned
                matched       INTEGER,           -- machine_info rows joined to one
                unmatched     INTEGER,           -- known machines AD did not have
                error         TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_directory_runs_started "
                     "ON directory_sync_runs(started_at)")


# ================================
# DN / OU PARSING  (pure)
# ================================
# RFC 4514 escaping: a comma inside a name is written "\," and must not be read as a
# component separator. "OU=Sales\, EMEA,DC=corp" is TWO components, not three -- get this
# wrong and every machine in that OU silently scopes to the wrong place.
_DN_SPLIT = re.compile(r'(?<!\\),')


def split_dn(dn):
    """Split a distinguished name into its components, honouring backslash escapes."""
    if not dn:
        return []
    return [part.strip() for part in _DN_SPLIT.split(str(dn)) if part.strip()]


def normalize_dn(dn):
    """Fold a DN for comparison. AD treats DNs case-insensitively and is inconsistent
    about the space after each comma, so two spellings of one OU must compare equal --
    otherwise an OU pasted from one AD tool scopes to nothing when it was copied from
    another."""
    return ",".join(part.strip().casefold() for part in split_dn(dn))


def ou_of(dn):
    """The container a computer object lives in: its DN minus the leaf CN.

    Returned in its ORIGINAL case, because this is also what the console displays --
    normalisation is for comparison only, and showing an admin `ou=clinical,dc=corp`
    when their AD says `OU=Clinical,DC=corp` reads like a different OU.
    """
    parts = split_dn(dn)
    if len(parts) < 2:
        return None
    return ",".join(parts[1:])


def ou_contains(scope_ou, machine_ou):
    """Is `machine_ou` inside `scope_ou` -- the same OU, or nested beneath it?

    Nesting counts, deliberately. An OU tree exists to be nested, so a group scoped to
    `OU=Clinical` that did NOT cover `OU=Ward 3,OU=Clinical` would be a trap: the admin
    sees the parent OU selected and every machine in it apparently in scope, while the
    ones actually filed in sub-OUs are silently excluded.

    Suffix matching is done COMPONENT-WISE rather than as a string endswith, which would
    make `OU=Clinical,DC=x` match `OU=NotClinical,DC=x`.
    """
    scope = split_dn(normalize_dn(scope_ou))
    machine = split_dn(normalize_dn(machine_ou))
    if not scope or not machine or len(scope) > len(machine):
        return False
    return machine[len(machine) - len(scope):] == scope


def cn_of(dn):
    """The leaf CN value of a DN, unescaped enough to display. Used for `managedBy`,
    which AD stores as a full DN -- showing an operator
    `CN=Dana Ruiz,OU=Staff,DC=corp,DC=local` where a name belongs is not an improvement
    over showing nothing."""
    parts = split_dn(dn)
    if not parts:
        return None
    head = parts[0]
    if "=" not in head:
        return head.replace("\\,", ",") or None
    return head.split("=", 1)[1].strip().replace("\\,", ",") or None


def hostname_of(entry):
    """The hostname to join an AD computer object to a machine_info row by.

    `dNSHostName` is preferred and `name` (the sAM-style short name) is the fallback,
    because a machine enrols under whatever Windows reports as its hostname -- which is
    the short name -- while AD's DNS name carries the domain suffix. Both are reduced to
    the leading label so `PC-1` and `pc-1.corp.local` are the same machine, which is the
    entire reason the join works at all.
    """
    raw = entry.get("dNSHostName") or entry.get("name") or ""
    return str(raw).split(".")[0].strip()


def match_key(hostname):
    """Machine names are matched case-insensitively: AD says `PC-1`, an agent may have
    enrolled as `pc-1`, and they are one PC."""
    return str(hostname or "").strip().casefold()


# ================================
# ATTRIBUTE COERCION  (pure)
# ================================
# 1601-01-01 to 1970-01-01 in seconds. AD stores lastLogonTimestamp as 100-nanosecond
# intervals since 1601 ("FILETIME"), which is not a unix epoch and is off by 369 years if
# treated as one.
_FILETIME_EPOCH_OFFSET = 11644473600
_FILETIME_TICKS_PER_SECOND = 10_000_000


def filetime_to_epoch(value):
    """Convert an AD FILETIME to unix epoch seconds, or None.

    0 and 0x7FFFFFFFFFFFFFFF are AD's two ways of spelling "never", and both must come
    back as None rather than as 1601 or as a date 30 000 years out -- either of which
    would sort to the top of a "least recently seen" list and send someone chasing it.
    """
    try:
        ticks = int(value)
    except (TypeError, ValueError):
        return None
    if ticks <= 0 or ticks >= 0x7FFFFFFFFFFFFFFF:
        return None
    seconds = ticks // _FILETIME_TICKS_PER_SECOND - _FILETIME_EPOCH_OFFSET
    return seconds if seconds > 0 else None


def _first(value):
    """LDAP is multi-valued everywhere; ldap3 hands back a list for some attributes and a
    scalar for others depending on the schema. Normalise to one value."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _text(value):
    value = _first(value)
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).strip()
    return text or None


def computer_facts(entry):
    """Reduce one raw LDAP entry to the fields machine_info stores. Pure, so the coercion
    rules above are testable without a directory."""
    dn = _text(entry.get("distinguishedName"))
    try:
        uac = int(_first(entry.get("userAccountControl")) or 0)
    except (TypeError, ValueError):
        uac = 0
    return {
        "hostname": hostname_of({k: _text(v) for k, v in entry.items()}),
        "ad_dn": dn,
        "ad_ou": ou_of(dn),
        "ad_object_guid": _text(entry.get("objectGUID")),
        "ad_owner": cn_of(_text(entry.get("managedBy"))),
        "ad_os": _text(entry.get("operatingSystem")),
        "ad_disabled": 1 if uac & UAC_ACCOUNTDISABLE else 0,
        "ad_last_logon": filetime_to_epoch(_first(entry.get("lastLogonTimestamp"))),
    }


# ================================
# RECONCILIATION  (pure)
# ================================
def reconcile(known_machines, entries):
    """Decide what to write, given the machines this hub knows and what AD returned.

    `known_machines` is an iterable of hostnames from machine_info; `entries` is raw LDAP
    entries. Returns {"updates", "unmatched", "unknown", "duplicates"}:

      * `updates`   -- [(machine, facts)] for machines AD had an object for.
      * `unmatched` -- machines this hub knows that AD did NOT have. These are what raise
        a review alert: a PC reporting telemetry that no longer has a computer account is
        either not domain-joined, renamed, or someone's decommissioning gone half-done.
      * `unknown`   -- AD computer objects with no machine here. NOT an error and NOT
        written: it is every domain PC without the agent installed, which on a first sync
        is most of them. Counted, because "AD has 340 computers, you manage 40" is a
        genuinely useful number, but never turned into machine records -- inventing
        machines from AD would fill the console with rows that never report.
      * `duplicates` -- hostnames AD returned more than one object for. Real: a machine
        re-joined without cleaning up the old account leaves two, and picking one
        arbitrarily means the OU flips between syncs. The FIRST is used and the collision
        is reported, so the choice is at least stable within a pass.

    The one-way rule this encodes: **AD is authoritative for AD's fields, and for nothing
    else.** A hostname AD does not know keeps every reading, command and backup it has.
    """
    known = {}
    for machine in known_machines:
        known[match_key(machine)] = machine

    updates = []
    duplicates = []
    unknown = []
    seen = {}
    for entry in entries:
        facts = computer_facts(entry)
        key = match_key(facts["hostname"])
        if not key:
            continue
        if key in seen:
            duplicates.append(facts["hostname"])
            continue
        seen[key] = facts
        machine = known.get(key)
        if machine is None:
            unknown.append(facts["hostname"])
            continue
        updates.append((machine, facts))

    unmatched = sorted(machine for key, machine in known.items() if key not in seen)
    return {
        "updates": updates,
        "unmatched": unmatched,
        "unknown": sorted(unknown),
        "duplicates": sorted(set(duplicates)),
    }


# ================================
# DB READS / WRITES
# ================================
def known_machines(db_path):
    with get_conn(db_path) as conn:
        return [row["machine"] for row in
                conn.execute("SELECT machine FROM machine_info ORDER BY machine")]


def apply_updates(db_path, updates, now=None):
    """Write reconciled AD facts onto machine_info rows.

    A plain UPDATE, never an INSERT: this must not create machine rows. `ad_synced_at` is
    stamped on every matched row so a stale value is visible as "AD stopped knowing about
    this one" rather than being indistinguishable from a fresh match.
    """
    now = int(now if now is not None else time.time())
    if not updates:
        return 0
    with get_conn(db_path) as conn:
        written = 0
        for machine, facts in updates:
            written += conn.execute(
                "UPDATE machine_info SET ad_dn = ?, ad_ou = ?, ad_object_guid = ?, "
                "ad_owner = ?, ad_os = ?, ad_disabled = ?, ad_last_logon = ?, "
                "ad_synced_at = ? WHERE machine = ?",
                (facts["ad_dn"], facts["ad_ou"], facts["ad_object_guid"],
                 facts["ad_owner"], facts["ad_os"], facts["ad_disabled"],
                 facts["ad_last_logon"], now, machine),
            ).rowcount or 0
    return written


def clear_machine(db_path, machine):
    """Drop the AD fields from one machine, leaving the rest of its record alone.

    Called for a machine AD no longer has an object for. The fields are cleared rather
    than left to rot because a stale OU is worse than no OU: with `ad_ou` scoping, a
    deleted computer account whose OU stayed behind would keep granting access through a
    group scoped to that OU, indefinitely, with nothing on screen to suggest it.
    """
    with get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE machine_info SET ad_dn = NULL, ad_ou = NULL, ad_object_guid = NULL, "
            "ad_owner = NULL, ad_os = NULL, ad_disabled = NULL, ad_last_logon = NULL "
            "WHERE machine = ?", (machine,)).rowcount or 0


def machines_in_ous(db_path, ous):
    """Every machine whose OU is inside any of `ous`, for `ad_ou` scope resolution.

    Filtering happens in Python rather than in SQL because containment is component-wise
    (see ou_contains) -- a `LIKE '%' || ? ` suffix match would let `OU=Clinical,DC=x`
    capture `OU=NotClinical,DC=x`, which is an access-control bug, not a display one. The
    candidate set is one indexed column over the machine roster, so this stays cheap.
    """
    wanted = [o for o in (ous or []) if str(o or "").strip()]
    if not wanted:
        return []
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT machine, ad_ou FROM machine_info WHERE ad_ou IS NOT NULL").fetchall()
    return sorted(row["machine"] for row in rows
                  if any(ou_contains(scope, row["ad_ou"]) for scope in wanted))


def known_ous(db_path):
    """Every distinct OU the fleet actually sits in, for the scope picker. Offering the
    admin the OUs their own machines are in beats making them paste a DN."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ad_ou FROM machine_info "
            "WHERE ad_ou IS NOT NULL AND ad_ou <> '' ORDER BY ad_ou").fetchall()
    return [row["ad_ou"] for row in rows]


# ================================
# SYNC RUN LOG
# ================================
def start_run(db_path, now=None):
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO directory_sync_runs(started_at, status) VALUES (?, 'running')",
            (now,))
        return cur.lastrowid


def finish_run(db_path, run_id, status, objects_found=None, matched=None,
               unmatched=None, error=None, now=None):
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE directory_sync_runs SET finished_at = ?, status = ?, "
            "objects_found = ?, matched = ?, unmatched = ?, error = ? WHERE id = ?",
            (now, status, objects_found, matched, unmatched,
             (str(error)[:500] if error else None), run_id))


def last_run(db_path):
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM directory_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def last_success(db_path):
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM directory_sync_runs WHERE status = 'succeeded' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def prune_runs(db_path, keep=200):
    """Keep the run log bounded. Hourly forever is ~9k rows a year of pure log."""
    with get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM directory_sync_runs WHERE id NOT IN "
            "(SELECT id FROM directory_sync_runs ORDER BY id DESC LIMIT ?)", (keep,))


# ================================
# LDAP  (the only networked part)
# ================================
class DirectoryError(Exception):
    """A sync could not be performed. Carries a message meant for an operator to read in
    the console -- these are almost always configuration, not code."""


def _load_ldap3():
    try:
        import ldap3
    except ImportError:
        raise DirectoryError(LDAP_IMPORT_HINT)
    return ldap3


def validate_config(config):
    """Check a sync configuration and return (server_url, base_dn, bind_dn, password).

    Pure, and deliberately called BEFORE the ldap3 import. Two reasons: an admin filling
    in the settings form should be told their bind DN is empty whether or not the library
    happens to be installed, and -- the one that matters -- the cleartext-bind refusal is
    a safety check, so it must not be reachable only on hubs that got a dependency right.
    """
    server_url = str(config.get("server") or "").strip()
    base_dn = str(config.get("base_dn") or "").strip()
    bind_dn = str(config.get("bind_dn") or "").strip()
    password = config.get("password") or ""
    if not server_url:
        raise DirectoryError("No domain controller is configured (directory.server).")
    if not base_dn:
        raise DirectoryError("No search base is configured (directory.base_dn).")
    if not bind_dn:
        raise DirectoryError("No bind account is configured (directory.bind_dn).")
    if not password:
        raise DirectoryError(
            f"No bind password is set. Put it in .env as {BIND_PASSWORD_ENV}=... and "
            f"restart the hub -- it is a credential, so it is deliberately not stored "
            f"in hub settings.")
    if not server_url.lower().startswith("ldaps://") and not config.get("allow_insecure"):
        # An LDAP simple bind sends the service-account password in cleartext. Refusing by
        # default is the point: this is a domain account with read access to the whole
        # directory, and the failure mode of getting it wrong is silent.
        raise DirectoryError(
            "Refusing to bind over plain LDAP -- the service-account password would be "
            "sent in cleartext. Use ldaps:// (port 636), or explicitly allow insecure "
            "binds in settings if this is an isolated lab.")
    return server_url, base_dn, bind_dn, password


def fetch_computers(config, page_size=500):
    """Bind to the DC and return raw computer entries as plain dicts.

    Paged, always. AD's default MaxPageSize is 1000 and it does NOT error when a search
    exceeds it -- it returns the first page and a referral, so an unpaged search against a
    2000-computer domain silently syncs half the fleet and marks the other half as having
    no AD object. That failure is invisible in every log and would raise a review alert on
    a thousand perfectly healthy machines, so paging is not an optimisation here.
    """
    server_url, base_dn, bind_dn, password = validate_config(config)
    ldap3 = _load_ldap3()
    use_ssl = server_url.lower().startswith("ldaps://")

    tls = None
    if use_ssl and config.get("tls_verify", True):
        import ssl
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED)
    elif use_ssl:
        import ssl
        tls = ldap3.Tls(validate=ssl.CERT_NONE)

    server = ldap3.Server(server_url, get_info=ldap3.NONE, tls=tls,
                          connect_timeout=int(config.get("timeout_seconds") or 15))
    search_filter = (str(config.get("computer_filter") or "").strip()
                     or "(objectClass=computer)")
    try:
        conn = ldap3.Connection(
            server, user=bind_dn, password=password,
            auto_bind=True, raise_exceptions=True,
            receive_timeout=int(config.get("timeout_seconds") or 15),
        )
    except Exception as e:
        raise DirectoryError(f"Could not bind to {server_url} as {bind_dn}: {e}")

    entries = []
    try:
        for entry in conn.extend.standard.paged_search(
            search_base=base_dn,
            search_filter=search_filter,
            search_scope=ldap3.SUBTREE,
            attributes=list(COMPUTER_ATTRIBUTES),
            paged_size=max(1, int(page_size or 500)),
            generator=True,
        ):
            # paged_search yields referral chases (type 'searchResRef') alongside real
            # entries; those have no attributes and are not objects.
            if entry.get("type") != "searchResEntry":
                continue
            attributes = dict(entry.get("attributes") or {})
            # `distinguishedName` is not always returned as an attribute even when asked
            # for, but the entry always carries its own dn.
            attributes.setdefault("distinguishedName", entry.get("dn"))
            entries.append(attributes)
    except Exception as e:
        raise DirectoryError(f"LDAP search under {base_dn} failed: {e}")
    finally:
        try:
            conn.unbind()
        except Exception:
            pass
    return entries


# ================================
# THE PASS
# ================================
def sync_once(db_path, config, fetcher=None, now=None, on_change=None):
    """One full sync: fetch, reconcile, write, alert. Returns a summary dict.

    `fetcher` is injectable so tests -- and anyone wanting a dry run -- can drive the
    whole reconcile/write/alert path against literal entries with no directory present.

    `on_change` is called (with no arguments) when any machine's OU actually moved. That
    is the hook `ad_ou` scoping hangs off: permission scopes resolved from OUs are cached,
    and the ONLY thing that can change them is a sync like this one, so invalidating here
    is both necessary and sufficient.
    """
    now = int(now if now is not None else time.time())
    run_id = start_run(db_path, now=now)
    try:
        fetch = fetcher or (lambda: fetch_computers(
            config, page_size=int(config.get("page_size") or 500)))
        entries = fetch()
        machines = known_machines(db_path)
        result = reconcile(machines, entries)

        ou_before = _ou_map(db_path)
        written = apply_updates(db_path, result["updates"], now=now)
        for machine in result["unmatched"]:
            clear_machine(db_path, machine)
        ou_after = _ou_map(db_path)

        if result["duplicates"]:
            print(f"[directory] {len(result['duplicates'])} hostname(s) have more than "
                  f"one computer object in AD; using the first of each: "
                  f"{', '.join(result['duplicates'][:10])}")

        if config.get("alert_on_unmatched", True):
            _sync_unmatched_alerts(db_path, result["unmatched"], now=now)
        else:
            _resolve_all_unmatched(db_path, now=now)

        finish_run(db_path, run_id, "succeeded",
                   objects_found=len(entries), matched=written,
                   unmatched=len(result["unmatched"]), now=now)
        prune_runs(db_path)

        if ou_before != ou_after and on_change:
            on_change()

        return {
            "status": "succeeded",
            "objects_found": len(entries),
            "matched": written,
            "unmatched": result["unmatched"],
            "unknown": len(result["unknown"]),
            "duplicates": result["duplicates"],
            "ou_changed": ou_before != ou_after,
        }
    except DirectoryError as e:
        finish_run(db_path, run_id, "failed", error=str(e), now=now)
        raise
    except Exception as e:
        finish_run(db_path, run_id, "failed", error=f"{type(e).__name__}: {e}", now=now)
        raise DirectoryError(f"AD sync failed: {e}")


def _ou_map(db_path):
    with get_conn(db_path) as conn:
        return {row["machine"]: row["ad_ou"] for row in
                conn.execute("SELECT machine, ad_ou FROM machine_info")}


# ================================
# ALERTS
# ================================
def _sync_unmatched_alerts(db_path, unmatched, now=None):
    """Raise a review alert per machine AD has no computer object for, and resolve the
    ones that have come back.

    Resolving matters as much as raising: a machine that was off the domain for an
    afternoon should not leave an alert an operator has to dismiss by hand, or the tab
    fills with stale rows and stops being read -- which is the failure mode that makes
    alerting worthless.
    """
    now = int(now if now is not None else time.time())
    wanted = set(unmatched or ())
    open_now = {row["machine"] for row in alerts.list_open(db_path)
                if row.get("kind") == alerts.KIND_AD_UNMATCHED and row.get("machine")}
    for machine in sorted(wanted - open_now):
        alerts.raise_ad_unmatched(db_path, machine, now=now)
    for machine in sorted(open_now - wanted):
        alerts.resolve_ad_unmatched(db_path, machine, now=now)


def _resolve_all_unmatched(db_path, now=None):
    """Turning the alert off should clear the ones it already raised, not freeze them on
    screen with no way to tell they are no longer being maintained."""
    for row in alerts.list_open(db_path):
        if row.get("kind") == alerts.KIND_AD_UNMATCHED and row.get("machine"):
            alerts.resolve_ad_unmatched(db_path, row["machine"], now=now)
