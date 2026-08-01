"""HTTP-layer test for bios_web.py (roadmap #9), plus the heartbeat's `bios` ingest.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot --
same approach as test_remote_web / test_fleet_web.

Two things are worth stating about what is asserted here:

  * **Reading is `view`, forcing a re-read is `issue_commands`, writing is
    `manage_firmware`.** A viewer must be able to answer "is Wake-on-LAN on?" without an
    admin, and must not be able to make a machine do anything. All three are checked, in both
    directions -- and the write gate is checked against a tech who holds `issue_commands`,
    since the whole point of a separate capability is that the two do not come together.
  * **A malformed `bios` block must not fail a heartbeat.** A 500 there marks the machine
    offline fleet-wide, which is a far worse outcome than a stale Firmware tab.
  * **The BIOS setup password must never appear in an audit row.** It is the reason the
    command carries a change id instead of the attribute list, so the audit trail is scanned
    for it rather than assumed clean.
"""
import functools
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import backups
import bios
import firmware
import fleet
import permissions
import settings
from bios_web import create_bios_blueprint
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

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


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    log_dir = tempfile.mkdtemp(prefix="fleethub-bios-")
    # The BIOS setup password rides the backup secret store, which needs a master key. Set
    # here rather than mocked, so what the test exercises is the real encrypt/decrypt path.
    os.environ["BACKUP_MASTER_KEY"] = backups.generate_master_key()
    try:
        fleet.init_fleet_db(db_path)
        bios.init_bios_db(db_path)
        firmware.init_firmware_db(db_path)
        # firmware.read_machine_facts reads machine_info, which app.init_db() owns and this
        # minimal app does not boot. Created here with just the two columns a precondition
        # check needs -- manufacturer and model ARE the check.
        import sqlite3
        with sqlite3.connect(db_path) as _c:
            _c.execute("CREATE TABLE IF NOT EXISTS machine_info (machine TEXT PRIMARY KEY, "
                       "manufacturer TEXT, model TEXT)")
            _c.execute("INSERT OR REPLACE INTO machine_info VALUES ('PC-01', 'Dell', "
                       "'Latitude 5540')")
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        # A tech who may act on PC-01 only.
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PC-01"], members=["tech@x.com"])
        # A viewer scoped to PC-01 with no command rights at all.
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-01"], members=["viewer@x.com"])
        # Holds manage_firmware and nothing else that would let them issue a command by
        # another route -- the point being that the write gate stands on its own.
        permissions.create_group(
            db_path, "Firmware", capabilities=[permissions.VIEW, permissions.MANAGE_FIRMWARE],
            machines=["PC-01"], members=["fw@x.com"])
        settings.invalidate()

        agent_id, token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        app.register_blueprint(
            create_bios_blueprint(db_path, log_dir, fake_login_required, access,
                                  hub_url="https://hub.example"))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        auth = {"Authorization": f"Bearer {agent_id}:{token}"}

        print("\n== A machine that has never reported ==")
        r = c.get("/api/bios/PC-01")
        check("GET -> 200", r.status_code == 200)
        check("support is null, not 'unsupported'", r.get_json()["support"] is None)

        print("\n== The heartbeat carries the inventory ==")
        r = c.post("/api/agent/heartbeat", json={
            "config_version": 0,
            "bios": {
                "support": "supported", "vendor": "Dell",
                "interface": r"root\dcim\sysman", "bios_version": "1.29.0",
                "password_set": False,
                "settings": [{"name": "WakeOnLan", "value": "LanOnly", "kind": "enum",
                              "possible_values": ["Disabled", "LanOnly"],
                              "read_only": False, "display_name": "Wake on LAN"}],
            },
        }, headers=auth)
        check("heartbeat -> 200", r.status_code == 200)
        body = c.get("/api/bios/PC-01").get_json()
        check("the report is readable through the console API",
              body["support"] == "supported" and body["vendor"] == "Dell")
        check("the attribute survived the round trip",
              body["settings"][0]["name"] == "WakeOnLan")
        check("password_set false is distinct from unknown", body["password_set"] is False)

        print("\n== A malformed report must never fail a heartbeat ==")
        for junk in ("not a dict", 7, {"support": "nonsense"}, {"settings": [1, 2, 3]}):
            r = c.post("/api/agent/heartbeat",
                       json={"config_version": 0, "bios": junk}, headers=auth)
            check(f"heartbeat with bios={junk!r} still 200", r.status_code == 200)
        # A heartbeat with no bios block at all must leave the last good report alone --
        # otherwise every 10 s heartbeat from an agent between scans would blank the tab.
        c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth)
        check("a heartbeat without a bios block leaves the stored report intact",
              c.get("/api/bios/PC-01").get_json()["support"] in ("supported", "error"))

        print("\n== Re-read is a real command ==")
        r = c.post("/api/bios/PC-01/refresh", json={})
        check("refresh -> 202", r.status_code == 202)
        check("a refresh_bios_inventory command was queued",
              any(cmd["type"] == "refresh_bios_inventory"
                  for cmd in fleet.claim_commands(db_path, agent_id, "PC-01")))

        print("\n== Capability + scope ==")
        CURRENT_USER = "viewer@x.com"
        check("a viewer may READ the firmware inventory",
              c.get("/api/bios/PC-01").status_code == 200)
        check("a viewer may NOT force a re-read -> 403",
              c.post("/api/bios/PC-01/refresh", json={}).status_code == 403)

        CURRENT_USER = "tech@x.com"
        check("a scoped tech may re-read their own machine",
              c.post("/api/bios/PC-01/refresh", json={}).status_code == 202)
        check("out-of-scope read -> 403", c.get("/api/bios/PC-09").status_code == 403)
        check("out-of-scope re-read -> 403",
              c.post("/api/bios/PC-09/refresh", json={}).status_code == 403)

        CURRENT_USER = "nobody@x.com"
        check("a stranger cannot read the inventory",
              c.get("/api/bios/PC-01").status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== Writing a setting is manage_firmware, not issue_commands ==")
        # Restore a known-good inventory: the malformed-report loop above deliberately left
        # the machine in `error`, and a write needs something to validate against.
        good = {
            "support": "supported", "vendor": "Dell", "interface": r"root\dcim\sysman",
            "bios_version": "1.29.0", "password_set": True,
            "settings": [
                {"name": "WakeOnLan", "value": "Disabled", "kind": "enum",
                 "possible_values": ["Disabled", "LanOnly"], "read_only": False},
                {"name": "AssetTag", "value": "A-1", "kind": "string", "read_only": False},
                {"name": "SecureBoot", "value": "Enabled", "kind": "enum",
                 "possible_values": ["Enabled", "Disabled"], "read_only": True},
            ],
        }
        c.post("/api/agent/heartbeat", json={"config_version": 0, "bios": good}, headers=auth)

        CURRENT_USER = "tech@x.com"
        r = c.post("/api/bios/PC-01/settings",
                   json={"changes": [{"name": "WakeOnLan", "value": "LanOnly"}]})
        check("issue_commands alone does NOT allow a firmware write -> 403",
              r.status_code == 403)

        CURRENT_USER = "fw@x.com"
        check("a read-only attribute is refused -> 400",
              c.post("/api/bios/PC-01/settings",
                     json={"changes": [{"name": "SecureBoot", "value": "Disabled"}]}
                     ).status_code == 400)
        check("a value the machine does not accept is refused -> 400",
              c.post("/api/bios/PC-01/settings",
                     json={"changes": [{"name": "WakeOnLan", "value": "Sometimes"}]}
                     ).status_code == 400)
        check("an attribute the machine never reported is refused -> 400",
              c.post("/api/bios/PC-01/settings",
                     json={"changes": [{"name": "Invented", "value": "x"}]}
                     ).status_code == 400)
        check("a no-op is refused -> 400",
              c.post("/api/bios/PC-01/settings",
                     json={"changes": [{"name": "WakeOnLan", "value": "Disabled"}]}
                     ).status_code == 400)
        check("out-of-scope write -> 403",
              c.post("/api/bios/PC-09/settings",
                     json={"changes": [{"name": "WakeOnLan", "value": "LanOnly"}]}
                     ).status_code == 403)

        r = c.post("/api/bios/PC-01/settings",
                   json={"changes": [{"name": "WakeOnLan", "value": "LanOnly"},
                                     {"name": "AssetTag", "value": "A-2"}]})
        check("a valid change -> 202", r.status_code == 202)
        change_id = r.get_json()["change_id"]
        check("a second change while one is in flight -> 409",
              c.post("/api/bios/PC-01/settings",
                     json={"changes": [{"name": "AssetTag", "value": "A-3"}]}
                     ).status_code == 409)

        print("\n== The command carries an id, never the password or the values ==")
        queued = [cmd for cmd in fleet.claim_commands(db_path, agent_id, "PC-01")
                  if cmd["type"] == "set_bios_settings"]
        check("a set_bios_settings command was queued", len(queued) == 1)
        check("its params are only the change id",
              queued and set(queued[0]["params"]) == {"change_id"})

        print("\n== The agent fetches the payload, once ==")
        r = c.get(f"/api/agent/bios/change/{change_id}", headers=auth)
        check("the agent may fetch its own change -> 200", r.status_code == 200)
        payload = r.get_json()
        check("the payload carries both attributes", len(payload["changes"]) == 2)
        check("no password is stored yet, so none is sent", payload["password"] is None)
        check("a second fetch is refused, so a redelivered command cannot replay writes",
              c.get(f"/api/agent/bios/change/{change_id}", headers=auth).status_code == 409)
        check("an unauthenticated fetch -> 401",
              c.get(f"/api/agent/bios/change/{change_id}").status_code == 401)

        print("\n== Verification: the re-read decides, not the exit code ==")
        c.post(f"/api/agent/bios/change/{change_id}/result", headers=auth, json={
            # WakeOnLan came back as asked -> applied. AssetTag came back as it was, with no
            # error -> the write is waiting for POST, which is the ONLY thing that should ever
            # produce a "restart to apply" in the console.
            "items": [{"name": "WakeOnLan", "observed": "LanOnly", "error": ""},
                      {"name": "AssetTag", "observed": "A-1", "error": ""}],
            "bios": dict(good, settings=[
                dict(good["settings"][0], value="LanOnly"),
                good["settings"][1], good["settings"][2]]),
        })
        CURRENT_USER = "fw@x.com"
        body = c.get("/api/bios/PC-01").get_json()
        change = body["changes"][0]
        # Applied + pending_reboot rolls up to pending_reboot, NOT partial: nothing went
        # wrong, one attribute simply needs a POST. `partial` is reserved for a change where
        # something did not take, so that an operator who sees it always has to look.
        check("applied + pending_reboot rolls up to pending_reboot",
              change["status"] == bios.CHANGE_PENDING_REBOOT)
        outcomes = {row["name"]: row["outcome"] for row in change["results"]}
        check("the attribute that came back changed is applied",
              outcomes["WakeOnLan"] == bios.OUTCOME_APPLIED)
        check("the attribute still reporting its old value is pending_reboot",
              outcomes["AssetTag"] == bios.OUTCOME_PENDING_REBOOT)
        check("the inventory that rode along was stored too",
              body["settings"][2]["value"] == "LanOnly")
        check("a resolved change no longer blocks the next one",
              c.post("/api/bios/PC-01/settings",
                     json={"changes": [{"name": "AssetTag", "value": "A-9"},
                                       {"name": "WakeOnLan", "value": "Disabled"}]}
                     ).status_code == 202)

        # One attribute refused, one applied. THIS is what `partial` is for, and the whole
        # reason it is not folded into `failed`: the operator has to see which one.
        pending = bios.open_change_for(db_path, "PC-01")
        c.get(f"/api/agent/bios/change/{pending['id']}", headers=auth)
        c.post(f"/api/agent/bios/change/{pending['id']}/result", headers=auth, json={
            "items": [{"name": "AssetTag", "observed": "A-9", "error": ""},
                      {"name": "WakeOnLan", "observed": "LanOnly",
                       "error": "the firmware refused the write (code 5)"}],
        })
        change = c.get("/api/bios/PC-01").get_json()["changes"][0]
        check("one failure among successes is `partial`",
              change["status"] == bios.CHANGE_PARTIAL)
        outcomes = {row["name"]: row["outcome"] for row in change["results"]}
        check("the refused attribute is failed, and says why",
              outcomes["WakeOnLan"] == bios.OUTCOME_FAILED)
        check("the other attribute is still reported applied on its own merits",
              outcomes["AssetTag"] == bios.OUTCOME_APPLIED)

        print("\n== The setup password: stored, sent, never read back ==")
        check("PUT with no password -> 400",
              c.put("/api/bios-password", json={}).status_code == 400)
        check("storing a fleet password -> 200",
              c.put("/api/bios-password", json={"password": "fleet-pw"}).status_code == 200)
        check("the console reports only THAT one is stored",
              c.get("/api/bios/PC-01").get_json()["password_stored"] is True)
        check("no route hands a password back",
              c.get("/api/bios-password").status_code in (404, 405))

        def queue_and_fetch(value):
            """Queue a change and take the agent's view of it. A fresh change each time,
            because a fetched one is already RUNNING and will not be handed over twice."""
            c.post("/api/bios/PC-01/settings",
                   json={"changes": [{"name": "AssetTag", "value": value}]})
            open_change = bios.open_change_for(db_path, "PC-01")
            fetched = c.get(f"/api/agent/bios/change/{open_change['id']}",
                            headers=auth).get_json()
            c.post(f"/api/agent/bios/change/{open_change['id']}/result", headers=auth,
                   json={"items": [{"name": "AssetTag", "observed": value, "error": ""}]})
            return fetched

        check("the fleet password is sent to the machine",
              queue_and_fetch("A-20")["password"] == "fleet-pw")
        c.put("/api/bios-password/PC-01", json={"password": "machine-pw"})
        check("a per-machine password overrides the fleet one",
              queue_and_fetch("A-21")["password"] == "machine-pw")
        c.delete("/api/bios-password/PC-01")
        check("clearing the override falls back to the fleet password",
              queue_and_fetch("A-22")["password"] == "fleet-pw")

        print("\n== The password is nowhere in the audit trail ==")
        # The reason the command carries a change id rather than the attribute list: audit
        # rows store params verbatim, and this is the assertion that keeps it that way.
        blob = json.dumps(fleet.list_audit(db_path, limit=500)["entries"], default=str)
        check("no stored password appears in any audit row",
              "fleet-pw" not in blob and "machine-pw" not in blob)
        check("but WHAT changed is audited, not just THAT something did",
              "WakeOnLan" in blob and "bios_settings_change" in blob)

        CURRENT_USER = "tech@x.com"
        check("issue_commands does not allow storing a password -> 403",
              c.put("/api/bios-password", json={"password": "x"}).status_code == 403)
        CURRENT_USER = "super@x.com"

        # ================================================================
        # FIRMWARE UPDATES (`update_bios`)
        # ================================================================
        print("\n== Uploading an image computes the digest hub-side ==")
        image = b"MZ" + b"\x00" * 4096
        r = c.post("/api/firmware/upload", content_type="multipart/form-data",
                   data={"file": (io.BytesIO(image), "L5540_1.31.0.exe")})
        check("upload -> 201", r.status_code == 201)
        uploaded = r.get_json()
        check("the hub hashes the bytes it actually stored",
              uploaded["sha256"] == hashlib.sha256(image).hexdigest())
        check("...and reports the size", uploaded["file_size"] == len(image))

        r = c.post("/api/firmware/payloads", json={
            "name": "Latitude 5540 BIOS 1.31.0", "vendor": "Dell",
            "models": ["Latitude 5540"], "to_version": "1.31.0",
            "sha256": uploaded["sha256"], "file_size": uploaded["file_size"],
            "file_name": uploaded["file_name"], "install_args": "/s /f"})
        check("creating the payload -> 201", r.status_code == 201)
        payload_id = r.get_json()["payload"]["id"]
        check("a payload with no models is refused -> 400",
              c.post("/api/firmware/payloads",
                     json={"name": "x", "vendor": "Dell", "models": [],
                           "to_version": "1.0", "sha256": uploaded["sha256"]}
                     ).status_code == 400)

        print("\n== Queueing a flash ==")
        r = c.post("/api/firmware/jobs", json={"payload_id": payload_id,
                                               "machines": ["PC-01"]})
        # 202, never 200: nothing has touched any firmware and will not until the scheduler
        # dispatches. The console says *queued*.
        check("create job -> 202", r.status_code == 202)
        job_body = r.get_json()
        check("the machine is queued", job_body["queued"] == ["PC-01"])
        job_id = job_body["job_id"]

        r = c.post("/api/firmware/jobs", json={"payload_id": payload_id,
                                               "machines": ["PC-01", "PC-GHOST"]})
        # Named back rather than silently dropped: "queued 1 of 2" with no word on the
        # other is how somebody believes a fleet was updated when it was not.
        check("a machine that cannot take the image is REFUSED and named",
              [x["machine"] for x in r.get_json()["refused"]] == ["PC-GHOST"])
        check("...with a reason", bool(r.get_json()["refused"][0]["reason"]))

        print("\n== The agent fetches everything the params do not carry ==")
        firmware.dispatch_once(db_path)
        target = firmware.get_job(db_path, job_id)["targets"][0]
        r = c.get(f"/api/agent/firmware/update/{target['id']}", headers=auth)
        check("agent fetch -> 200", r.status_code == 200)
        fetched = r.get_json()
        check("the image URL is built from the hub's public address",
              fetched["url"].startswith("https://hub.example/api/agent/firmware/image/"))
        check("the digest rides along so the agent can verify before flashing",
              fetched["sha256"] == uploaded["sha256"])
        check("the BIOS setup password is handed over here, never in params",
              fetched["password"] == "fleet-pw")
        check("the power preconditions ride down with it",
              fetched["require_ac_power"] is True
              and isinstance(fetched["min_battery_percent"], int))
        check("a redelivered command cannot fetch it twice -> 409",
              c.get(f"/api/agent/firmware/update/{target['id']}",
                    headers=auth).status_code == 409)
        check("an unauthenticated agent gets 401",
              c.get(f"/api/agent/firmware/update/{target['id']}").status_code == 401)

        r = c.get(f"/api/agent/firmware/image/{uploaded['sha256']}", headers=auth)
        check("the image downloads to an enrolled agent", r.status_code == 200
              and r.data == image)
        check("a digest belonging to no payload is not readable -> 404",
              c.get("/api/agent/firmware/image/" + "c" * 64,
                    headers=auth).status_code == 404)

        print("\n== The flash is confirmed by the heartbeat, not by the agent ==")
        r = c.post(f"/api/agent/firmware/update/{target['id']}/result",
                   json={"ok": True}, headers=auth)
        check("a successful report only reaches 'rebooting'",
              r.get_json()["status"] == firmware.TARGET_REBOOTING)
        c.post("/api/agent/heartbeat", json={
            "config_version": 0,
            "bios": dict(good, bios_version="1.31.0"),
        }, headers=auth)
        check("the machine coming back on the new version is what applies it",
              firmware.get_target(db_path, target["id"])["status"]
              == firmware.TARGET_APPLIED)

        print("\n== Firmware updates are manage_firmware, all of them ==")
        CURRENT_USER = "tech@x.com"
        check("issue_commands cannot list images -> 403",
              c.get("/api/firmware/payloads").status_code == 403)
        check("issue_commands cannot queue a flash -> 403",
              c.post("/api/firmware/jobs",
                     json={"payload_id": payload_id,
                           "machines": ["PC-01"]}).status_code == 403)
        check("issue_commands cannot upload an image -> 403",
              c.post("/api/firmware/upload", content_type="multipart/form-data",
                     data={"file": (io.BytesIO(b"x"), "x.exe")}).status_code == 403)
        CURRENT_USER = "viewer@x.com"
        check("a viewer cannot see the image library either -> 403",
              c.get("/api/firmware/payloads").status_code == 403)

        CURRENT_USER = "fw@x.com"
        check("manage_firmware CAN list images",
              c.get("/api/firmware/payloads").status_code == 200)
        check("...and is still bound by machine scope -> 403",
              c.post("/api/firmware/jobs",
                     json={"payload_id": payload_id,
                           "machines": ["PC-09"]}).status_code == 403)
        CURRENT_USER = "super@x.com"

        print("\n== Cancel says which of the three things happened ==")
        # A SECOND image, because PC-01 is now on 1.31.0 and the hub refuses a flash to the
        # version a machine is already running -- correctly, since such a flash could never
        # be confirmed. Re-using the first payload here would produce refused targets and a
        # 409 for the wrong reason.
        image2 = b"MZ" + b"\x01" * 2048
        up2 = c.post("/api/firmware/upload", content_type="multipart/form-data",
                     data={"file": (io.BytesIO(image2), "L5540_1.32.0.exe")}).get_json()
        payload2 = c.post("/api/firmware/payloads", json={
            "name": "Latitude 5540 BIOS 1.32.0", "vendor": "Dell",
            "models": ["Latitude 5540"], "to_version": "1.32.0",
            "sha256": up2["sha256"]}).get_json()["payload"]["id"]

        r = c.post("/api/firmware/jobs", json={"payload_id": payload2,
                                               "machines": ["PC-01"]})
        check("a machine already on the target version is refused, a different one is not",
              r.get_json()["queued"] == ["PC-01"])
        pending_job = r.get_json()["job_id"]
        pending_target = firmware.get_job(db_path, pending_job)["targets"][0]["id"]
        check("a queued machine cancels -> 200",
              c.post(f"/api/firmware/updates/{pending_target}/cancel",
                     json={}).status_code == 200)

        r = c.post("/api/firmware/jobs", json={"payload_id": payload2,
                                               "machines": ["PC-01"]})
        held_job = r.get_json()["job_id"]
        firmware.dispatch_once(db_path)
        held = firmware.get_job(db_path, held_job)["targets"][0]["id"]
        c.get(f"/api/agent/firmware/update/{held}", headers=auth)
        r = c.post(f"/api/firmware/updates/{held}/cancel", json={})
        # Claiming is at-most-once and there is no back-channel; the firmware may already be
        # written. A 200 here would be the console lying at the worst possible moment.
        check("a machine already holding the image cannot be recalled -> 409",
              r.status_code == 409)
        check("...and the 409 says the firmware may already have been written",
              "already" in r.get_json()["error"])

        print("\n== Nothing in the audit trail exposes the image URL or the password ==")
        blob = json.dumps(fleet.list_audit(db_path, limit=500)["entries"], default=str)
        check("no stored password in any firmware audit row",
              "fleet-pw" not in blob and "machine-pw" not in blob)
        check("but WHICH image was aimed WHERE is audited",
              "create_firmware_job" in blob and "Latitude 5540 BIOS 1.31.0" in blob)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
