"""Unit tests for directory.py -- the Active Directory sync (roadmap #4).

No LDAP server, and deliberately no `ldap3` either: `fetch_computers` is the only function
that touches a network, and `sync_once` takes a `fetcher` precisely so the whole
reconcile / write / alert path can be driven against literal entries. Everything with a
decision in it is therefore tested here; what is not covered is the ldap3 call itself,
which is a thin adapter.

The emphasis is on the ways this can be wrong SILENTLY, because AD sync has an unusually
bad set of those:

  * a DN parsed wrongly scopes a permission group to the wrong OU, and looks configured
  * a truncated search marks half the fleet as missing from AD
  * a stale OU left behind on a deleted computer account keeps granting access forever
  * FILETIME read as a unix epoch gives every machine a 1601 or a year-30828 last-logon

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import alerts
import directory
import fleet
import permissions

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def entry(name, ou="OU=Clinical,DC=corp,DC=local", **extra):
    """One AD computer object, in the shape ldap3 hands back."""
    doc = {
        "distinguishedName": f"CN={name},{ou}",
        "name": name,
        "dNSHostName": f"{name}.corp.local",
        "objectGUID": "{%s}" % name.lower(),
        "operatingSystem": "Windows 11 Pro",
        "userAccountControl": 4096,
    }
    doc.update(extra)
    return doc


def seed_machine(db_path, machine):
    with directory.get_conn(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO machine_info(machine) VALUES (?)", (machine,))


def machine_row(db_path, machine):
    with directory.get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM machine_info WHERE machine = ?",
                           (machine,)).fetchone()
    return dict(row) if row else None


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        # machine_info is created by app.init_db(); recreate the minimum here so
        # init_directory_db has something to ALTER, exactly as it would in production.
        with directory.get_conn(db_path) as conn:
            conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, "
                         "asset_tag TEXT, serial_number TEXT, model TEXT, updated_at TEXT)")
        fleet.init_fleet_db(db_path)
        alerts.init_alerts_db(db_path)
        permissions.init_permissions_db(db_path)
        directory.init_directory_db(db_path)
        permissions.invalidate()

        print("\n== Schema setup is idempotent ==")
        directory.init_directory_db(db_path)      # must not raise on a second run
        with directory.get_conn(db_path) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(machine_info)")}
        check("the AD columns are added to machine_info",
              {"ad_dn", "ad_ou", "ad_object_guid", "ad_owner", "ad_synced_at"} <= cols)
        check("the pre-existing columns are untouched",
              {"machine", "asset_tag", "serial_number", "model"} <= cols)

        print("\n== DN parsing ==")
        check("a plain DN splits into components",
              directory.split_dn("CN=PC-1,OU=Clinical,DC=corp,DC=local")
              == ["CN=PC-1", "OU=Clinical", "DC=corp", "DC=local"])
        # RFC 4514: an escaped comma is part of a name, not a separator. Getting this
        # wrong silently re-homes every machine in such an OU.
        check("an escaped comma does NOT split a component",
              directory.split_dn(r"CN=PC-1,OU=Sales\, EMEA,DC=corp")
              == ["CN=PC-1", r"OU=Sales\, EMEA", "DC=corp"])
        check("the OU is the DN minus the leaf CN",
              directory.ou_of("CN=PC-1,OU=Clinical,DC=corp,DC=local")
              == "OU=Clinical,DC=corp,DC=local")
        check("a bare CN has no OU", directory.ou_of("CN=PC-1") is None)
        check("an empty DN has no OU", directory.ou_of("") is None)
        check("the OU keeps its ORIGINAL case (it is displayed back)",
              directory.ou_of("CN=PC-1,OU=Clinical,DC=Corp") == "OU=Clinical,DC=Corp")
        check("normalisation folds case and comma spacing",
              directory.normalize_dn("OU=Clinical, DC=Corp, DC=Local")
              == directory.normalize_dn("ou=clinical,dc=corp,dc=local"))

        print("\n== OU containment -- the access-control decision ==")
        check("an OU contains itself",
              directory.ou_contains("OU=Clinical,DC=corp", "OU=Clinical,DC=corp"))
        check("case and spacing differences still match",
              directory.ou_contains("ou=clinical, dc=corp", "OU=Clinical,DC=corp"))
        # Nesting must count, or an admin who picks a parent OU silently excludes every
        # machine actually filed in a child.
        check("a parent OU contains a nested child OU",
              directory.ou_contains("OU=Clinical,DC=corp",
                                    "OU=Ward 3,OU=Clinical,DC=corp"))
        check("a child OU does NOT contain its parent",
              not directory.ou_contains("OU=Ward 3,OU=Clinical,DC=corp",
                                        "OU=Clinical,DC=corp"))
        # THE bug a string endswith would introduce: a scope granting access to an OU it
        # merely shares a suffix spelling with.
        check("matching is component-wise, so NotClinical is not inside Clinical",
              not directory.ou_contains("OU=Clinical,DC=corp",
                                        "OU=NotClinical,DC=corp"))
        check("...and Clinical-Admin is not inside Clinical",
              not directory.ou_contains("OU=Clinical,DC=corp",
                                        "OU=Clinical-Admin,DC=corp"))
        check("a sibling OU does not match",
              not directory.ou_contains("OU=Clinical,DC=corp", "OU=Finance,DC=corp"))
        check("an empty scope matches nothing",
              not directory.ou_contains("", "OU=Clinical,DC=corp"))
        check("an empty machine OU matches nothing",
              not directory.ou_contains("OU=Clinical,DC=corp", ""))

        print("\n== Hostname join key ==")
        check("dNSHostName is reduced to its leading label",
              directory.hostname_of({"dNSHostName": "PC-1.corp.local"}) == "PC-1")
        check("name is the fallback",
              directory.hostname_of({"name": "PC-2"}) == "PC-2")
        check("dNSHostName wins when both are present",
              directory.hostname_of({"dNSHostName": "a.corp.local", "name": "b"}) == "a")
        check("matching is case-insensitive",
              directory.match_key("PC-1") == directory.match_key("pc-1"))

        print("\n== managedBy is shown as a name, not a DN ==")
        check("the leaf CN is extracted",
              directory.cn_of("CN=Dana Ruiz,OU=Staff,DC=corp,DC=local") == "Dana Ruiz")
        check("an escaped comma survives",
              directory.cn_of(r"CN=Ruiz\, Dana,OU=Staff,DC=corp") == "Ruiz, Dana")
        check("no managedBy -> None", directory.cn_of(None) is None)

        print("\n== FILETIME conversion ==")
        # 133000000000000000 ticks -> 2022-08-26ish. The point is the 1601 epoch offset.
        converted = directory.filetime_to_epoch(133000000000000000)
        check("a real FILETIME lands in a plausible decade",
              converted is not None and 1_600_000_000 < converted < 2_000_000_000)
        # AD's two spellings of "never". Both must be None, or a machine sorts to the top
        # of a "least recently seen" list forever.
        check("0 means never, not 1601", directory.filetime_to_epoch(0) is None)
        check("0x7FFFFFFFFFFFFFFF means never, not the year 30828",
              directory.filetime_to_epoch(0x7FFFFFFFFFFFFFFF) is None)
        check("a non-numeric value is None, not a crash",
              directory.filetime_to_epoch("not-a-number") is None)
        check("None is None", directory.filetime_to_epoch(None) is None)

        print("\n== Reconciliation ==")
        result = directory.reconcile(
            ["PC-1", "PC-2"], [entry("PC-1"), entry("PC-3")])
        check("a machine AD knows is an update",
              [m for m, _ in result["updates"]] == ["PC-1"])
        check("a machine AD does NOT know is unmatched", result["unmatched"] == ["PC-2"])
        # The important non-action: AD is full of computers without the agent. Inventing
        # machine records for them would fill the console with rows that never report.
        check("an AD computer with no machine record is 'unknown', not created",
              result["unknown"] == ["PC-3"])

        check("case differences still join",
              [m for m, _ in directory.reconcile(["pc-1"], [entry("PC-1")])["updates"]]
              == ["pc-1"])
        check("...and the DB's spelling of the hostname is preserved",
              directory.reconcile(["PC-1"], [entry("pc-1")])["updates"][0][0] == "PC-1")

        dupes = directory.reconcile(
            ["PC-1"], [entry("PC-1", ou="OU=Clinical,DC=corp"),
                       entry("PC-1", ou="OU=Finance,DC=corp")])
        check("a hostname with two AD objects is reported as a duplicate",
              dupes["duplicates"] == ["PC-1"])
        check("...and only the first is used, so the OU does not flip between syncs",
              len(dupes["updates"]) == 1
              and dupes["updates"][0][1]["ad_ou"] == "OU=Clinical,DC=corp")
        check("an entry with no usable hostname is skipped, not crashed on",
              directory.reconcile(["PC-1"], [{"distinguishedName": "OU=Empty,DC=corp"}])
              ["unmatched"] == ["PC-1"])
        check("no AD entries at all -> every machine unmatched",
              directory.reconcile(["PC-1", "PC-2"], [])["unmatched"] == ["PC-1", "PC-2"])

        print("\n== A sync writes AD facts onto machine rows ==")
        for name in ("PC-1", "PC-2", "PC-3"):
            seed_machine(db_path, name)
        entries = [
            entry("PC-1", ou="OU=Ward 3,OU=Clinical,DC=corp,DC=local",
                  managedBy="CN=Dana Ruiz,OU=Staff,DC=corp,DC=local",
                  lastLogonTimestamp=133000000000000000),
            entry("PC-2", ou="OU=Finance,DC=corp,DC=local"),
        ]
        summary = directory.sync_once(db_path, {"alert_on_unmatched": True},
                                      fetcher=lambda: entries)
        check("the pass succeeds", summary["status"] == "succeeded")
        check("both known machines matched", summary["matched"] == 2)
        check("the machine AD lacks is reported unmatched", summary["unmatched"] == ["PC-3"])

        row = machine_row(db_path, "PC-1")
        check("the DN is stored", row["ad_dn"] == "CN=PC-1,OU=Ward 3,OU=Clinical,DC=corp,DC=local")
        check("the OU is the DN minus the CN",
              row["ad_ou"] == "OU=Ward 3,OU=Clinical,DC=corp,DC=local")
        check("managedBy is stored as a display name", row["ad_owner"] == "Dana Ruiz")
        check("the OS is stored", row["ad_os"] == "Windows 11 Pro")
        check("lastLogonTimestamp is converted", row["ad_last_logon"] > 1_600_000_000)
        check("the sync time is stamped", row["ad_synced_at"] > 0)
        check("an enabled account is not marked disabled", row["ad_disabled"] == 0)

        print("\n== A disabled computer account is recorded, not treated as missing ==")
        # 4096 (WORKSTATION_TRUST_ACCOUNT) | 2 (ACCOUNTDISABLE)
        directory.sync_once(db_path, {"alert_on_unmatched": False},
                            fetcher=lambda: [entry("PC-1", userAccountControl=4098)])
        check("the disabled flag is set", machine_row(db_path, "PC-1")["ad_disabled"] == 1)
        check("...and it still counts as matched, not unmatched",
              machine_row(db_path, "PC-1")["ad_dn"] is not None)

        print("\n== A machine that leaves AD has its AD fields CLEARED ==")
        # A stale OU is worse than no OU: with ad_ou scoping it would keep granting access
        # through a deleted computer account, indefinitely and invisibly.
        directory.sync_once(db_path, {"alert_on_unmatched": True},
                            fetcher=lambda: [entry("PC-2", ou="OU=Finance,DC=corp,DC=local")])
        gone = machine_row(db_path, "PC-1")
        check("the OU is cleared", gone["ad_ou"] is None)
        check("the DN is cleared", gone["ad_dn"] is None)
        check("the owner is cleared", gone["ad_owner"] is None)
        check("but the machine record itself survives", gone["machine"] == "PC-1")

        print("\n== Unmatched machines raise (and clear) a review alert ==")
        open_kinds = [a for a in alerts.list_open(db_path)
                      if a["kind"] == alerts.KIND_AD_UNMATCHED]
        check("PC-1 and PC-3 are flagged as missing from AD",
              {a["machine"] for a in open_kinds} == {"PC-1", "PC-3"})
        first_created = {a["machine"]: a["created_at"] for a in open_kinds}

        # A second identical pass must not re-stamp the alert: an hourly sync would
        # otherwise float a months-old problem to the top of the list every hour.
        directory.sync_once(db_path, {"alert_on_unmatched": True},
                            fetcher=lambda: [entry("PC-2", ou="OU=Finance,DC=corp,DC=local")])
        again = [a for a in alerts.list_open(db_path)
                 if a["kind"] == alerts.KIND_AD_UNMATCHED]
        check("a repeat pass does not duplicate the alerts", len(again) == 2)
        check("...and does not re-stamp created_at",
              {a["machine"]: a["created_at"] for a in again} == first_created)

        # PC-1 comes back.
        directory.sync_once(db_path, {"alert_on_unmatched": True},
                            fetcher=lambda: [entry("PC-1"),
                                             entry("PC-2", ou="OU=Finance,DC=corp,DC=local")])
        still_open = {a["machine"] for a in alerts.list_open(db_path)
                      if a["kind"] == alerts.KIND_AD_UNMATCHED}
        check("a machine that reappears in AD auto-resolves its alert",
              "PC-1" not in still_open)
        check("...while one that is still missing stays flagged", "PC-3" in still_open)
        check("and its AD fields are repopulated",
              machine_row(db_path, "PC-1")["ad_ou"] == "OU=Clinical,DC=corp,DC=local")

        print("\n== Turning the alert off clears the ones it raised ==")
        directory.sync_once(db_path, {"alert_on_unmatched": False},
                            fetcher=lambda: [entry("PC-1")])
        check("no ad_unmatched alerts remain open",
              not [a for a in alerts.list_open(db_path)
                   if a["kind"] == alerts.KIND_AD_UNMATCHED])

        print("\n== A dismissed alert stays dismissed across syncs ==")
        directory.sync_once(db_path, {"alert_on_unmatched": True},
                            fetcher=lambda: [])
        target = next(a for a in alerts.list_open(db_path)
                      if a["kind"] == alerts.KIND_AD_UNMATCHED and a["machine"] == "PC-3")
        alerts.dismiss(db_path, target["id"])
        directory.sync_once(db_path, {"alert_on_unmatched": True}, fetcher=lambda: [])
        check("a dismissed alert is not re-raised by the next sync",
              not [a for a in alerts.list_open(db_path)
                   if a["kind"] == alerts.KIND_AD_UNMATCHED and a["machine"] == "PC-3"])

        print("\n== The run log records failures, which is its whole point ==")
        def boom():
            raise directory.DirectoryError("could not bind to dc1")
        try:
            directory.sync_once(db_path, {}, fetcher=boom)
            check("a failing sync raises DirectoryError", False)
        except directory.DirectoryError:
            check("a failing sync raises DirectoryError", True)
        last = directory.last_run(db_path)
        check("the failure is logged", last["status"] == "failed")
        check("...with the operator-readable reason", "could not bind" in last["error"])
        check("the last SUCCESS is still reported separately",
              directory.last_success(db_path)["status"] == "succeeded")

        print("\n== on_change fires only when an OU actually moved ==")
        # This is what invalidates ad_ou permission scoping. Firing it on every pass would
        # dump the permissions cache hourly for nothing; not firing it when an OU moved
        # would leave a machine in its old group's scope until the hub restarted.
        fired = []
        directory.sync_once(db_path, {"alert_on_unmatched": False},
                            fetcher=lambda: [entry("PC-1", ou="OU=Clinical,DC=corp,DC=local")],
                            on_change=lambda: fired.append(1))
        before = len(fired)
        directory.sync_once(db_path, {"alert_on_unmatched": False},
                            fetcher=lambda: [entry("PC-1", ou="OU=Clinical,DC=corp,DC=local")],
                            on_change=lambda: fired.append(1))
        check("an unchanged pass does not fire it", len(fired) == before)
        directory.sync_once(db_path, {"alert_on_unmatched": False},
                            fetcher=lambda: [entry("PC-1", ou="OU=Finance,DC=corp,DC=local")],
                            on_change=lambda: fired.append(1))
        check("a machine moving OU does fire it", len(fired) == before + 1)

        print("\n== machines_in_ous resolves nesting, for ad_ou scoping ==")
        directory.sync_once(db_path, {"alert_on_unmatched": False}, fetcher=lambda: [
            entry("PC-1", ou="OU=Ward 3,OU=Clinical,DC=corp,DC=local"),
            entry("PC-2", ou="OU=Clinical,DC=corp,DC=local"),
            entry("PC-3", ou="OU=Finance,DC=corp,DC=local"),
        ])
        check("a parent OU picks up machines in child OUs",
              directory.machines_in_ous(db_path, ["OU=Clinical,DC=corp,DC=local"])
              == ["PC-1", "PC-2"])
        check("a child OU picks up only its own",
              directory.machines_in_ous(db_path, ["OU=Ward 3,OU=Clinical,DC=corp,DC=local"])
              == ["PC-1"])
        check("several OUs union",
              directory.machines_in_ous(
                  db_path, ["OU=Finance,DC=corp,DC=local",
                            "OU=Ward 3,OU=Clinical,DC=corp,DC=local"]) == ["PC-1", "PC-3"])
        check("an empty OU list resolves to nothing (never to everything)",
              directory.machines_in_ous(db_path, []) == [])
        check("a blank string resolves to nothing",
              directory.machines_in_ous(db_path, ["", "   "]) == [])
        check("an OU nothing is in resolves to nothing",
              directory.machines_in_ous(db_path, ["OU=Nowhere,DC=corp"]) == [])
        check("known_ous lists the distinct OUs in use",
              set(directory.known_ous(db_path)) == {
                  "OU=Ward 3,OU=Clinical,DC=corp,DC=local",
                  "OU=Clinical,DC=corp,DC=local",
                  "OU=Finance,DC=corp,DC=local"})

        print("\n== Configuration validation ==")
        # Asserted on the MESSAGE, not merely that DirectoryError was raised. These ran
        # against a hub without ldap3 installed, where "not configured" and "library
        # missing" are both DirectoryError -- so a bare raises-check passed vacuously and
        # proved nothing about the validation it claimed to cover.
        for config, why, expect in (
            ({}, "no server", "directory.server"),
            ({"server": "ldaps://dc"}, "no base DN", "directory.base_dn"),
            ({"server": "ldaps://dc", "base_dn": "DC=corp"}, "no bind account",
             "directory.bind_dn"),
            ({"server": "ldaps://dc", "base_dn": "DC=corp", "bind_dn": "CN=svc"},
             "no bind password", directory.BIND_PASSWORD_ENV),
        ):
            try:
                directory.validate_config(config)
                check(f"refuses a config with {why}", False)
            except directory.DirectoryError as e:
                check(f"refuses a config with {why}, and says which field", expect in str(e))
        # The one that matters: a simple bind over plain LDAP sends a domain service
        # account's password in cleartext. This is a safety check, so it must be reachable
        # WITHOUT ldap3 installed -- i.e. it must not sit behind the library import.
        try:
            directory.validate_config({"server": "ldap://dc", "base_dn": "DC=corp",
                                       "bind_dn": "CN=svc", "password": "pw"})
            check("refuses a cleartext bind by default", False)
        except directory.DirectoryError as e:
            check("refuses a cleartext bind by default", "cleartext" in str(e))
        ok = directory.validate_config({"server": "ldap://dc", "base_dn": "DC=corp",
                                        "bind_dn": "CN=svc", "password": "pw",
                                        "allow_insecure": True})
        check("...but allows it when explicitly opted into", ok[0] == "ldap://dc")
        check("a complete ldaps config validates",
              directory.validate_config({"server": "ldaps://dc", "base_dn": "DC=corp",
                                         "bind_dn": "CN=svc", "password": "pw"})[1]
              == "DC=corp")

        print("\n== A missing ldap3 is a configuration message, not a crash ==")
        if not directory.ldap3_installed():
            try:
                directory.fetch_computers({"server": "ldaps://dc", "base_dn": "DC=corp",
                                           "bind_dn": "CN=svc", "password": "pw"})
                check("reports the missing library", False)
            except directory.DirectoryError as e:
                check("reports the missing library with an install hint",
                      "ldap3" in str(e) and "pip install" in str(e))
        else:
            check("ldap3 is installed -- import path not exercised here", True)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
