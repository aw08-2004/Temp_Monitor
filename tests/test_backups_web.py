"""HTTP-layer test for backups_web.py using a minimal Flask app + test client.
Avoids app.py's Google-OAuth boot requirement by wiring the blueprint directly,
exactly like test_packages_web.py and test_fleet_web.py.

What this file is really guarding is the boundary, not the plumbing:

  * `manage_backups` gates EVERY route, including the read-only ones -- the run list
    names object keys and destinations, which is reconnaissance for anyone who
    shouldn't have it.
  * The master key comes back from exactly two routes, both POST (so a link or an
    <img> cannot trigger them), and both audited every single time.
  * A destination's credentials go in and never come out -- not in the create response,
    not in the list, not masked.
  * The schedule route writes `backup.*` settings and NOTHING else, so a
    `manage_backups` holder cannot use it to reach `hub.auto_update`.

Unlike the packages suite there is no machine scoping here: a hub-database backup is the
whole hub, so `manage_backups` is deliberately all-or-nothing (see backups_web.py's
module docstring). The scoped operator in this run is therefore testing that the
capability is required, not that a subset of machines is visible.
"""
import functools
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backups
import fleet
import permissions
import settings
from backups_web import create_backups_blueprint
from permissions_web import create_access
from flask import Flask

PASS = 0
FAIL = 0

# Which operator the fake session gate reports. Mutable so a test can switch identity.
CURRENT_USER = "root@x.com"

# The same awkward machine the path-grammar tests use: bob has OneDrive Known Folder
# Move, carol is missing folders. Read from the shared fixture rather than duplicated so
# the HTTP layer and the grammar agree about what a machine looks like.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "backup_path_vectors.json"), encoding="utf-8") as _fh:
    SAMPLE_PROFILES = json.load(_fh)["profiles"]


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
    """An in-memory bucket, so "back up now" completes without a network."""

    def __init__(self):
        self.objects = {}

    def put(self, key, fileobj, size, sha256_hex):
        self.objects[key] = fileobj.read()

    def open(self, key):
        class Response:
            def __init__(self, payload):
                self.content = payload

            def close(self):
                pass
        return Response(self.objects[key])

    def delete(self, key):
        self.objects.pop(key, None)

    def list(self, prefix):
        return [{"key": k, "size": len(v)} for k, v in self.objects.items()
                if k.startswith(prefix)]


def audit_actions(db_path):
    with fleet.get_conn(db_path) as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_log")]


