"""Unit tests for posture.py -- security posture and its CIS IG1 checks (roadmap #25 D).

**The silent failure this file exists to catch is an unknown that is counted as a verdict.**
A compliance view is read by people who will not open the machine to check, so:

  * a value the agent could not read (JSON null, a provider error) must judge `unknown`,
    never `pass` and never `fail` -- either way round the console lies, and a false red is
    as damaging as a false green, because it teaches the helpdesk to stop reading;
  * a payload that is not a posture at all must replace nothing, or one truncated heartbeat
    swaps a real report for a document of unknowns;
  * a string "false" from a confused agent must not be stored as a boolean true -- Python
    truthiness is how a firewall reported off would read as on.

Also pinned: the judgments CIS cares about on a real fleet (a passive Defender beside a
running Kaspersky is not a finding; a machine-wide lock limit settles 4.3 whatever a user's
screen saver says; Domain Admins and the built-in Administrator are sanctioned, a support
group is not until somebody names it), the fleet summary's scoping, and the lifecycle.

The posture shapes here are synthetic. The first real reading (a domain PC with Kaspersky,
roadmap #25 D) is what the antivirus and administrator cases are modelled on.
"""
import gc
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import posture
import settings

PASS = 0
FAIL = 0

THRESHOLDS = {"signature_max_age_days": 7, "lock_max_seconds": 900, "admin_allowlist": []}


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def report(**override):
    """A healthy posture, with whole areas replaced by `override`."""
    base = {
        "antivirus": {"supported": True, "error": "", "products": [
            {"name": "Microsoft Defender Antivirus", "state": 0x61100, "enabled": True,
             "up_to_date": True}]},
        "defender": {"present": True, "antivirus_enabled": True, "realtime": True,
                     "signature_age_days": 0, "signature_updated": 1, "mode": "Normal",
                     "error": ""},
        "firewall": {"error": "", "profiles": [{"name": n, "enabled": True}
                                               for n in ("domain", "private", "public")]},
        "autorun": {"no_drive_type_autorun": 255, "no_autorun": 1, "error": ""},
        "session_lock": {"machine_inactivity_seconds": 600, "users": [], "error": ""},
        "accounts": {"error": "",
                     "local": [{"name": "Administrator", "sid": "S-1-5-21-1-2-3-500", "enabled": False},
                               {"name": "Guest", "sid": "S-1-5-21-1-2-3-501", "enabled": False}],
                     "administrators": [
                         {"name": "PC\\Administrator", "sid": "S-1-5-21-1-2-3-500",
                          "kind": "user", "local": True},
                         {"name": "CORP\\Domain Admins", "sid": "S-1-5-21-9-9-9-512",
                          "kind": "group", "local": False}]},
        "secure_boot": {"state": "on", "error": ""},
        "tpm": {"present": True, "enabled": True, "activated": True, "spec_version": "2.0",
                "error": ""},
    }
    base.update(override)
    return posture.clean_posture(base)


ENCRYPTED = {"support": "supported", "volumes": [{"mount": "C:", "protection": "on"}]}


def verdicts(p, thresholds=THRESHOLDS, encryption=ENCRYPTED):
    return {c["id"]: c for c in posture.evaluate(p, thresholds, encryption)}


