"""HTTP-layer test for files_web.py -- the remote file explorer's endpoints, both ways.

Wires the blueprints directly onto a minimal Flask app, avoiding app.py's OAuth boot --
same approach as test_processes_web / test_wake_web / test_bios_web / test_fleet_web.

What is worth stating about the assertions here:

  * **Browsing is `issue_commands`, not `view`.** That is the one place this feature departs
    from the Processes card next door, and it is deliberate: a folder listing is not
    inventory, it is the names of somebody's documents, and the same door reads the bytes in
    them. The capability adds nothing to anyone who has it (they already have a SYSTEM shell
    through the Terminal tab), and withholding it from `view` keeps every user's files away
    from everyone who can read a temperature graph. So the test that matters is that a
    VIEWER is refused a directory listing.

  * **The upload is two requests, and the split is a CSRF control.** The multipart half must
    be inert -- no command, no machine touched -- because multipart is the one state-changing
    shape a cross-site form can still produce. The JSON half is what acts, and JSON is what
    app.login_required's content-type rule covers. Both halves are asserted.

  * **An agent acts for itself and for nothing else.** Every agent-facing route re-checks
    that the row belongs to the calling machine, so PC-2 cannot answer PC-1's listing or read
    the bytes queued for PC-1's disk. That is asserted on all three.

  * **There is exactly one door.** Every path is validated in files_web before it becomes a
    command, so the generic command endpoint must turn these types away -- otherwise the
    validation is optional.
"""
import functools
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import files
import fleet
import permissions
import settings
from files_web import create_files_blueprint
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
    spool_dir = tempfile.mkdtemp(prefix="filespool")
    try:
        fleet.init_fleet_db(db_path)
        files.init_files_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PC-1"], members=["tech@x.com"])
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-1"], members=["viewer@x.com"])
        settings.invalidate()

        agent_id, agent_token = fleet.enroll_agent(db_path, "PC-1", SECRET, SECRET)
        other_id, other_token = fleet.enroll_agent(db_path, "PC-2", SECRET, SECRET)

        app.register_blueprint(create_files_blueprint(
            db_path, spool_dir, fake_login_required, access,
            hub_url="https://hub.example.com"))
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        auth = {"Authorization": f"Bearer {agent_id}:{agent_token}"}
        other_auth = {"Authorization": f"Bearer {other_id}:{other_token}"}

        # ------------------------------------------------------------------
        print("\n== Browsing needs issue_commands, not view ==")
        CURRENT_USER = "viewer@x.com"
        r = c.post("/api/machines/PC-1/files/list", json={"path": "C:\\Users"})
        check("a viewer cannot list a folder -> 403", r.status_code == 403)
        r = c.post("/api/machines/PC-1/files/download", json={"path": "C:\\Users\\a.txt"})
        check("...nor download a file -> 403", r.status_code == 403)

        CURRENT_USER = "tech@x.com"
        r = c.post("/api/machines/PC-2/files/list", json={"path": "C:\\"})
        check("a tech cannot reach a machine outside their scope -> 403",
              r.status_code == 403)

        # ------------------------------------------------------------------
        print("\n== Asking for a folder queues a command and answers with an id ==")
        r = c.post("/api/machines/PC-1/files/list", json={"path": "c:/Users/bob"})
        check("POST -> 201", r.status_code == 201)
        started = r.get_json()
        request_id = started["request_id"]
        check("the cadence is served, not hardcoded in the browser",
              started["poll_interval"] == files.POLL_INTERVAL_SECONDS)
        queued = fleet.get_command(db_path, started["command_id"])
        check("the queued command carries the NORMALIZED path, not what was typed",
              queued["params"]["path"] == "C:\\Users\\bob")
        check("...and is attributed to the operator who clicked",
              queued["issued_by"] == "tech@x.com")

        r = c.get(f"/api/machines/PC-1/files/list/{request_id}")
        check("polling it before the machine answers says pending, not empty",
              r.get_json()["status"] == "pending")

        r = c.post("/api/machines/PC-1/files/list", json={})
        check("no path means the drive list, which is an answer rather than an error",
              r.status_code == 201)
        drives_request = r.get_json()["request_id"]
        check("...and the machine is told to enumerate drives",
              fleet.get_command(db_path, r.get_json()["command_id"])["params"]["drives"] is True)

        r = c.post("/api/machines/PC-1/files/list", json={"path": "C:\\Users\\..\\Windows"})
        check("a path with '..' is refused before it becomes a command -> 400",
              r.status_code == 400)

        # ------------------------------------------------------------------
        print("\n== The machine answers, and only about itself ==")
        listing = {"path": "C:\\Users\\bob", "entries": [
            {"name": "Documents", "directory": True},
            {"name": "notes.txt", "directory": False, "size": 11},
        ]}
        r = c.post(f"/api/agent/files/listing/{request_id}", json=listing, headers=other_auth)
        check("another machine's agent cannot answer this listing -> 404",
              r.status_code == 404)
        r = c.post(f"/api/agent/files/listing/{request_id}", json=listing)
        check("...nor can an unauthenticated caller -> 401", r.status_code == 401)
        r = c.post(f"/api/agent/files/listing/{request_id}", json=listing, headers=auth)
        check("the machine it was asked of can -> 200", r.status_code == 200)

        r = c.get(f"/api/machines/PC-1/files/list/{request_id}")
        body = r.get_json()
        check("...and the console reads the folder back",
              body["status"] == "ready"
              and [e["name"] for e in body["entries"]] == ["Documents", "notes.txt"])

        r = c.post(f"/api/agent/files/listing/{drives_request}",
                   json={"error": "Access is denied"}, headers=auth)
        check("a refusal is reported the same way -> 200", r.status_code == 200)
        body = c.get(f"/api/machines/PC-1/files/list/{drives_request}").get_json()
        check("...and reads back as the ANSWER, not as a broken request",
              body["status"] == "failed" and body["error"] == "Access is denied")

        # ------------------------------------------------------------------
        print("\n== Operations are validated before they become commands ==")
        r = c.post("/api/machines/PC-1/files/operation",
                   json={"op": "delete", "paths": ["C:\\Users\\bob\\notes.txt"]})
        check("a delete queues -> 201", r.status_code == 201)
        check("...as a file_operation",
              fleet.get_command(db_path, r.get_json()["command_id"])["type"] == "file_operation")

        r = c.post("/api/machines/PC-1/files/operation",
                   json={"op": "delete", "paths": ["C:\\"]})
        check("deleting a whole drive is refused -> 400", r.status_code == 400)
        r = c.post("/api/machines/PC-1/files/operation",
                   json={"op": "move", "paths": ["C:\\a"], "destination": "C:\\a\\b"})
        check("moving a folder inside itself is refused -> 400", r.status_code == 400)
        r = c.post("/api/machines/PC-1/files/operation", json={"op": "chmod", "paths": ["C:\\a"]})
        check("an invented verb is refused -> 400", r.status_code == 400)

        # ------------------------------------------------------------------
        print("\n== A download is two hops, and the machine only fills the first ==")
        r = c.post("/api/machines/PC-1/files/download",
                   json={"path": "C:\\Users\\bob\\notes.txt"})
        check("POST -> 201", r.status_code == 201)
        download = r.get_json()
        check("the download is named after the file", download["name"] == "notes.txt")
        params = fleet.get_command(db_path, download["command_id"])["params"]
        check("...and the machine is handed the hub's PUBLIC address to PUT to",
              params["url"].startswith("https://hub.example.com/api/agent/files/transfer/"))

        r = c.post("/api/machines/PC-1/files/download", json={"path": "C:\\"})
        check("a whole drive is not a download -> 400", r.status_code == 400)

        r = c.get(f"/api/machines/PC-1/files/transfers/{download['transfer_id']}/content")
        check("collecting bytes that have not arrived is refused -> 409", r.status_code == 409)

        url = f"/api/agent/files/transfer/{download['transfer_id']}"
        r = c.put(url, data=b"hello world", headers=other_auth)
        check("another machine cannot fill this transfer -> 404", r.status_code == 404)
        r = c.put(url, data=b"hello world", headers=auth)
        check("the machine it belongs to can -> 200", r.status_code == 200)
        check("...and the hub counted the bytes it actually received",
              r.get_json()["size_bytes"] == 11)

        r = c.get(f"/api/machines/PC-1/files/transfers/{download['transfer_id']}")
        check("the console sees it go ready", r.get_json()["status"] == "ready")
        check("the spool filename never leaves the building",
              "spool" not in r.get_json())

        r = c.get(f"/api/machines/PC-1/files/transfers/{download['transfer_id']}/content")
        check("...and collects the bytes", r.status_code == 200 and r.data == b"hello world")
        # Served as an attachment with a generic type: these bytes came off a managed machine
        # and are entirely under the control of whoever put them there. Inline, an uploaded
        # .html would run as script on the hub's own origin against the operator's session.
        check("...as an attachment, never inline",
              "attachment" in r.headers.get("Content-Disposition", ""))
        check("...and never as a type a browser would render",
              r.headers.get("Content-Type", "").startswith("application/octet-stream"))

        r = c.put(url, data=b"again", headers=auth)
        check("a second PUT is refused -- the operator may already be downloading", r.status_code == 409)

        # ------------------------------------------------------------------
        print("\n== An upload is two requests, and only the second one acts ==")
        commands_before = len(fleet.list_commands(db_path, "PC-1"))
        r = c.post("/api/machines/PC-1/files/upload",
                   data={"file": (io.BytesIO(b"MZ fake installer"), "setup.exe")},
                   content_type="multipart/form-data")
        check("the multipart half stores the bytes -> 201", r.status_code == 201)
        spooled = r.get_json()
        check("...and measures them", spooled["size_bytes"] == 17)
        # The whole basis of the CSRF exemption this endpoint gets in app.CSRF_UPLOAD_ENDPOINTS:
        # a cross-site form can reach it, so it must do nothing a machine would notice.
        check("...and queues NOTHING -- that is what makes it safe to reach cross-site",
              len(fleet.list_commands(db_path, "PC-1")) == commands_before)

        r = c.get(f"/api/agent/files/transfer/{spooled['transfer_id']}/content", headers=auth)
        check("the machine cannot collect bytes that were never aimed at it -> 409",
              r.status_code == 409)

        r = c.post("/api/machines/PC-1/files/push",
                   json={"transfer_id": spooled["transfer_id"],
                         "destination": "C:\\Temp\\drivers", "name": "setup.exe"})
        check("the JSON half aims them and queues the command -> 201", r.status_code == 201)
        pushed = r.get_json()
        check("...at the full path the file will land on",
              pushed["path"] == "C:\\Temp\\drivers\\setup.exe")
        params = fleet.get_command(db_path, pushed["command_id"])["params"]
        check("...and overwrite is off unless it was asked for",
              params["overwrite"] is False)

        r = c.get(f"/api/agent/files/transfer/{spooled['transfer_id']}/content",
                  headers=other_auth)
        check("another machine cannot collect them -> 404", r.status_code == 404)
        r = c.get(f"/api/agent/files/transfer/{spooled['transfer_id']}/content", headers=auth)
        check("the machine they were aimed at can -> 200",
              r.status_code == 200 and r.data == b"MZ fake installer")

        r = c.post("/api/machines/PC-1/files/push",
                   json={"transfer_id": spooled["transfer_id"],
                         "destination": "C:\\Temp", "name": "setup.exe"})
        check("re-aiming bytes an agent is already fetching is refused -> 409",
              r.status_code == 409)

        r = c.post(f"/api/agent/files/transfer/{spooled['transfer_id']}/result",
                   json={}, headers=auth)
        check("the machine reports it landed -> 200", r.status_code == 200)
        check("...and the hub reclaims the spooled bytes",
              not os.path.exists(os.path.join(spool_dir,
                                              files.new_spool_name(spooled["transfer_id"]))))

        # ------------------------------------------------------------------
        print("\n== A failure is reported, so the console is not left polling ==")
        r = c.post("/api/machines/PC-1/files/download", json={"path": "C:\\locked.pst"})
        stuck = r.get_json()["transfer_id"]
        r = c.post(f"/api/agent/files/transfer/{stuck}/result",
                   json={"error": "The process cannot access the file"}, headers=auth)
        check("the machine can say it could not read the file -> 200", r.status_code == 200)
        body = c.get(f"/api/machines/PC-1/files/transfers/{stuck}").get_json()
        check("...and the console reads the reason rather than waiting out the hour",
              body["status"] == "failed"
              and body["error"] == "The process cannot access the file")

        # ------------------------------------------------------------------
        print("\n== There is exactly one door ==")
        # Every path is validated in files_web before it becomes a command, and the listing
        # and transfer rows are minted there too. A hand-rolled copy would skip the first and
        # answer into nothing for the second.
        for command_type in sorted(fleet.FILE_COMMANDS):
            r = c.post("/api/fleet/commands",
                       json={"machine": "PC-1", "type": command_type,
                             "params": {"path": "C:\\Users\\..\\Windows"}})
            check(f"{command_type} cannot be hand-rolled through the command channel -> 400",
                  r.status_code == 400)

        try:
            fleet.create_favorite(db_path, "tech@x.com", "nuke", "file_operation",
                                  {"op": "delete", "paths": ["C:\\Temp\\build"]})
            saved = True
        except ValueError:
            saved = False
        check("...and none of them can be saved as a favorite aimed at a later machine",
              saved is False)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
        for name in os.listdir(spool_dir):
            try:
                os.remove(os.path.join(spool_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(spool_dir)
        except OSError:
            pass


if __name__ == "__main__":
    main()
