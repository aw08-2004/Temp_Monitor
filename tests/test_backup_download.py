"""The two ways a machine's backed-up files come back WITHOUT the machine answering.

Both halves here exist because of the same silent failure, seen from opposite ends.

  * **A restore that never reports leaves a row saying `running` forever.** The row is
    opened before the command is queued and closed only when the agent POSTs a result --
    so a command that expired unclaimed, one the agent failed outright, and one whose plan
    the hub refused (which the agent reports against the COMMAND, never the restore) all
    looked identical in the console: a spinner, no count, no reason, for a day. The
    console was showing "still working" for restores that had been dead for hours.
    `reconcile_restores` closes them from the command's own terminal status and log, which
    is the only place the reason was ever written down.

  * **Download is the answer to "just send me the file".** Restore needs an agent, an
    online PC and an overwrite decision; recovering one deleted document needs none of
    those, and before this the only path was `restore_backup.py` on the hub's own console.
    What this file guards is that the endpoint is gated exactly as hard as a restore
    (it ships a user's documents off the fleet), that a single file arrives as itself
    rather than as a zip of one, and that a multi-file download is a zip a real unpacker
    opens -- it is written to an unseekable stream, so a regression there produces a file
    that downloads perfectly and cannot be opened.
"""
import functools
import gzip
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import backup_paths
import backups
import fleet
import i18n
import permissions
import settings
from backups_web import create_backups_blueprint
from permissions_web import create_access
from flask import Flask

PASS = 0
FAIL = 0

CURRENT_USER = "root@x.com"

#: What PC-1's one archive holds. Two files in two folders, so a folder tick can be
#: proved to pull one of them and not the other.
SAMPLE_FILES = {
    "C:\\Users\\bob\\Desktop\\notes.txt": b"desktop notes, recovered",
    "C:\\Users\\bob\\Documents\\q1.xlsx": b"documents payload",
}
OBJECT_KEY = "machines/PC-1/20260101T000000Z-chain-000-full.fhb"


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


class FakeDestination:
    """An in-memory WebDAV share. `open` returns a streaming-shaped response, because
    that is what the download path consumes -- a client that handed back `.content`
    would pass a test the real one fails."""

    objects = {}

    def __init__(self, config=None, secret=None):
        pass

    def put(self, key, fileobj, size, sha256_hex):
        FakeDestination.objects[key] = fileobj.read()

    def open(self, key):
        payload = FakeDestination.objects[key]

        class Response:
            headers = {"Content-Length": str(len(payload))}

            def iter_content(self, chunk_size=1024):
                # Deliberately small blocks regardless of what was asked for: the reader
                # under test is an adapter over an iterator, and a single-block stream
                # would never exercise the boundary it exists to get right.
                step = max(1, min(chunk_size, 64))
                for start in range(0, len(payload), step):
                    yield payload[start:start + step]

            def close(self):
                pass

        return Response()

    def delete(self, key):
        FakeDestination.objects.pop(key, None)

    def list(self, prefix):
        return [{"key": k, "size": len(v)} for k, v in FakeDestination.objects.items()
                if k.startswith(prefix)]