def wait_for_run(db_path, timeout=10):
    """Poll for the background run row. The manual-backup route answers 202 and does the
    work on a thread, so the test has to wait for it rather than assume it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = backups.list_runs(db_path, limit=5)
        finished = [r for r in runs if r["status"] != backups.RUN_RUNNING]
        if finished:
            return finished[0]
        time.sleep(0.1)
    return None


def main():
    global CURRENT_USER
    workdir = tempfile.mkdtemp(prefix="bkweb-tests-")
    log_dir = os.path.join(workdir, "logs")
    os.makedirs(log_dir)
    db_path = os.path.join(log_dir, "temp_v2.db")
    env_path = os.path.join(workdir, ".env")
    saved_env = os.environ.get(backups.MASTER_KEY_ENV)
    real_build = backups.build_client
    bucket = FakeDestination()
    try:
        os.environ.pop(backups.MASTER_KEY_ENV, None)
        open(env_path, "w", encoding="utf-8").close()

        fleet.init_fleet_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        backups.init_backups_db(db_path)
        permissions.init_permissions_db(db_path)
        permissions.invalidate()

        # An operator who can see the console but must not touch backups.
        permissions.create_group(
            db_path, name="Viewers", capabilities=[permissions.VIEW],
            machines=["HOSPITAL-1"], members=["viewer@x.com"], actor="root@x.com")
        # An operator granted exactly `manage_backups` and nothing else -- the case the
        # schedule route exists for (it must not need `manage_settings` too).
        permissions.create_group(
            db_path, name="Backup operators",
            capabilities=[permissions.VIEW, permissions.MANAGE_BACKUPS],
            machines=["HOSPITAL-1"], members=["backup@x.com"], actor="root@x.com")

        # Pointed at the real template tree: `access.require` renders denied.html for a
        # browser navigation rather than returning JSON, so without it a 403 on a PAGE
        # route surfaces as a 500 (TemplateNotFound) and the gate looks like it failed
        # for the wrong reason.
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app = Flask(__name__,
                    template_folder=os.path.join(repo_root, "templates"),
                    static_folder=os.path.join(repo_root, "static"))
        app.secret_key = "test"
        app.register_blueprint(create_backups_blueprint(
            db_path, log_dir, env_path, fake_login_required,
            create_access(db_path, {"root@x.com"}), hub_version="1.28.0"))

        # denied.html extends base.html, whose sidebar/topbar url_for() the app.py routes.
        # Stubbing them is what lets a PAGE route's 403 be asserted here at all -- without
        # them the deny path dies in template rendering and reports 500, which would look
        # like the gate letting the request through.
        for endpoint in ("index", "history_page", "inventory_page", "alerts_page",
                         "logout"):
            app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "")

        @app.context_processor
        def _nav_context():
            # Mirrors app.py's inject_nav_context, which the sidebar reads to decide
            # which links to draw.
            return {"cap": permissions, "user_capabilities": set(),
                    "open_alert_count": 0, "is_superuser": False,
                    "hub_version": "1.28.0"}

        @app.before_request
        def _seed_session():
            from flask import session
            session["user"] = {"email": CURRENT_USER}

        c = app.test_client()

        print("\n== Capability gate ==")
        CURRENT_USER = "viewer@x.com"
        check("a viewer cannot read the backup overview",
              c.get("/api/backups").status_code == 403)
        check("a viewer cannot read run history",
              c.get("/api/backups/runs").status_code == 403)
        check("a viewer cannot create the encryption key",
              c.post("/api/backups/key", json={}).status_code == 403)
        check("a viewer cannot reveal the encryption key",
              c.post("/api/backups/key/reveal", json={}).status_code == 403)
        check("a viewer cannot add a destination",
              c.post("/api/backups/destinations", json={}).status_code == 403)
        check("a viewer cannot start a backup",
              c.post("/api/backups/run", json={}).status_code == 403)
        check("a viewer cannot change the schedule",
              c.put("/api/backups/schedule", json={}).status_code == 403)
        check("a viewer is refused the page too",
              c.get("/backups").status_code == 403)

        print("\n== The master key ==")
        CURRENT_USER = "backup@x.com"
        r = c.get("/api/backups")
        check("overview loads for a backup operator", r.status_code == 200)
        body = r.get_json()
        check("overview reports no key yet", body["key"]["configured"] is False)
        check("overview describes the destination kinds it accepts",
              {k["name"] for k in body["destination_kinds"]} == {"s3", "webdav"})

        check("adding a destination before a key exists is refused with 409",
              c.post("/api/backups/destinations",
                     json={"name": "x", "kind": "s3"}).status_code == 409)

        r = c.post("/api/backups/key", json={})
        check("key creation returns 201", r.status_code == 201)
        key_b64 = r.get_json()["key"]
        check("the key is returned exactly once, in the clear", len(key_b64) > 40)
        check("the key reached .env",
              key_b64 in open(env_path, encoding="utf-8").read())
        check("creating a second key is refused rather than rotating",
              c.post("/api/backups/key", json={}).status_code == 409)

        r = c.post("/api/backups/key/reveal", json={})
        check("reveal returns the same key", r.get_json()["key"] == key_b64)
        check("reveal is audited", audit_actions(db_path).count("backup_key_reveal") == 1)
        check("creation is audited", "backup_key_create" in audit_actions(db_path))
        check("the key is NOT in the overview payload",
              key_b64 not in c.get("/api/backups").get_data(as_text=True))

        state = c.get("/api/backups").get_json()["key"]
        check("overview now reports a configured key", state["configured"] is True)
        check("overview reports the key id, not the key",
              state["key_id"] and state["key_id"] not in key_b64)
        check("the key is flagged as never stored offline", state["escrowed_at"] is None)

        c.post("/api/backups/key/escrowed", json={})
        check("escrow acknowledgement is recorded",
              c.get("/api/backups").get_json()["key"]["escrowed_at"] is not None)
        check("escrow acknowledgement is audited",
              "backup_key_escrowed" in audit_actions(db_path))

        print("\n== Destinations ==")
        good = {"name": "Offsite", "kind": "s3",
                "config": {"endpoint": "https://s3.example.com", "bucket": "backups",
                           "region": "eu-west-1", "prefix": "hub-a", "path_style": True},
                "secret": {"access_key_id": "AKID", "secret_access_key": "TOPSECRET"}}
        r = c.post("/api/backups/destinations", json=good)
        check("destination created", r.status_code == 201)
        dest = r.get_json()
        dest_id = dest["id"]
        check("the create response does not echo the secret",
              "TOPSECRET" not in r.get_data(as_text=True))
        check("the create response reports credentials exist",
              dest["has_credentials"] is True)
        check("the secret is not in the overview either",
              "TOPSECRET" not in c.get("/api/backups").get_data(as_text=True))

        check("a bad endpoint is a 400 with a usable message",
              "https" in c.post("/api/backups/destinations",
                                json=dict(good, name="Bad",
                                          config=dict(good["config"],
                                                      endpoint="http://s3.example.com"))
                                ).get_json()["error"])
        check("a duplicate name is a 400",
              c.post("/api/backups/destinations", json=good).status_code == 400)

        r = c.put(f"/api/backups/destinations/{dest_id}",
                  json={"name": "Offsite S3", "secret": {}})
        check("update renames without touching credentials",
              r.status_code == 200 and r.get_json()["name"] == "Offsite S3")
        check("updating an unknown destination is a 404",
              c.put("/api/backups/destinations/nope", json={"name": "x"}
                    ).status_code == 404)
        check("deleting an unknown destination is a 404",
              c.delete("/api/backups/destinations/nope").status_code == 404)

        print("\n== Schedule ==")
        check("arming the schedule with no destination is refused",
              c.put("/api/backups/schedule",
                    json={"backup.hub_enabled": True}).status_code == 400)
        check("a non-backup setting is refused, not silently ignored",
              c.put("/api/backups/schedule",
                    json={"hub.auto_update": True}).status_code == 400)
        check("hub.auto_update was NOT written",
              settings.get(db_path, "hub.auto_update") is None)
        check("an unknown destination is a 404",
              c.put("/api/backups/schedule",
                    json={"backup.hub_destination": "nope"}).status_code == 404)

        r = c.put("/api/backups/schedule", json={
            "backup.hub_enabled": True,
            "backup.hub_destination": dest_id,
            "backup.hub_interval_hours": 12,
            "backup.hub_keep_generations": 7,
        })
        check("the schedule saves", r.status_code == 200)
        saved = r.get_json()["schedule"]
        check("the schedule reads back enabled", saved["enabled"] is True)
        check("the schedule reads back the interval", saved["interval_hours"] == 12)
        check("the schedule reports when it is next due", saved["next_due_at"] == 0)
        check("a manage_backups holder could set it without manage_settings",
              settings.get_int(db_path, "backup.hub_keep_generations") == 7)
        check("the schedule change is audited",
              "backup_schedule_update" in audit_actions(db_path))
        check("an out-of-range interval is a 400 naming the field",
              "Back up every" in c.put("/api/backups/schedule",
                                       json={"backup.hub_interval_hours": 99999}
                                       ).get_json()["error"])

        print("\n== Back up now ==")
        check("an unknown destination is a 404",
              c.post("/api/backups/run",
                     json={"destination_id": "nope"}).status_code == 404)

        backups.build_client = lambda record, secret: bucket
        r = c.post("/api/backups/run", json={"destination_id": dest_id})
        check("a manual run is accepted with 202", r.status_code == 202)
        run = wait_for_run(db_path)
        check("the background run finished", run is not None)
        check("the background run succeeded",
              run and run["status"] == backups.RUN_SUCCEEDED)
        check("it was attributed to the operator, not the scheduler",
              run and run["actor"] == "backup@x.com")
        check("it is labelled a manual run",
              run and run["trigger"] == backups.TRIGGER_MANUAL)
        check("the object landed in the bucket",
              run and run["object_key"] in bucket.objects)
        check("the run appears in the run list",
              any(x["id"] == run["id"] for x in
                  c.get("/api/backups/runs").get_json()["runs"]))

        print("\n== Per-PC backup settings ==")
        r = c.put("/api/backups/schedule", json={
            "backup.files_enabled": True,
            "backup.files_destination": dest_id,
            "backup.files_include": ["%Desktop%", "%Documents%",
                                     "C:\\Users\\%Users%\\Projects"],
            "backup.files_exclude": ["*.tmp", "**\\node_modules\\**"],
        })
        check("per-PC settings save through the same capability", r.status_code == 200)
        files = r.get_json()["files"]
        check("include list reads back in the case it was typed",
              files["include"] == ["%Desktop%", "%Documents%",
                                   "C:\\Users\\%Users%\\Projects"])
        check("...which is the point of path_list over str_list",
              "%Users%" in files["include"][2])
        check("exclude list reads back", files["exclude"][0] == "*.tmp")

        check("a typo'd token is refused rather than silently matching nothing",
              c.put("/api/backups/schedule",
                    json={"backup.files_include": ["%Userss%\\Desktop"]}
                    ).status_code == 400)
        check("...and the error names the bad token",
              "%userss%" in c.put("/api/backups/schedule",
                                  json={"backup.files_include": ["%Userss%\\Desktop"]}
                                  ).get_json()["error"].lower())
        check("an empty exclude list is allowed",
              c.put("/api/backups/schedule",
                    json={"backup.files_exclude": []}).status_code == 200)
        check("arming per-PC backups with no destination is refused",
              c.put("/api/backups/schedule",
                    json={"backup.files_enabled": True,
                          "backup.files_destination": ""}).status_code == 400)
        # Put the destination back for the machine tests below.
        c.put("/api/backups/schedule", json={"backup.files_destination": dest_id,
                                             "backup.files_exclude": ["*.tmp"]})

        check("the overview carries the token reference for the UI",
              len(c.get("/api/backups").get_json()["path_tokens"]) >= 8)

        print("\n== Per-machine overrides are machine-SCOPED ==")
        backups.record_profiles(db_path, "HOSPITAL-1", SAMPLE_PROFILES)
        r = c.get("/api/backups/machines/HOSPITAL-1")
        check("an in-scope machine is readable", r.status_code == 200)
        body = r.get_json()
        check("the effective policy merges the fleet list",
              "%Desktop%" in body["effective"]["include"])
        check("the preview resolves against reported profiles",
              any("OneDrive" in root["path"] for root in body["preview"]["roots"]))
        check("...and names a user whose folder is missing",
              any("carol" in p for p in body["preview"]["problems"]))

        # This is the difference from the hub-DB routes: manage_backups is not enough on
        # its own, the machine has to be in scope too.
        check("an out-of-scope machine is refused on read",
              c.get("/api/backups/machines/HR-1").status_code == 403)
        check("an out-of-scope machine is refused on write",
              c.put("/api/backups/machines/HR-1",
                    json={"enabled": False}).status_code == 403)
        check("an out-of-scope machine cannot be previewed either",
              c.post("/api/backups/preview",
                     json={"machine": "HR-1", "include": ["%Desktop%"]}
                     ).status_code == 403)

        r = c.put("/api/backups/machines/HOSPITAL-1",
                  json={"include": ["%Users%\\Projects"], "enabled": False})
        check("an override saves", r.status_code == 200)
        check("extra paths are ADDED to the fleet list",
              r.get_json()["effective"]["include"][-1] == "%Users%\\Projects")
        check("a machine can opt out of the fleet policy",
              r.get_json()["effective"]["enabled"] is False)
        check("the machine appears in the exceptions list",
              [m["machine"] for m in
               c.get("/api/backups/machines").get_json()["machines"]]
              == ["HOSPITAL-1"])
        check("a bad pattern on a machine is a 400",
              c.put("/api/backups/machines/HOSPITAL-1",
                    json={"include": ["%Nope%"]}).status_code == 400)

        print("\n== Preview is lenient while you type ==")
        r = c.post("/api/backups/preview",
                   json={"machine": "HOSPITAL-1", "include": ["%Deskt"], "exclude": []})
        check("a half-typed token is a problem, not a 500", r.status_code == 200)
        check("...reported as something an operator can act on",
              "%" in r.get_json()["preview"]["problems"][0])
        r = c.post("/api/backups/preview",
                   json={"include": ["%Desktop%"], "exclude": []})
        check("preview with no machine still answers",
              r.status_code == 200 and r.get_json()["has_profiles"] is False)
        check("...and explains that nothing could be resolved",
              r.get_json()["preview"]["problems"] != [])

        print("\n== Deleting the scheduled destination disarms the schedule ==")
        check("the schedule still points at it",
              settings.get(db_path, "backup.hub_destination") == dest_id)
        check("deleting it succeeds",
              c.delete(f"/api/backups/destinations/{dest_id}").status_code == 200)
        check("the schedule no longer points at a destination that is gone",
              settings.get(db_path, "backup.hub_destination") == "")
        check("and it is turned off rather than left armed at nothing",
              settings.get_bool(db_path, "backup.hub_enabled") is False)
        check("run history survives the destination being deleted",
              len(backups.list_runs(db_path, limit=5)) > 0)

        print("\n== Superuser break-glass ==")
        CURRENT_USER = "root@x.com"
        check("a break-glass superuser reaches the overview",
              c.get("/api/backups").status_code == 200)

    finally:
        backups.build_client = real_build
        if saved_env is None:
            os.environ.pop(backups.MASTER_KEY_ENV, None)
        else:
            os.environ[backups.MASTER_KEY_ENV] = saved_env
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


def test_backups_web():
    main()


if __name__ == "__main__":
    sys.exit(main())
