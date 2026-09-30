"""Tests for the device sheet and its HTTP surface (roadmap #25 A), plus the software half's
routes and heartbeat ingest (#25 B).

Wires the blueprints onto a minimal Flask app, avoiding app.py's OAuth boot -- the same
approach as test_bitlocker_web.py. machine_info is created by hand here with the columns the
sheet reads, because the table belongs to app.py.

**The silent failure this file exists to catch is an empty section that reads as a fact.**
A sheet gets printed and stapled to a ticket, and a Software heading with nothing under it
tells every later reader "this PC has nothing installed". So the tests pin that:

  * a machine whose agent never sent software is `waiting` / `agent_update`, never `ok` with
    an empty list -- and once it reports an empty list, it is `ok` and empty;
  * hardware detail and security posture that NOTHING collects yet are labelled as such
    (phase_c / phase_d), not left blank;
  * the summary CSV writes a BLANK, not a 0, for an unreported count -- a spreadsheet summing
    that column must not count a silent machine as fully patched;
  * every CSV cell that looks like a formula is neutralised, because a software name is text
    any local user can write into their own uninstall key;
  * the selection is scoped by the hub, and an out-of-scope name is dropped silently rather
    than refused (a refusal would be an oracle for which hostnames exist);
  * the sheet leaves off escrow bookkeeping, which its own page gates higher.
"""
import csv
import functools
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import apps
import bios
import bitlocker
import capabilities
import device_groups
import fleet
import location
import patches
import permissions
import remote
import reports
import rules
import settings
import software
import wake
from flask import Flask, session as flask_session
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from reports_web import create_reports_blueprint
from software_web import create_software_blueprint

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def make_machine_info(db, rows):
    with fleet.get_conn(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS machine_info (machine TEXT PRIMARY KEY, asset_tag TEXT, "
            "serial_number TEXT, model TEXT, updated_at TEXT, manufacturer TEXT, "
            "companion_version TEXT, os_caption TEXT, os_build TEXT, ad_ou TEXT, ad_dn TEXT, "
            "ad_owner TEXT, primary_sensor_name TEXT)")
        for row in rows:
            cols = ", ".join(row)
            conn.execute(f"INSERT INTO machine_info ({cols}) VALUES "
                         f"({', '.join('?' for _ in row)})", list(row.values()))


def probe(machine):
    if machine != "PC-01":
        return None
    return {"cpu": "Intel Core i5-8500", "gpu": None, "memory_gb": 15.9,
            "volumes": [{"name": "C:", "used_gb": 100.0, "total_gb": 237.0, "used_pct": 42.2}],
            "reported_at": None}


def parse_csv(response):
    text = response.get_data(as_text=True)
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def main():
    global CURRENT_USER
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        for init in (fleet.init_fleet_db, bios.init_bios_db, bitlocker.init_bitlocker_db,
                     wake.init_wake_db, apps.init_apps_db, patches.init_patches_db,
                     remote.init_remote_db, capabilities.init_capabilities_db,
                     location.init_location_db, rules.init_rules_db,
                     device_groups.init_device_groups_db, software.init_software_db,
                     permissions.init_permissions_db, settings.init_settings_db):
            init(db)
        settings.invalidate()
        make_machine_info(db, [
            {"machine": "PC-01", "model": "OptiPlex 7070", "manufacturer": "Dell Inc.",
             "serial_number": "=HYPERLINK(\"http://x\")", "os_caption": "Windows 11 Pro",
             "os_build": "22631", "companion_version": "3.37.1", "updated_at": "2026-09-30 10:00",
             "ad_ou": "OU=Office", "primary_sensor_name": "internal-config"},
            {"machine": "PC-02", "model": "ThinkPad", "updated_at": "2026-09-29 09:00"},
            {"machine": "PC-SECRET", "model": "Server"},
        ])

        SECRET = "hub-enroll-secret"
        one_id, one_token = fleet.enroll_agent(db, "PC-01", SECRET, SECRET)
        one_auth = {"Authorization": f"Bearer {one_id}:{one_token}"}

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db, {"super@x.com"})
        permissions.create_group(db, "Techs", capabilities=[permissions.VIEW],
                                 machines=["PC-01", "PC-02"], members=["tech@x.com"])
        settings.invalidate()
        app.register_blueprint(create_fleet_blueprint(db, SECRET, fake_login_required, access))
        app.register_blueprint(create_software_blueprint(db, fake_login_required, access))
        app.register_blueprint(create_reports_blueprint(db, fake_login_required, access,
                                                        hardware_probe=probe))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()

        print("\n== Every section id has a translated title ==")
        catalog = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              "..", "hub", "locales", "en.json"),
                                 encoding="utf-8"))
        check("report.section.<id> exists for every reports.SECTIONS id",
              all(s in catalog["report"]["section"] for s in reports.SECTIONS))

        print("\n== A machine before its agent reports anything ==")
        sheet = reports.build_sheet(db, "PC-02")
        sec = sheet["sections"]
        check("identity is there from machine_info", sec["identity"]["status"] == "ok"
              and sec["identity"]["data"]["model"] == "ThinkPad")
        check("software is WAITING for an agent update -- not ok-and-empty",
              sec["software"]["status"] == "waiting"
              and sec["software"]["waiting_for"] == "agent_update")
        check("network is waiting on a check-in", sec["network"]["waiting_for"] == "agent_report")
        check("an absent Android inventory is omitted, not 'waiting'", "apps" not in sec)
        check("the print order is the SECTIONS order, since jsonify sorts keys",
              sheet["order"] == [s for s in reports.SECTIONS if s in sec])
        check("an unknown machine has no sheet", reports.build_sheet(db, "NOPE") is None)
        check("internal machine_info columns are not printed",
              "primary_sensor_name" not in json.dumps(reports.build_sheet(db, "PC-01")))

        print("\n== The heartbeat carries software, and the sheet shows it ==")
        r = c.post("/api/agent/heartbeat", json={"config_version": 0, "software": {
            "software": [{"id": "HKLM32:{A}", "name": "=cmd|' /C calc'!A0", "version": "1",
                          "publisher": "Evil"},
                         {"id": "HKLM64:{B}", "name": "7-Zip", "version": "23.01",
                          "publisher": "Igor Pavlov", "arch": "x64"}],
            "error": ""}}, headers=one_auth)
        check("heartbeat -> 200", r.status_code == 200)
        for junk in ("x", 5, {"software": "nope"}, {"software": [1]}):
            r = c.post("/api/agent/heartbeat", json={"config_version": 0, "software": junk},
                       headers=one_auth)
            check(f"a malformed software block {junk!r:.24} still heartbeats", r.status_code == 200)
        check("...and replaced nothing", software.get_inventory(db, "PC-01")["count"] == 2)

        sheet = c.get("/api/reports/machines/PC-01").get_json()
        sec = sheet["sections"]
        check("software is ok with both programs", sec["software"]["status"] == "ok"
              and len(sec["software"]["data"]["items"]) == 2)
        check("hardware carries the probe's CPU", sec["hardware"]["data"]["cpu"]
              == "Intel Core i5-8500")
        check("...and says DIMMs and friends are not collected yet (phase C), not blank",
              sec["hardware"]["data"]["detail_waiting_for"] == "phase_c")
        check("volumes come from the probe", sec["volumes"]["data"][0]["name"] == "C:")

        c.post("/api/agent/heartbeat", json={"config_version": 0, "bitlocker": {
            "support": "supported", "volumes": [{
                "mount": "C:", "protection": "on", "conversion": "fully_encrypted",
                "method": "XTS-AES 128",
                "protectors": [{"id": "{REC}", "kind": "recovery_password"}]}]}},
            headers=one_auth)
        sec = c.get("/api/reports/machines/PC-01").get_json()["sections"]
        check("security shows posture", sec["security"]["data"]["volumes"][0]["protection"] == "on")
        check("...and whether a recovery password exists",
              sec["security"]["data"]["volumes"][0]["has_recovery_password"] is True)
        check("...but no protector ids or escrow bookkeeping",
              "{REC}" not in json.dumps(sec["security"]) and "read_count" not in json.dumps(sec))
        check("...and names antivirus/firewall as phase D, not blank",
              sec["security"]["data"]["posture_waiting_for"] == "phase_d")

        print("\n== An empty software report is ok-and-empty, not waiting ==")
        c.post("/api/agent/heartbeat", json={"config_version": 0, "software": {"software": []}},
               headers=one_auth)
        sec = reports.build_sheet(db, "PC-01")["sections"]
        check("status ok, zero items", sec["software"]["status"] == "ok"
              and sec["software"]["data"]["items"] == [])
        c.post("/api/agent/heartbeat", json={"config_version": 0, "software": {"software": [
            {"id": "a", "name": "=cmd|' /C calc'!A0", "version": "1"},
            {"id": "b", "name": "7-Zip", "version": "23.01"}]}}, headers=one_auth)

        print("\n== Patches: fully patched is not 'never scanned' ==")
        check("before any scan, patches wait", reports.build_sheet(db, "PC-01")["sections"]
              ["patches"]["status"] == "waiting")
        c.post("/api/agent/heartbeat", json={"config_version": 0, "patches": {"updates": []}},
               headers=one_auth)
        sec = reports.build_sheet(db, "PC-01")["sections"]["patches"]
        check("an empty scan is ok-and-empty -- a fully patched PC",
              sec["status"] == "ok" and sec["data"] == [] and sec["reported_at"])
        row = reports.summary_row(reports.build_sheet(db, "PC-01"))
        check("...and its summary count is 0, not blank", row["pending_patches"] == 0)

        print("\n== Groups resolve live ==")
        device_groups.save_group(db, {"name": "Office", "target": {
            "include": [{"kind": "machines", "machines": ["PC-01"]}], "exclude": []}})
        check("PC-01 is in Office", reports.build_sheet(db, "PC-01")["sections"]["groups"]["data"]
              == ["Office"])

        print("\n== Scope ==")
        CURRENT_USER = "tech@x.com"
        check("an in-scope sheet is readable", c.get("/api/reports/machines/PC-02").status_code == 200)
        check("an out-of-scope sheet is refused",
              c.get("/api/reports/machines/PC-SECRET").status_code == 403)
        check("...and so is its JSON download",
              c.get("/api/reports/machines/PC-SECRET?download=1").status_code == 403)
        rows = c.get("/api/reports/fleet").get_json()["rows"]
        check("the whole-fleet selection is only what the caller can see",
              sorted(r["machine"] for r in rows) == ["PC-01", "PC-02"])
        rows = c.get("/api/reports/fleet?machines=PC-02,PC-SECRET,nope").get_json()["rows"]
        check("named machines out of scope (or unknown) are dropped silently, not refused",
              [r["machine"] for r in rows] == ["PC-02"])
        group_id = device_groups.list_groups(db)[0]["id"]
        rows = c.get(f"/api/reports/fleet?group={group_id}").get_json()["rows"]
        check("a device group selects its members", [r["machine"] for r in rows] == ["PC-01"])
        check("a non-numeric group is a 400", c.get("/api/reports/fleet?group=x").status_code == 400)
        check("the software catalog is scoped",
              {r["name"] for r in c.get("/api/software/catalog").get_json()["catalog"]}
              == {"=cmd|' /C calc'!A0", "7-Zip"})
        check("an out-of-scope machine's software is refused",
              c.get("/api/software/machines/PC-SECRET").status_code == 403)
        settings.set_many(db, {"security.genai_watchlist": ["7-Zip"]})
        settings.invalidate()
        body = c.get("/api/software/genai").get_json()
        check("shadow-AI findings match the watch list, and say what was searched for",
              [(f["machine"], f["name"]) for f in body["findings"]] == [("PC-01", "7-Zip")]
              and body["patterns"] == ["7-zip"])
        CURRENT_USER = "nobody@x.com"
        check("no view, no shadow-AI findings", c.get("/api/software/genai").status_code == 403)
        check("no view capability, no fleet report", c.get("/api/reports/fleet").status_code == 403)
        check("...and no export", c.get("/api/reports/export.csv").status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== CSV exports ==")
        r = c.get("/api/reports/export.csv?section=summary")
        check("summary -> 200 with a download name", r.status_code == 200
              and "attachment" in r.headers.get("Content-Disposition", ""))
        check("...UTF-8 with a BOM for Excel", r.get_data(as_text=True).startswith("﻿"))
        table = parse_csv(r)
        header = table[0]
        check("header is the field names", tuple(header) == reports.SUMMARY_FIELDS)
        by_machine = {row[0]: dict(zip(header, row)) for row in table[1:]}
        check("the formula-looking serial is neutralised",
              by_machine["PC-01"]["serial_number"].startswith("'="))
        check("an unreported software count is BLANK, not 0",
              by_machine["PC-02"]["software_count"] == "")
        check("...and an unreported patch count too", by_machine["PC-02"]["pending_patches"] == "")
        check("a reported count is a number", by_machine["PC-01"]["software_count"] == "2")

        table = parse_csv(c.get("/api/reports/export.csv?section=software"))
        names = {row[1] for row in table[1:]}
        check("the software file has one row per program", len(table) == 3)
        check("...and the hostile name is neutralised", "'=cmd|' /C calc'!A0" in names)
        check("an unknown section is a 400",
              c.get("/api/reports/export.csv?section=passwords").status_code == 400)

        r = c.get("/api/reports/machines/PC-01?download=1")
        check("per-machine JSON download", r.status_code == 200
              and "fleethub-PC-01-" in r.headers.get("Content-Disposition", ""))
        body = json.loads(r.get_data(as_text=True))
        check("...carries the sheet", body["sheets"][0]["machine"] == "PC-01")
        r = c.get("/api/reports/export.json?machines=PC-01,PC-02")
        check("fleet JSON export carries every selected sheet",
              [s["machine"] for s in json.loads(r.get_data(as_text=True))["sheets"]]
              == ["PC-01", "PC-02"])

        print("\n== neutralise_cell ==")
        check("numbers are left alone, including negatives",
              reports.neutralise_cell(-3) == "-3" and reports.neutralise_cell(1.5) == "1.5")
        check("booleans are words", reports.neutralise_cell(True) == "true")
        check("None is blank", reports.neutralise_cell(None) == "")
        for lead in ("=", "+", "-", "@", "\t", "\r"):
            check(f"a leading {lead!r} is prefixed", reports.neutralise_cell(lead + "x")[0] == "'")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db + suffix)
            except OSError:
                pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
