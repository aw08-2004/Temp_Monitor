"""HTTP-layer test for bitlocker_web.py (roadmap #19), plus the heartbeat's `bitlocker`
ingest and its `bitlocker_escrow_wanted` reply.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot -- same
approach as test_bios_web / test_wake_web / test_fleet_web.

What is worth stating about the assertions here, because each is a way this feature fails
quietly rather than loudly:

  * **Reading the posture is `view`; reading a KEY is `read_recovery_keys`.** The test that
    matters is that an operator who can see a machine can tell whether it is encrypted and
    cannot be handed its recovery password. A capability that silently granted the second with
    the first would look identical in the console until somebody read the audit log.
  * **A reveal writes a security-level audit row.** Escrow is only worth having if taking a key
    back out leaves a record naming who took it; an unaudited reveal is a shared password.
  * **An agent files keys only for ids the HUB asked for, and only its own.** Both halves are
    asserted: an invented id is refused, and one enrolled machine cannot file against another's
    name -- which would mean overwriting the custody record of a PC it has nothing to do with.
  * **A malformed `bitlocker` block must not fail a heartbeat.** A 500 there marks the machine
    offline fleet-wide, which is far worse than a stale encryption card.
  * **With escrow switched off the hub asks for nothing** and keeps what it already has. A
    checkbox that deleted the fleet's recovery keys would be the most destructive control in
    this console.
"""
import functools
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import backups
import bitlocker
import fleet
import permissions
import settings
import bitlocker_web
from bitlocker_web import create_bitlocker_blueprint
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


def posture(protector_id="{REC-1}", kind="recovery_password"):
    return {
        "support": "supported",
        "error": "",
        "volumes": [{
            "mount": "C:", "device_id": r"\\?\Volume{1}", "protection": "on",
            "conversion": "fully_encrypted", "percentage": 100, "method": "XTS-AES 128",
            "protectors": [{"id": protector_id, "kind": kind, "label": ""}],
        }],
    }


