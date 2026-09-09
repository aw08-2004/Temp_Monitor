"""HTTP-layer test for wipe_web.py (roadmap #23 phase H) -- remote lock and remote wipe.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot -- the same
approach as test_location_web / test_apps_web / test_usage_web.

**The silent failure this file exists to catch is a second door.** `wipe_web` is meant to be the
only way to reach these two command types, and three other paths could otherwise get there:
`/api/fleet/commands` (gated on `issue_commands`, which the whole helpdesk has), a saved
favorite, and a rule -- which is the worst of the three, because a rule fires with nobody
present. Each is refused by name, and each refusal is asserted here rather than trusted, because
a hole in any of them looks exactly like a working feature until the day a device is erased by
somebody who only had a reboot button.

The second is the typed-name confirmation being enforced on the SERVER. The console asks for it
and that is a courtesy; this is the control, and a request that skips it must be refused with
nothing recorded and nothing queued.

The third is the audit row, written BEFORE the command is created. A wipe that is issued and then
fails to be recorded is the one ordering that cannot be reconstructed afterwards: the device is
gone and the trail never mentions it.

And the fourth is the gate split. DOING either of these is `wipe_device`; SEEING that they
happened is `view` plus scope -- because a machine that stopped reporting because somebody
erased it looks exactly like one with a flat battery, and this row is the only thing that can
tell an operator which it was.
"""
import functools
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import capabilities
import fleet
import permissions
import rules
import settings
import wipe
from fleet_web import create_fleet_blueprint
from permissions_web import create_access
from wipe_web import create_wipe_blueprint
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"
SECRET = "hub-enroll-secret"


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
    try:
        fleet.init_fleet_db(db_path)
        wipe.init_wipe_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(
            db_path, "Readers", capabilities=[permissions.VIEW],
            machines=["PHONE-1"], members=["reader@x.com"])
        permissions.create_group(
            db_path, "Securers",
            capabilities=[permissions.VIEW, permissions.WIPE_DEVICE],
            machines=["PHONE-1"], members=["securer@x.com"])
        # Somebody with the ordinary command gate and nothing else -- the whole helpdesk.
        permissions.create_group(
            db_path, "Helpdesk",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PHONE-1"], members=["helpdesk@x.com"])
        settings.invalidate()

        app.register_blueprint(create_wipe_blueprint(db_path, fake_login_required, access))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        # The device says it can do both, which is what stops create_command refusing them.
        capabilities.record_capabilities(db_path, "PHONE-1", {
            "platform": "android",
            "commands": ["rename", "locate_device", "lock_device", "wipe_device"],
            "features": ["device_owner"],
        })

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()

        print("== The capability, which is not issue_commands ==")
        CURRENT_USER = "helpdesk@x.com"
        check("somebody with issue_commands cannot lock",
              c.post("/api/wipe/machines/PHONE-1/lock", json={}).status_code == 403)
        check("...nor wipe",
              c.post("/api/wipe/machines/PHONE-1/wipe",
                     json={"confirm": "PHONE-1"}).status_code == 403)

        print("\n== ...and cannot reach either through the generic command endpoint ==")
        # The assertion this file exists for. This endpoint's gate IS issue_commands.
        for command_type in ("lock_device", "wipe_device"):
            r = c.post("/api/fleet/commands",
                       json={"machine": "PHONE-1", "type": command_type})
            check(f"a hand-rolled {command_type} is refused there",
                  r.status_code == 400 and "wipe_device" in (r.get_json() or {}).get("error", ""))

        print("\n== ...nor save one as a favorite, nor put one in a rule ==")
        for command_type in ("lock_device", "wipe_device"):
            try:
                fleet._validate_favorite("f", command_type, {})
                saved = True
            except ValueError:
                saved = False
            check(f"{command_type} cannot be saved as a favorite", saved is False)
            check(f"...and a rule may not issue {command_type}",
                  command_type not in rules.RULE_ALLOWED_COMMANDS)

        print("\n== Reading is view plus scope, not the wipe capability ==")
        CURRENT_USER = "reader@x.com"
        r = c.get("/api/wipe/machines/PHONE-1")
        check("a reader can see what was done to a machine they administer",
              r.status_code == 200)
        check("...and is told they may not do any of it",
              r.get_json()["can_wipe"] is False)
        check("a machine outside their scope is refused",
              c.get("/api/wipe/machines/PC-01").status_code == 403)

        print("\n== Locking ==")
        CURRENT_USER = "securer@x.com"
        r = c.post("/api/wipe/machines/PHONE-1/lock", json={})
        check("somebody with wipe_device can lock", r.status_code == 202)
        check("...and gets the queued command back", bool(r.get_json().get("command_id")))
        check("...with no typed confirmation asked for, deliberately",
              len(wipe.history(db_path, "PHONE-1")) == 1)
        queued = fleet.list_commands(db_path, machine="PHONE-1")
        check("...and a lock_device is actually on the queue",
              any(cmd["type"] == "lock_device" for cmd in queued))

        print("\n== Wiping needs the machine's name, checked HERE ==")
        for body in ({}, {"confirm": ""}, {"confirm": "phone-1"}, {"confirm": "PHONE-12"}):
            before = len(wipe.history(db_path, "PHONE-1"))
            r = c.post("/api/wipe/machines/PHONE-1/wipe", json=body)
            check(f"a wipe with confirm={body.get('confirm')!r} is refused",
                  r.status_code == 400)
            check("...with nothing recorded and nothing queued",
                  len(wipe.history(db_path, "PHONE-1")) == before)

        r = c.post("/api/wipe/machines/PHONE-1/wipe",
                   json={"confirm": "PHONE-1", "reset_protection": False})
        check("the exact name wipes", r.status_code == 202)
        body = r.get_json()
        check("...and the answer carries the wipe, so the page can say so",
              body["last_wipe"] is not None)
        check("...remembering that reset protection was NOT cleared",
              body["last_wipe"]["reset_protection"] is False)

        queued = [cmd for cmd in fleet.list_commands(db_path, machine="PHONE-1")
                  if cmd["type"] == "wipe_device"]
        check("a wipe_device is on the queue", len(queued) == 1)
        # Read straight from the table: list_commands does not carry params, and the value the
        # DEVICE will act on is the one that matters here.
        with fleet.get_conn(db_path) as conn:
            stored = conn.execute("SELECT params_json FROM commands WHERE id = ?",
                                  (queued[0]["id"],)).fetchone()
        check("...carrying the reset-protection answer to the device",
              json.loads(stored["params_json"]).get("reset_protection") is False)

        print("\n== The audit row exists, at security level ==")
        entries = fleet.list_audit(db_path, limit=50)["entries"]
        actions = [e["action"] for e in entries]
        check("locking is audited", "lock_device" in actions)
        check("wiping is audited", "wipe_device" in actions)
        wiped = [e for e in entries if e["action"] == "wipe_device"][0]
        check("...at security level, with the operator named",
              wiped["level"] == fleet.LEVEL_SECURITY and wiped["actor"] == "securer@x.com")
        check("...naming the machine that was erased", wiped["target"] == "PHONE-1")

        print("\n== A device that says it cannot do this ==")
        capabilities.record_capabilities(db_path, "PC-01", {
            "platform": "windows", "commands": ["restart", "shutdown"], "features": [],
        })
        permissions.create_group(
            db_path, "Both", capabilities=[permissions.VIEW, permissions.WIPE_DEVICE],
            machines=["PHONE-1", "PC-01"], members=["securer@x.com"])
        settings.invalidate()
        r = c.post("/api/wipe/machines/PC-01/lock", json={})
        check("locking a machine that reported it cannot is refused with a reason",
              r.status_code == 400 and "lock_device" in (r.get_json() or {}).get("error", ""))

        print("\n== Content type ==")
        r = c.post("/api/wipe/machines/PHONE-1/wipe", data="confirm=PHONE-1")
        check("a form post is refused rather than parsed", r.status_code == 415)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        settings.invalidate()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