def seal_archive(machine, files):
    """tar -> gzip -> FHBK1, the way the agent's BackupFilesExecutor builds one.

    Built here rather than read from a fixture because what matters is the MEMBER NAMES:
    the hub plans a download of an archive it never wrote, and names its members with
    backup_paths.archive_member. Sealing with the same function the agent's C# mirrors is
    what makes a drift in either one show up as a download that finds nothing.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path, payload in files.items():
            info = tarfile.TarInfo(backup_paths.archive_member(path))
            info.size = len(payload)
            info.mtime = 1_700_000_000
            tar.addfile(info, io.BytesIO(payload))
    sealed = io.BytesIO()
    backups.write_envelope(
        io.BytesIO(gzip.compress(raw.getvalue())), sealed,
        backups.machine_key_for(machine),
        {"kind": "machine_files", "machine": machine, "chain_id": "chain",
         "sequence": 0, "full": True})
    return sealed.getvalue()


def main():
    global CURRENT_USER
    workdir = tempfile.mkdtemp(prefix="bkdl-tests-")
    log_dir = os.path.join(workdir, "logs")
    os.makedirs(log_dir)
    db_path = os.path.join(log_dir, "temp_v2.db")
    env_path = os.path.join(workdir, ".env")
    saved_env = os.environ.get(backups.MASTER_KEY_ENV)
    real_build = backups.build_client
    FakeDestination.objects = {}

    try:
        os.environ.pop(backups.MASTER_KEY_ENV, None)
        open(env_path, "w", encoding="utf-8").close()

        fleet.init_fleet_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        backups.init_backups_db(db_path)
        permissions.init_permissions_db(db_path)
        permissions.invalidate()

        permissions.create_group(
            db_path, name="Viewers", capabilities=[permissions.VIEW],
            machines=["PC-1"], members=["viewer@x.com"], actor="root@x.com")
        permissions.create_group(
            db_path, name="Hospital backups",
            capabilities=[permissions.VIEW, permissions.MANAGE_BACKUPS],
            machines=["PC-1"], members=["backup@x.com"], actor="root@x.com")

        backups.build_client = lambda record, secret: FakeDestination()

        hub_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub")
        app = Flask(__name__,
                    template_folder=os.path.join(hub_dir, "templates"),
                    static_folder=os.path.join(hub_dir, "static"))
        app.secret_key = "test"
        app.register_blueprint(create_backups_blueprint(
            db_path, log_dir, env_path, fake_login_required,
            create_access(db_path, {"root@x.com"}), hub_version="1.0.0",
            hub_url="https://hub.example",
            machine_roster=lambda: [{"machine": "PC-1", "online": True},
                                    {"machine": "HR-1", "online": True}]))
        for endpoint in ("index", "inventory_page", "alerts_page", "logout"):
            app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "")

        @app.context_processor
        def _nav_context():
            context = {"cap": permissions, "user_capabilities": set(),
                       "open_alert_count": 0, "is_superuser": False,
                       "hub_version": "1.0.0"}
            context.update(i18n.template_context("en"))
            return context

        @app.before_request
        def _seed_session():
            from flask import session
            session["user"] = {"email": CURRENT_USER}

        c = app.test_client()

        # ---- a machine with one real, sealed archive behind it ----
        CURRENT_USER = "root@x.com"
        c.post("/api/backups/key", json={})
        dest_id = c.post("/api/backups/destinations", json={
            "name": "Store", "kind": "webdav",
            "config": {"base_url": "https://dav.example/backups"},
            "secret": {"username": "u", "password": "p"}}).get_json()["id"]
        c.put("/api/backups/schedule", json={"backup.files_destination": dest_id})

        FakeDestination.objects[OBJECT_KEY] = seal_archive("PC-1", SAMPLE_FILES)
        plan = backups.plan_next_run(db_path, "PC-1", full_every=5)
        backups.record_file_set(
            db_path, run_id="run-1", machine="PC-1", chain_id=plan["chain_id"],
            sequence=0, object_key=OBJECT_KEY, stored_bytes=900,
            files=[{"path": path, "size": len(body), "mtime": 1_700_000_000,
                    "sha256": "aa"} for path, body in SAMPLE_FILES.items()])

        print("\n== Download is gated like a restore, not like a listing ==")
        CURRENT_USER = "viewer@x.com"
        check("a viewer cannot download a machine's files",
              c.get("/api/backups/machines/PC-1/download?path=C:").status_code == 403)
        check("a viewer cannot even price one up",
              c.post("/api/backups/machines/PC-1/download/preview",
                     json={"paths": ["C:"]}).status_code == 403)
        CURRENT_USER = "backup@x.com"
        check("a scoped operator cannot download another team's files",
              c.get("/api/backups/machines/HR-1/download?path=C:").status_code == 403)

        print("\n== One file comes back as itself ==")
        r = c.get("/api/backups/machines/PC-1/download"
                  "?path=C:\\Users\\bob\\Desktop\\notes.txt")
        check("the download succeeds", r.status_code == 200)
        check("...with the file's own bytes, decrypted and unpacked",
              r.get_data() == SAMPLE_FILES["C:\\Users\\bob\\Desktop\\notes.txt"])
        check("...saved under its own name rather than as a zip of one",
              'filename="notes.txt"' in r.headers["Content-Disposition"])

        print("\n== A folder comes back as a zip a real unpacker opens ==")
        r = c.get("/api/backups/machines/PC-1/download?path=C:\\Users\\bob")
        check("the download succeeds", r.status_code == 200)
        check("...named for the machine", r.headers["Content-Disposition"].endswith(".zip"))
        check("...and says how many files it holds before it is opened",
              r.headers.get("X-Backup-Files") == "2")
        # The property the streaming writer exists for: it never seeks back to patch the
        # local headers, so a regression produces a file that downloads and will not open.
        bundle = zipfile.ZipFile(io.BytesIO(r.get_data()))
        check("the zip is structurally valid", bundle.testzip() is None)
        check("...holding both files, under their archive names",
              sorted(bundle.namelist()) == ["C/Users/bob/Desktop/notes.txt",
                                            "C/Users/bob/Documents/q1.xlsx"])
        check("...with the real contents",
              bundle.read("C/Users/bob/Documents/q1.xlsx")
              == SAMPLE_FILES["C:\\Users\\bob\\Documents\\q1.xlsx"])

        print("\n== The preview answers before anything is saved ==")
        # The download itself is a navigation, so its refusal never reaches the page. The
        # preview is the only place a selection can be turned down where somebody sees it.
        r = c.post("/api/backups/machines/PC-1/download/preview",
                   json={"paths": ["C:\\Users\\bob", "C:\\Nope"]})
        body = r.get_json()
        check("the preview prices the selection", body["file_count"] == 2)
        check("...and names what matched nothing", body["missing"] == ["C:\\Nope"])
        check("...and says what the file will be called", body["filename"].endswith(".zip"))
        check("a selection matching nothing at all is a 400",
              c.post("/api/backups/machines/PC-1/download/preview",
                     json={"paths": ["D:\\gone"]}).status_code == 400)
        check("too many separate ticks is a 400 naming the folder way out",
              "folder" in c.post(
                  "/api/backups/machines/PC-1/download/preview",
                  json={"paths": [f"C:\\x{n}" for n in
                                  range(backups.MAX_DOWNLOAD_SELECTIONS + 1)]}
              ).get_json()["error"])

        print("\n== Every download is audited, because documents leave the fleet ==")
        with fleet.get_conn(db_path) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT actor, level, target, detail_json FROM audit_log "
                "WHERE action = 'backup_download'")]
        check("the download is on the audit trail", len(rows) == 2)
        check("...at security level", rows[0]["level"] == fleet.LEVEL_SECURITY)
        check("...naming the operator and the machine",
              rows[0]["actor"] == "backup@x.com" and rows[0]["target"] == "PC-1")
        check("...with the SHAPE of it, never the file list",
              "notes.txt" not in rows[0]["detail_json"]
              and json.loads(rows[0]["detail_json"])["files"] == 1)

        print("\n== A destination that is gone is refused where somebody sees it ==")
        # A per-machine override outlives the destination it names, and "is it set" was the
        # only check: the restore was created, the command queued, and the hub refused the
        # agent's plan -- into the agent's command result, which no operator reads.
        CURRENT_USER = "root@x.com"
        # Reached the way it is reached in the field: a machine is pointed at its own
        # destination, and that destination is later deleted. delete_destination clears
        # the hub schedule and deliberately keeps run history -- but nothing walks the
        # per-machine overrides, so PC-1 is left naming a destination that is gone.
        doomed = c.post("/api/backups/destinations", json={
            "name": "Old NAS", "kind": "webdav",
            "config": {"base_url": "https://old.example/backups"},
            "secret": {"username": "u", "password": "p"}}).get_json()["id"]
        c.put("/api/backups/machines/PC-1", json={"destination_id": doomed})
        check("deleting it succeeds",
              c.delete(f"/api/backups/destinations/{doomed}").status_code == 200)
        check("...and the machine is still pointed at it",
              backups.get_machine_config(db_path, "PC-1")["destination_id"] == doomed)
        r = c.post("/api/backups/machines/PC-1/restore",
                   json={"paths": ["C:\\Users\\bob"], "target_dir": "C:\\Rest"})
        check("a restore from a deleted destination is a 400, not a queued command",
              r.status_code == 400)
        check("...saying what to do about it",
              "no longer exists" in r.get_json()["error"])
        check("...and no restore row was opened for it",
              len(backups.list_restores(db_path, machine="PC-1")) == 0)
        check("the download refuses it too",
              c.get("/api/backups/machines/PC-1/download?path=C:").status_code == 400)
        c.put("/api/backups/machines/PC-1", json={"destination_id": ""})

        print("\n== A refused plan closes the restore with the HUB's reason ==")
        # The failure Alexander hit: the agent asks for the plan, the hub says no, and all
        # the agent can report is "the hub would not supply the plan" -- into a command
        # result nobody reads, while the row spins for a day. The sentence that explains it
        # exists only here, so it is written onto the row rather than thrown away with the
        # 400. Provoked by deleting the destination out from under a live restore, which is
        # the same shape as any other terminal plan refusal.
        CURRENT_USER = "root@x.com"
        agent_id, token = fleet.enroll_agent(db_path, "PC-1", "secret", "secret")
        auth = {"Authorization": f"Bearer {agent_id}:{token}"}
        doomed_id = c.post("/api/backups/machines/PC-1/restore",
                           json={"paths": ["C:\\Users\\bob"],
                                 "target_dir": "C:\\Rest"}).get_json()["restore_id"]
        c.delete(f"/api/backups/destinations/{dest_id}")
        r = c.get(f"/api/agent/backups/restore/{doomed_id}/plan", headers=auth)
        check("the plan is refused", r.status_code == 400)
        refused = backups.get_restore(db_path, doomed_id)
        check("...and the restore stops saying running", refused["status"]
              == backups.RUN_FAILED)
        check("...carrying the reason, which only the hub ever knew",
              "no longer exists" in refused["error"])
        check("a second ask is the already-finished answer, not a second close",
              c.get(f"/api/agent/backups/restore/{doomed_id}/plan",
                    headers=auth).status_code == 409)
        # Put the destination back for whatever runs after this.
        dest_id = c.post("/api/backups/destinations", json={
            "name": "Store again", "kind": "webdav",
            "config": {"base_url": "https://dav.example/backups"},
            "secret": {"username": "u", "password": "p"}}).get_json()["id"]
        c.put("/api/backups/schedule", json={"backup.files_destination": dest_id})

        print("\n== A restore whose command died stops saying 'running' ==")
        def open_restore():
            r = c.post("/api/backups/machines/PC-1/restore",
                       json={"paths": ["C:\\Users\\bob\\Desktop"],
                             "target_dir": "C:\\Rest"})
            return r.get_json()["restore_id"]

        # 1. The one that actually bit: the agent could not fetch the plan, so it reported
        #    against the COMMAND and deliberately not against the restore.
        failed_id = open_restore()
        command_id = backups.get_restore(db_path, failed_id)["command_id"]
        fleet.claim_commands(db_path, agent_id, "PC-1")
        fleet.complete_command(
            db_path, command_id, agent_id, success=False,
            output="[restore] FAILED: The hub would not supply the plan for this "
                   "restore: the hub answered HTTP 404.")
        check("nothing closes it while the result could still be in flight",
              backups.reconcile_restores(db_path) == 0)
        check("...so it is still running", backups.get_restore(db_path, failed_id)["status"]
              == backups.RUN_RUNNING)
        later = int(time.time()) + backups.RESTORE_RECONCILE_GRACE_SECONDS + 1
        check("past the grace period it is closed", backups.reconcile_restores(
            db_path, now=later) == 1)
        closed = backups.get_restore(db_path, failed_id)
        check("...as failed", closed["status"] == backups.RUN_FAILED)
        check("...carrying the machine's own words, which is where the reason was",
              "HTTP 404" in closed["error"])
        check("...and not claiming zero files were written, which it cannot know",
              closed["restored_count"] is None)
        check("a second pass leaves it alone",
              backups.reconcile_restores(db_path, now=later) == 0)

        # 2. The offline case: the command expired before anyone claimed it. The operator
        #    needs "it never picked this up", not the agent's silence rendered as progress.
        expired_id = open_restore()
        expired_command = backups.get_restore(db_path, expired_id)["command_id"]
        with fleet.get_conn(db_path) as conn:
            conn.execute("UPDATE commands SET status = ?, expires_at = ? WHERE id = ?",
                         (fleet.STATUS_EXPIRED, int(time.time()) - 3600, expired_command))
        check("an expired command closes its restore",
              backups.reconcile_restores(db_path) == 1)
        check("...explaining that the machine never picked it up",
              "never picked" in backups.get_restore(db_path, expired_id)["error"])

        # 3. A restore the agent DID report is none of this function's business -- it is
        #    already terminal, and reconciling it again would overwrite a real count.
        reported_id = open_restore()
        reported_command = backups.get_restore(db_path, reported_id)["command_id"]
        backups.ingest_restore_result(db_path, reported_id,
                                      {"restored": 1, "bytes_restored": 24})
        fleet.claim_commands(db_path, agent_id, "PC-1")
        fleet.complete_command(db_path, reported_command, agent_id, success=True,
                               output="[restore] 1 file(s) written")
        check("a reported restore is left exactly as the agent left it",
              backups.reconcile_restores(db_path, now=later + 10_000) == 0)
        settled = backups.get_restore(db_path, reported_id)
        check("...succeeded, with its real count",
              settled["status"] == backups.RUN_SUCCEEDED
              and settled["restored_count"] == 1)

        # 4. A command still in flight is not a dead one. Closing a claimed command's
        #    restore would kill every restore that takes longer than one scheduler pass --
        #    which is every restore worth the name.
        running_id = open_restore()
        fleet.claim_commands(db_path, agent_id, "PC-1")
        check("a claimed, unfinished command keeps its restore running",
              backups.reconcile_restores(db_path, now=later + 20_000) == 0)
        check("...still running", backups.get_restore(db_path, running_id)["status"]
              == backups.RUN_RUNNING)

        print("\n== A machine cannot write headers through its own file listing ==")
        # A manifest path is the AGENT's report of what it backed up, and
        # backup_paths.normalize() reshapes separators and whitespace while leaving
        # control characters exactly where they were. That name is what a single-file
        # download is saved as, which means it reaches a Content-Disposition header -- so
        # without this, a compromised machine appends headers to an operator's download by
        # naming a file. hub/apkhost.py declined to echo even an OPERATOR-supplied name
        # into that header; a machine's own listing is the less trusted of the two.
        CURRENT_USER = "root@x.com"
        poisoned = "C:\\Users\\bob\\Desktop\\notes.txt\r\nX-Injected: 1"
        backups.record_file_set(
            db_path, run_id="run-poison", machine="PC-1", chain_id=plan["chain_id"],
            sequence=1, object_key=OBJECT_KEY, stored_bytes=900,
            files=[{"path": poisoned, "size": 4, "mtime": 1_700_000_100,
                    "sha256": "bb"}])
        r = c.post("/api/backups/machines/PC-1/download/preview",
                   json={"paths": [poisoned]})
        saved_as = (r.get_json() or {}).get("filename", "")
        check("the poisoned path is still a file the hub will plan",
              r.status_code == 200 and (r.get_json() or {}).get("file_count") == 1)
        check("...but a CR or LF in it never reaches the saved name",
              "\r" not in saved_as and "\n" not in saved_as)
        check("...and the name still says which file it was",
              saved_as.startswith("notes.txt"))
        check("a quote cannot close the header's own quoted string",
              '"' not in backups.safe_name('a".txt'))
        check("an accented name survives, because that is the normal case here",
              backups.safe_name("informe a\u00f1o.pdf") == "informe a\u00f1o.pdf")

    finally:
        backups.build_client = real_build
        if saved_env is None:
            os.environ.pop(backups.MASTER_KEY_ENV, None)
        else:
            os.environ[backups.MASTER_KEY_ENV] = saved_env
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


def test_backup_download():
    main()


if __name__ == "__main__":
    sys.exit(main())