def audit_actions(db_path):
    with fleet.get_conn(db_path) as conn:
        return [(r["action"], r["actor"], r["level"]) for r in conn.execute(
            "SELECT action, actor, level FROM audit_log").fetchall()]


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    log_dir = tempfile.mkdtemp()
    try:
        fleet.init_fleet_db(db_path)
        bitlocker.init_bitlocker_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        # The master key is what wraps the sidecar store. Set in the environment because that
        # is where the hub reads it from -- and because "no master key" is itself a state this
        # file asserts on below.
        os.environ["BACKUP_MASTER_KEY"] = backups.generate_master_key()

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(
            db_path, "Techs", capabilities=[permissions.VIEW],
            machines=["PC-01", "PC-02"], members=["tech@x.com"])
        permissions.create_group(
            db_path, "Recovery",
            capabilities=[permissions.VIEW, permissions.READ_RECOVERY_KEYS],
            machines=["PC-01"], members=["recover@x.com"])
        settings.invalidate()

        one_id, one_token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        two_id, two_token = fleet.enroll_agent(db_path, "PC-02", SECRET, SECRET)

        app.register_blueprint(create_bitlocker_blueprint(
            db_path, log_dir, fake_login_required, access))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        one_auth = {"Authorization": f"Bearer {one_id}:{one_token}"}
        two_auth = {"Authorization": f"Bearer {two_id}:{two_token}"}

        print("\n== A machine that has never reported ==")
        r = c.get("/api/bitlocker/PC-01")
        check("GET -> 200", r.status_code == 200)
        check("support is null, not an empty volume list dressed as 'nothing to encrypt'",
              r.get_json()["support"] is None)

        print("\n== The heartbeat carries the posture, and is answered with what is missing ==")
        r = c.post("/api/agent/heartbeat",
                   json={"config_version": 0, "bitlocker": posture()}, headers=one_auth)
        check("heartbeat -> 200", r.status_code == 200)
        check("...and the reply names the protector the hub has no key for",
              r.get_json().get("bitlocker_escrow_wanted") == ["{REC-1}"])
        body = c.get("/api/bitlocker/PC-01").get_json()
        check("the posture is readable through the console API",
              body["volumes"][0]["protection"] == "on")
        check("...and a protector carries no password field at all, only whether it is held",
              set(body["volumes"][0]["protectors"][0])
              == {"id", "kind", "label", "escrowed", "escrowed_at", "last_read_at",
                  "read_count"})

        print("\n== A malformed report must never fail a heartbeat ==")
        for junk in ("not a dict", 7, {"support": "supported", "volumes": "nope"},
                     {"volumes": [1, 2]}, {"support": 5}):
            r = c.post("/api/agent/heartbeat",
                       json={"config_version": 0, "bitlocker": junk}, headers=one_auth)
            check(f"heartbeat with bitlocker={junk!r:.24} still 200", r.status_code == 200)
        check("...and a report the hub cannot understand is stored as an error, never as "
              "'unsupported' -- a real fault must not hide in the state nobody is shown",
              c.get("/api/bitlocker/PC-01").get_json()["support"] == "error")
        # Back to a good posture, so what follows is testing the heartbeat rather than the
        # wreckage the junk above deliberately left behind.
        c.post("/api/agent/heartbeat",
               json={"config_version": 0, "bitlocker": posture()}, headers=one_auth)
        c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=one_auth)
        check("a heartbeat without a bitlocker block leaves the last posture alone",
              c.get("/api/bitlocker/PC-01").get_json()["support"] == "supported")

        print("\n== Escrow: only what was asked for, and only from the right machine ==")
        r = c.post("/api/agent/bitlocker/keys", json={"keys": [
            {"protector_id": "{REC-1}", "recovery_password": "1" * 48, "volume": "C:"}]},
            headers=one_auth)
        check("the asked-for key is stored", r.status_code == 200
              and r.get_json()["stored"] == 1)
        check("...and the heartbeat stops asking for it",
              c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=one_auth)
              .get_json().get("bitlocker_escrow_wanted") == [])
        check("...and it is in the audit trail at security level",
              ("bitlocker_key_escrow", "PC-01", fleet.LEVEL_SECURITY) in audit_actions(db_path))
        check("...and the posture endpoint still hands out no key material -- the reveal is "
              "the only way out, and it is the only thing audited as one",
              "1" * 48 not in json.dumps(c.get("/api/bitlocker/PC-01").get_json()))

        r = c.post("/api/agent/bitlocker/keys", json={"keys": [
            {"protector_id": "{INVENTED}", "recovery_password": "9" * 48}]},
            headers=one_auth)
        check("an id the hub never asked for is refused", r.get_json()["stored"] == 0)

        # PC-02 tries to file a key against a protector PC-01 reported. The machine name comes
        # from the bearer token, never the body, so this can only ever touch PC-02's own store
        # -- where {REC-1} was never wanted.
        r = c.post("/api/agent/bitlocker/keys", json={"keys": [
            {"protector_id": "{REC-1}", "recovery_password": "7" * 48}]},
            headers=two_auth)
        check("one machine cannot file a key against another's protector",
              r.get_json()["stored"] == 0 and bitlocker.escrowed_ids(db_path, "PC-02") == set())

        r = c.post("/api/agent/bitlocker/keys", json={"keys": []})
        check("an unauthenticated escrow post is a 401", r.status_code == 401)

        print("\n== Reading a key is its own capability ==")
        CURRENT_USER = "tech@x.com"
        check("a viewer can see the posture",
              c.get("/api/bitlocker/PC-01").status_code == 200)
        check("...and is told they may not read keys, so the console offers no button",
              c.get("/api/bitlocker/PC-01").get_json()["can_read_keys"] is False)
        r = c.post("/api/bitlocker/PC-01/reveal", json={"protector_id": "{REC-1}"})
        check("...and the reveal itself is refused", r.status_code == 403)

        CURRENT_USER = "recover@x.com"
        r = c.post("/api/bitlocker/PC-01/reveal", json={"protector_id": "{REC-1}"})
        check("the capability holder is given the key", r.status_code == 200
              and r.get_json()["recovery_password"] == "1" * 48)
        check("...and taking it is recorded at security level, naming who took it",
              ("bitlocker_key_read", "recover@x.com", fleet.LEVEL_SECURITY)
              in audit_actions(db_path))
        check("...and the card can now say the key has left this hub before",
              c.get("/api/bitlocker/PC-01").get_json()["volumes"][0]["protectors"][0]
              ["read_count"] == 1)
        check("an unknown protector is a 404, not a 500",
              c.post("/api/bitlocker/PC-01/reveal",
                     json={"protector_id": "{NOPE}"}).status_code == 404)
        check("a reveal with no protector named is refused",
              c.post("/api/bitlocker/PC-01/reveal", json={}).status_code == 400)
        # Out of scope for this group, and the refusal must not distinguish "not allowed" from
        # "no such machine" -- see permissions_web.require_machine.
        check("a machine outside the operator's scope is refused",
              c.post("/api/bitlocker/PC-02/reveal",
                     json={"protector_id": "{REC-1}"}).status_code == 403)

        print("\n== Escrow switched off ==")
        CURRENT_USER = "super@x.com"
        settings.set_many(db_path, {"security.escrow_bitlocker_keys": False},
                          updated_by="super@x.com")
        settings.invalidate()
        c.post("/api/agent/heartbeat",
               json={"config_version": 0, "bitlocker": posture("{REC-2}")}, headers=one_auth)
        r = c.post("/api/agent/heartbeat", json={"config_version": 0}, headers=one_auth)
        check("the hub asks for nothing while escrow is off",
              "bitlocker_escrow_wanted" not in r.get_json())
        r = c.post("/api/agent/bitlocker/keys", json={"keys": [
            {"protector_id": "{REC-2}", "recovery_password": "3" * 48}]}, headers=one_auth)
        check("...and a key offered anyway is not stored, and is not an error for the agent",
              r.status_code == 200 and r.get_json()["stored"] == 0)
        check("...and what was already escrowed is untouched",
              bitlocker.escrowed_ids(db_path, "PC-01") == {"{REC-1}"})
        CURRENT_USER = "recover@x.com"
        check("...and is still readable, because turning collection off is not a delete",
              c.post("/api/bitlocker/PC-01/reveal",
                     json={"protector_id": "{REC-1}"}).status_code == 200)

        print("\n== Moving escrowed keys with a machine's name ==")
        # Three outcomes, and the caller acts on which one: only FAILED may hold the posture
        # rename back. A bool here meant "nothing to move" read as failure, so the posture of
        # every machine that had never escrowed a key -- most of the fleet, and all of it on a
        # hub with escrow off -- silently stayed under the merged-away hostname after a merge.
        check("a machine with no escrowed keys reports nothing to move, not failure",
              bitlocker_web.move_escrow(log_dir, "NEVER-ESCROWED", "PC-NEW")
              == bitlocker_web.ESCROW_NOTHING)

        bitlocker_web.backups.store_secret(
            log_dir, backups.load_master_key(), bitlocker.secret_id_for("HAS-KEYS"),
            {"keys": {"{REC-M}": {"recovery_password": "4" * 48, "volume": "C:"}}})
        check("...a machine that has them reports them moved",
              bitlocker_web.move_escrow(log_dir, "HAS-KEYS", "SURVIVOR")
              == bitlocker_web.ESCROW_MOVED)
        moved = backups.load_secret(log_dir, backups.load_master_key(),
                                    bitlocker.secret_id_for("SURVIVOR"))
        check("...and the password is readable under the new name",
              moved["keys"]["{REC-M}"]["recovery_password"] == "4" * 48)
        check("...and gone from under the old one",
              not backups.has_secret(log_dir, bitlocker.secret_id_for("HAS-KEYS")))

        # A blob that exists and cannot be opened: the master key changed under it. This is the
        # one case that must NOT report success, because moving the index without the passwords
        # leaves the console offering keys the hub can no longer find.
        with open(os.path.join(log_dir, "backup_secrets.json"), encoding="utf-8") as fh:
            store = json.load(fh)
        store[bitlocker.secret_id_for("UNREADABLE")] = store[bitlocker.secret_id_for("SURVIVOR")]
        with open(os.path.join(log_dir, "backup_secrets.json"), "w", encoding="utf-8") as fh:
            json.dump(store, fh)
        check("an existing blob that will not open reports failure, not nothing",
              bitlocker_web.move_escrow(log_dir, "UNREADABLE", "PC-02")
              == bitlocker_web.ESCROW_FAILED)
        check("...and it is left where it is rather than half-moved",
              backups.has_secret(log_dir, bitlocker.secret_id_for("UNREADABLE")) and
              not backups.has_secret(log_dir, bitlocker.secret_id_for("PC-02")))
        check("...and the same answer is available BEFORE a merge destroys anything",
              bitlocker_web.escrow_blocked(log_dir, "UNREADABLE"))
        check("...while a machine with nothing escrowed is never blocked",
              not bitlocker_web.escrow_blocked(log_dir, "NEVER-ESCROWED"))

        # A blob with no master key to open it. The secret id is the AAD, so a blob is never
        # copied to a new name -- it is opened and sealed again -- and without the key that
        # cannot happen. Reading this as "nothing to move" renamed the index away from passwords
        # that are still sitting in the store, and restoring the key afterwards does not put the
        # index back: the two halves stay under different hostnames for good.
        saved_key = os.environ.pop("BACKUP_MASTER_KEY")
        check("a blob with no master key to open it is blocked, not empty",
              bitlocker_web.escrow_blocked(log_dir, "SURVIVOR"))
        check("...and reports failure rather than nothing to move",
              bitlocker_web.move_escrow(log_dir, "SURVIVOR", "PC-03")
              == bitlocker_web.ESCROW_FAILED)
        check("...and a machine that never escrowed anything still is not blocked, "
              "so an ordinary merge is untouched",
              not bitlocker_web.escrow_blocked(log_dir, "NEVER-ESCROWED"))
        os.environ["BACKUP_MASTER_KEY"] = saved_key

        # A store file that will not parse at all. backups._read_secret_file degrades it to {} so
        # one bad file cannot take the console down, which makes has_secret() answer False for
        # every id -- including ids whose blob is sitting in that very file. Left alone, that is
        # the same stranding as above, arrived at from the other direction. Last in this section:
        # it destroys the store.
        with open(os.path.join(log_dir, "backup_secrets.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        check("an unparseable store blocks the move instead of reading as an empty one",
              bitlocker_web.escrow_blocked(log_dir, "SURVIVOR"))
        check("...and the move refuses rather than renaming the index off its keys",
              bitlocker_web.move_escrow(log_dir, "SURVIVOR", "PC-04")
              == bitlocker_web.ESCROW_FAILED)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        os.environ.pop("BACKUP_MASTER_KEY", None)
        shutil.rmtree(log_dir, ignore_errors=True)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