def main():
    print("\n== A healthy PC passes every check, in CHECKS order ==")
    checks = posture.evaluate(report(), THRESHOLDS, ENCRYPTED)
    check("ten checks, in order", [c["id"] for c in checks] == list(posture.CHECK_IDS))
    check("all pass", all(c["status"] == "pass" for c in checks))
    check("each names its CIS safeguard",
          {c["id"]: c["cis"] for c in checks}["firewall"] == "4.4, 4.5")

    print("\n== Unknown is unknown, never a verdict ==")
    v = verdicts(posture.clean_posture({"firewall": {"profiles": [
        {"name": "domain", "enabled": True}, {"name": "public", "enabled": None}], "error": ""}}))
    check("a profile the service would not describe is unknown, not off",
          v["firewall"]["status"] == "unknown")
    check("an area the agent did not send at all is unknown", v["tpm"]["status"] == "unknown")
    check("...and so is antivirus", v["antivirus"]["status"] == "unknown")
    v = verdicts(report(tpm={"present": None, "enabled": None, "activated": None,
                             "spec_version": "", "error": "ManagementException: Access denied"}))
    check("a provider error is unknown and carries the error",
          v["tpm"]["status"] == "unknown" and v["tpm"]["detail"] == "read_failed"
          and "Access denied" in v["tpm"]["params"]["error"])
    check("...without costing the other checks", v["firewall"]["status"] == "pass")
    v = verdicts(report(), encryption={"support": None})
    check("BitLocker never reported is unknown, not unencrypted",
          v["encryption"]["status"] == "unknown")

    print("\n== Strict types at the ingest boundary ==")
    p = posture.clean_posture({"firewall": {"profiles": [{"name": "Public", "enabled": "false"}]}})
    check("a string 'false' is not stored as true", p["firewall"]["profiles"][0]["enabled"] is None)
    check("profile names are normalised", p["firewall"]["profiles"][0]["name"] == "public")
    p = posture.clean_posture({"autorun": {"no_drive_type_autorun": True}})
    check("a boolean is not an integer", p["autorun"]["no_drive_type_autorun"] is None)
    check("not a posture at all -> None", posture.clean_posture({"something": 1}) is None)
    check("not a dict -> None", posture.clean_posture(["firewall"]) is None)

    print("\n== The judgments a real fleet needs ==")
    kaspersky = report(
        antivirus={"supported": True, "error": "", "products": [
            {"name": "Kaspersky Endpoint Security", "state": 0x41000, "enabled": True,
             "up_to_date": True}]},
        defender={"present": True, "antivirus_enabled": False, "realtime": False,
                  "signature_age_days": None, "signature_updated": None,
                  "mode": "Not running", "error": ""})
    v = verdicts(kaspersky)
    check("a running third-party product passes antivirus",
          v["antivirus"]["status"] == "pass" and "Kaspersky" in v["antivirus"]["params"]["names"])
    check("...and a passive Defender with no signatures is not a stale-signature finding",
          v["signatures"]["status"] == "pass")
    v = verdicts(report(antivirus={"supported": True, "error": "", "products": [
        {"name": "Kaspersky", "state": 0x40000, "enabled": False, "up_to_date": True}]},
        defender={"present": True, "realtime": False, "error": ""}))
    check("a registered product that is off fails, by name",
          v["antivirus"]["status"] == "fail" and v["antivirus"]["detail"] == "av_off")
    v = verdicts(report(defender={"present": True, "realtime": True, "signature_age_days": 9,
                                  "error": ""},
                        antivirus={"supported": True, "error": "", "products": []}))
    check("Defender nine days stale against a seven-day limit fails",
          v["signatures"]["status"] == "fail" and v["signatures"]["params"] == {"days": 9, "max": 7})
    check("...and the limit is the operator's",
          verdicts(report(defender={"present": True, "realtime": True, "signature_age_days": 9,
                                    "error": ""},
                          antivirus={"supported": True, "error": "", "products": []}),
                   thresholds={**THRESHOLDS, "signature_max_age_days": 10})["signatures"]["status"]
          == "pass")
    v = verdicts(report(antivirus={"supported": False, "error": "", "products": []},
                        defender={"present": True, "realtime": False, "error": ""}))
    check("on a Server, Defender off is antivirus off", v["antivirus"]["status"] == "fail")

    v = verdicts(report(autorun={"no_drive_type_autorun": None, "no_autorun": None, "error": ""}))
    check("AutoRun not configured fails -- Windows' default is on",
          v["autorun"]["status"] == "fail")
    v = verdicts(report(autorun={"no_drive_type_autorun": 255, "no_autorun": None, "error": ""}))
    check("...and AutoPlay off without NoAutorun is still a fail", v["autorun"]["status"] == "fail")

    v = verdicts(report(firewall={"error": "", "profiles": [
        {"name": "domain", "enabled": True}, {"name": "private", "enabled": False},
        {"name": "public", "enabled": False}]}))
    check("profiles that are off fail and are named",
          v["firewall"]["status"] == "fail" and v["firewall"]["params"]["profiles"]
          == ["private", "public"])

    one_user = {"sid": "S-1-5-21-1-1001", "active": True, "secure": True, "timeout_seconds": 1800}
    v = verdicts(report(session_lock={"machine_inactivity_seconds": None, "users": [one_user],
                                      "error": ""}))
    check("a 30-minute screen saver against a 15-minute limit fails, with both numbers",
          v["session_lock"]["status"] == "fail"
          and v["session_lock"]["params"] == {"seconds": 1800, "max": 900})
    v = verdicts(report(session_lock={"machine_inactivity_seconds": 600, "users": [one_user],
                                      "error": ""}))
    check("...but a machine-wide limit settles it for every session",
          v["session_lock"]["status"] == "pass")
    v = verdicts(report(session_lock={"machine_inactivity_seconds": None, "users": [],
                                      "error": ""}))
    check("nobody signed in and no machine limit is unknown, not a fail",
          v["session_lock"]["status"] == "unknown")
    v = verdicts(report(session_lock={"machine_inactivity_seconds": None, "users": [
        {"sid": "S-1-5-21-1-1001", "active": True, "secure": False, "timeout_seconds": 300}],
        "error": ""}))
    check("a screen saver that does not ask for a password does not lock anything",
          v["session_lock"]["status"] == "fail" and v["session_lock"]["detail"] == "lock_missing")

    accounts = report()["accounts"]
    enabled_admin = {**accounts, "local": [{"name": "Administrador", "sid": "S-1-5-21-1-2-3-500",
                                            "enabled": True}]}
    v = verdicts(report(accounts=enabled_admin))
    check("an enabled built-in Administrator fails 4.7, by its localised name",
          v["default_accounts"]["status"] == "fail"
          and v["default_accounts"]["params"]["names"] == "Administrador")
    support_group = {**accounts, "administrators": accounts["administrators"] + [
        {"name": "CORP\\Soporte", "sid": "S-1-5-21-9-9-9-26777", "kind": "group", "local": False}]}
    v = verdicts(report(accounts=support_group))
    check("a support group nobody named fails 5.4",
          v["admin_accounts"]["status"] == "fail"
          and v["admin_accounts"]["params"]["names"] == "CORP\\Soporte")
    for entry in ("soporte", "CORP\\soporte", "S-1-5-21-9-9-9-26777"):
        v = verdicts(report(accounts=support_group),
                     thresholds={**THRESHOLDS, "admin_allowlist": [entry]})
        check(f"...and passes once allowed as {entry!r}", v["admin_accounts"]["status"] == "pass")
    entra = {**accounts, "administrators": [
        {"name": "", "sid": "S-1-12-1-111-222-333-444", "kind": "group", "local": False}]}
    check("an Entra role group is sanctioned",
          verdicts(report(accounts=entra))["admin_accounts"]["status"] == "pass")
    entra_user = {**accounts, "administrators": [
        {"name": "AzureAD\\pat", "sid": "S-1-12-1-5-6-7-8", "kind": "user", "local": False}]}
    check("...but an Entra USER is a person, and is not",
          verdicts(report(accounts=entra_user))["admin_accounts"]["status"] == "fail")

    v = verdicts(report(secure_boot={"state": "unsupported", "error": ""}))
    check("a legacy-BIOS boot fails Secure Boot with its own wording",
          v["secure_boot"]["status"] == "fail" and v["secure_boot"]["detail"] == "secure_boot_legacy")
    v = verdicts(report(), encryption={"support": "supported", "volumes": [
        {"mount": "C:", "protection": "on"}, {"mount": "D:", "protection": "off"}]})
    check("an unprotected data volume fails 3.6 and is named",
          v["encryption"]["status"] == "fail" and v["encryption"]["params"]["volumes"] == "D:")

    print("\n== Storage, scoping and lifecycle ==")
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        posture.init_posture_db(db)
        posture.init_posture_db(db)  # idempotent
        settings.init_settings_db(db)
        settings.invalidate()
        check("never reported -> posture None",
              posture.get_posture(db, "PC-01") == {"posture": None, "reported_at": None})
        raw = {"firewall": {"profiles": [{"name": "public", "enabled": False}], "error": ""}}
        check("a report is stored", posture.record_posture(db, "PC-01", raw, now=100))
        check("...and read back", posture.get_posture(db, "PC-01")["reported_at"] == 100)
        check("a non-posture replaces nothing",
              not posture.record_posture(db, "PC-01", {"oops": True})
              and posture.get_posture(db, "PC-01")["reported_at"] == 100)
        posture.record_posture(db, "PC-02", {"secure_boot": {"state": "on", "error": ""}}, now=200)

        def no_encryption(_machine):
            return {"support": None}

        t = posture.thresholds_from_settings(db)
        check("thresholds come from settings with the CIS defaults",
              t == {"signature_max_age_days": 7, "lock_max_seconds": 900, "admin_allowlist": []})
        summary = posture.fleet_summary(db, ["PC-01", "PC-02", "PC-03"], t, no_encryption)
        rows = {r["id"]: r for r in summary["checks"]}
        check("the summary counts reporting and not-yet-reporting machines",
              summary["reporting"] == 2 and summary["not_reported"] == 1)
        check("...and names the machine failing the firewall",
              rows["firewall"]["fail"] == 1 and rows["firewall"]["failing"] == ["PC-01"])
        scoped = posture.fleet_summary(db, ["PC-02"], t, no_encryption)
        check("a scoped caller sees only its own machines",
              scoped["reporting"] == 1 and {r["id"]: r for r in scoped["checks"]}["firewall"]["fail"] == 0)
        check("an empty scope is an empty summary, never the fleet",
              posture.fleet_summary(db, [], t, no_encryption)["reporting"] == 0)

        posture.rename_machine(db, "PC-01", "PC-02")
        check("a merge keeps the survivor's own posture",
              posture.get_posture(db, "PC-02")["reported_at"] == 200
              and posture.get_posture(db, "PC-01")["posture"] is None)
        posture.rename_machine(db, "PC-02", "PC-09")
        check("a plain rename moves it", posture.get_posture(db, "PC-09")["reported_at"] == 200)
        posture.forget_machine(db, "PC-09")
        check("forgetting a machine takes its posture with it",
              posture.get_posture(db, "PC-09")["posture"] is None)
    finally:
        # On Windows a closed-by-scope sqlite3 connection still holds the file until the
        # collector runs, and unlink fails with WinError 32 -- which several older modules hit
        # here. Collect first so this one tears down cleanly on both platforms.
        gc.collect()
        os.unlink(db)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
