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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
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

    def presigned_url(self, key, method="PUT", expires_seconds=3600):
        """Stands in for the SigV4 query-string signer.

        Shaped like the real thing rather than returning a bare key, because what the
        restore tests assert about it is exactly what matters in production: that the
        agent is handed a URL scoped to one object, and never the credential.
        """
        return (f"https://fake.invalid/{key}?X-Amz-Signature=deadbeef"
                f"&X-Amz-Expires={int(expires_seconds)}")


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
        hub_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub")
        app = Flask(__name__,
                    template_folder=os.path.join(hub_dir, "templates"),
                    static_folder=os.path.join(hub_dir, "static"))
        app.secret_key = "test"
        # A roster the tests can steer. app.py derives `online` from agents.last_seen;
        # here it is a dict so a machine can be taken offline mid-test, which is the only
        # way to assert that "Back up now" queues rather than fails.
        roster = {"HOSPITAL-1": True, "HOSPITAL-2": True, "CLINIC-9": True}
        app.register_blueprint(create_backups_blueprint(
            db_path, log_dir, env_path, fake_login_required,
            create_access(db_path, {"root@x.com"}), hub_version="1.28.0",
            machine_roster=lambda: [{"machine": m, "online": on}
                                    for m, on in roster.items()]))

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

        print("\n== Clearing an override goes back to the fleet policy ==")
        # HOSPITAL-1 was opted out just above. Selecting "Follow the fleet policy" in the
        # console sends an explicit null, which used to arrive as "leave it alone" -- so
        # an opted-out machine could never be opted back IN from the UI, and every later
        # "Back up now" on it was refused.
        r = c.put("/api/backups/machines/HOSPITAL-1", json={"enabled": None})
        check("an explicit null clears the override", r.status_code == 200
              and r.get_json()["config"]["enabled"] is None)
        check("...so the machine follows the fleet again",
              r.get_json()["effective"]["enabled"] is True)
        check("...while omitting the key still leaves it alone",
              c.put("/api/backups/machines/HOSPITAL-1",
                    json={"exclude": []}).get_json()["config"]["enabled"] is None)

        print("\n== Back up one PC now ==")
        CURRENT_USER = "backup@x.com"
        r = c.post("/api/backups/machines/HOSPITAL-1/run", json={})
        check("an in-scope machine can be backed up on demand", r.status_code == 202)
        body = r.get_json()
        check("...and it started, because the machine is online",
              body["status"] == "started")
        check("...returning the same payload the tab already renders", "runs" in body)
        started = backups.list_runs(db_path, limit=5,
                                    kind=backups.BACKUP_MACHINE_FILES,
                                    machine="HOSPITAL-1")
        check("...as a manual run credited to the operator",
              started[0]["trigger"] == backups.TRIGGER_MANUAL
              and started[0]["actor"] == "backup@x.com")
        check("...and it is audited",
              "backup_files_run" in audit_actions(db_path))

        # The case the whole request/dispatch split exists for.
        roster["HOSPITAL-1"] = False
        backups.ingest_file_result(db_path, log_dir, started[0]["id"],
                                   {"error": "done"}, keep_chains=2)
        r = c.post("/api/backups/machines/HOSPITAL-1/run", json={})
        body = r.get_json()
        check("backing up an OFFLINE PC is queued, not refused",
              r.status_code == 202 and body["status"] == "queued")
        check("...and says so in words an operator can act on",
              "comes online" in body["message"])
        check("...leaving the request pending on the machine",
              backups.get_machine_config(db_path, "HOSPITAL-1")["run_requested_at"])
        roster["HOSPITAL-1"] = True

        check("a machine outside the caller's scope is refused",
              c.post("/api/backups/machines/CLINIC-9/run", json={}).status_code == 403)
        CURRENT_USER = "viewer@x.com"
        check("a viewer cannot start a backup",
              c.post("/api/backups/machines/HOSPITAL-1/run",
                     json={}).status_code == 403)
        check("...nor a fleet-wide one",
              c.post("/api/backups/files/run", json={}).status_code == 403)
        CURRENT_USER = "backup@x.com"
        # Not decorative: Content-Type: application/json is not CORS-safelisted, so a
        # cross-site form cannot produce one -- but only if the route CHECKS.
        check("a form post cannot trigger a backup",
              c.post("/api/backups/machines/HOSPITAL-1/run",
                     data="machine=HOSPITAL-1",
                     content_type="application/x-www-form-urlencoded"
                     ).status_code == 415)

        print("\n== Back up the whole fleet now ==")
        backups.clear_file_run_request(db_path, "HOSPITAL-1")
        backups.set_machine_config(db_path, "HOSPITAL-2", enabled=False,
                                   actor="root@x.com")
        r = c.post("/api/backups/files/run", json={})
        check("the fleet run is accepted", r.status_code == 202)
        body = r.get_json()
        check("...covering only machines in the caller's scope",
              body["requested"] + body["skipped"] == 1)
        check("a machine that opted out is skipped, not failed",
              c.post("/api/backups/files/run", json={}).status_code == 202)
        CURRENT_USER = "root@x.com"
        r = c.post("/api/backups/files/run", json={}).get_json()
        check("a superuser sees the whole fleet",
              r["requested"] + r["skipped"] == 3)
        check("...and HOSPITAL-2 is counted as skipped, being switched off",
              r["skipped"] >= 1)
        check("a form post cannot trigger a fleet backup",
              c.post("/api/backups/files/run", data="x=1",
                     content_type="application/x-www-form-urlencoded"
                     ).status_code == 415)
        CURRENT_USER = "backup@x.com"
        backups.set_machine_config(db_path, "HOSPITAL-2", enabled=None,
                                   actor="root@x.com")

        print("\n== Cancel a PC backup ==")
        # Queue one on an offline machine, then cancel it: the request-only case.
        roster["HOSPITAL-1"] = False
        c.post("/api/backups/machines/HOSPITAL-1/run", json={})
        check("a queued backup is pending before cancel",
              backups.get_machine_config(db_path, "HOSPITAL-1")["run_requested_at"])
        r = c.post("/api/backups/machines/HOSPITAL-1/cancel", json={})
        check("cancel is accepted", r.status_code == 200)
        body = r.get_json()
        check("...reporting the queued request was cleared",
              body["cancelled"]["request_cleared"] is True)
        check("...in words for the operator", "cancelled" in body["message"].lower())
        check("...and the request is gone",
              backups.get_machine_config(db_path, "HOSPITAL-1")["run_requested_at"]
              is None)
        check("...and it is audited",
              "backup_files_cancel" in audit_actions(db_path))
        roster["HOSPITAL-1"] = True

        # Earlier fleet-run tests left running rows on HOSPITAL-1; drain to a clean state
        # (one cancel stops the running run) before asserting the empty case.
        c.post("/api/backups/machines/HOSPITAL-1/cancel", json={})
        check("cancelling with nothing running is a calm no-op",
              c.post("/api/backups/machines/HOSPITAL-1/cancel",
                     json={}).get_json()["cancelled"]["nothing_to_cancel"] is True)
        check("a machine outside scope cannot be cancelled",
              c.post("/api/backups/machines/CLINIC-9/cancel",
                     json={}).status_code == 403)
        check("a form post cannot cancel a backup",
              c.post("/api/backups/machines/HOSPITAL-1/cancel", data="x=1",
                     content_type="application/x-www-form-urlencoded"
                     ).status_code == 415)
        CURRENT_USER = "viewer@x.com"
        check("a viewer cannot cancel",
              c.post("/api/backups/machines/HOSPITAL-1/cancel",
                     json={}).status_code == 403)
        check("...nor cancel fleet-wide",
              c.post("/api/backups/files/cancel", json={}).status_code == 403)
        CURRENT_USER = "backup@x.com"

        print("\n== Cancel the whole fleet ==")
        roster["HOSPITAL-1"] = False
        c.post("/api/backups/machines/HOSPITAL-1/run", json={})
        r = c.post("/api/backups/files/cancel", json={})
        check("the fleet cancel is accepted", r.status_code == 200)
        cbody = r.get_json()
        check("...and reports at least the one queued request it cleared",
              cbody["requests_cleared"] >= 1)
        check("a form post cannot cancel the fleet",
              c.post("/api/backups/files/cancel", data="x=1",
                     content_type="application/x-www-form-urlencoded"
                     ).status_code == 415)
        roster["HOSPITAL-1"] = True

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

        print("\n== Browsing a machine's manifest ==")
        # A manifest to browse. Recorded through backups.py rather than posted as an agent
        # result, so this file stays a test of the HTTP boundary rather than of ingest.
        m_plan = backups.plan_next_run(db_path, "HOSPITAL-1", full_every=5)
        backups.record_file_set(
            db_path, run_id="run-manifest", machine="HOSPITAL-1",
            chain_id=m_plan["chain_id"], sequence=0,
            object_key="machines/HOSPITAL-1/full.fhb", stored_bytes=500, files=[
                {"path": "C:\\Users\\bob\\Desktop\\a.txt", "size": 10, "mtime": 1,
                 "sha256": "aa"},
                {"path": "C:\\Users\\bob\\Documents\\r.docx", "size": 20, "mtime": 1,
                 "sha256": "rr"},
            ])
        # HR-1's own manifest exists so the scope checks below are proving a real refusal
        # rather than an empty result.
        hr_plan = backups.plan_next_run(db_path, "HR-1", full_every=5)
        backups.record_file_set(
            db_path, run_id="run-hr", machine="HR-1", chain_id=hr_plan["chain_id"],
            sequence=0, object_key="machines/HR-1/full.fhb", stored_bytes=1,
            files=[{"path": "C:\\Users\\hr\\salaries.xlsx", "size": 1, "sha256": "ss"}])

        r = c.get("/api/backups/machines/HOSPITAL-1/manifest")
        check("the manifest root is browsable", r.status_code == 200)
        check("...counting what is recoverable",
              r.get_json()["summary"]["file_count"] == 2)
        check("...and listing the drive as a folder",
              [d["name"] for d in r.get_json()["result"]["dirs"]] == ["C:"])

        r = c.get("/api/backups/machines/HOSPITAL-1/manifest"
                  "?path=C:\\Users\\bob\\Desktop")
        check("a folder lists its files",
              [f["name"] for f in r.get_json()["result"]["files"]] == ["a.txt"])
        r = c.get("/api/backups/machines/HOSPITAL-1/manifest?search=docx")
        check("search answers in search mode", r.get_json()["mode"] == "search")
        check("...finding the file", len(r.get_json()["result"]["files"]) == 1)

        # The manifest names every file on a machine, which is exactly the kind of
        # reconnaissance scoping exists to prevent -- reading it must be as gated as
        # restoring from it.
        check("another team's manifest is refused",
              c.get("/api/backups/machines/HR-1/manifest").status_code == 403)

        print("\n== Restore ==")
        c.put("/api/backups/schedule", json={"backup.files_destination": dest_id})
        r = c.post("/api/backups/machines/HOSPITAL-1/restore",
                   json={"paths": ["C:\\Users\\bob\\Desktop"], "target_dir": "C:\\Rest"})
        check("a restore is accepted", r.status_code == 202)
        body = r.get_json()
        check("...reporting what it will fetch",
              body["file_count"] == 1 and body["archives"] == 1)
        restore_id = body["restore_id"]
        queued = fleet.list_commands(db_path, machine="HOSPITAL-1")
        check("a restore_files command was queued for the target",
              any(cmd["type"] == "restore_files" for cmd in queued))

        r = c.post("/api/backups/machines/HOSPITAL-1/restore",
                   json={"paths": ["C:\\Users\\bob", "C:\\Nope"]})
        check("a selection matching nothing is reported back, not swallowed",
              r.get_json()["missing"] == ["C:\\Nope"])
        check("a selection matching nothing AT ALL is a 400",
              c.post("/api/backups/machines/HOSPITAL-1/restore",
                     json={"paths": ["D:\\nothing"]}).status_code == 400)
        check("a bad target folder is a 400",
              c.post("/api/backups/machines/HOSPITAL-1/restore",
                     json={"paths": ["C:\\Users\\bob"],
                           "target_dir": "relative"}).status_code == 400)

        # BOTH ends are checked. Reading HR-1's files and writing files onto an HR machine
        # are separate things to be allowed to do, and holding scope on one does not imply
        # the other.
        check("restoring FROM an out-of-scope machine is refused",
              c.post("/api/backups/machines/HR-1/restore",
                     json={"paths": ["C:\\Users\\hr"]}).status_code == 403)
        check("restoring ONTO an out-of-scope machine is refused",
              c.post("/api/backups/machines/HOSPITAL-1/restore",
                     json={"paths": ["C:\\Users\\bob"],
                           "target": "HR-1"}).status_code == 403)
        check("restore history is scoped too",
              c.get("/api/backups/machines/HR-1/restores").status_code == 403)
        check("...and readable for a machine in scope",
              len(c.get("/api/backups/machines/HOSPITAL-1/restores")
                  .get_json()["restores"]) == 2)

        print("\n== Agent restore endpoints ==")
        # Unauthenticated is the first thing to prove: these hand out a decryption key and
        # stream archives.
        check("the plan needs agent auth",
              c.get(f"/api/agent/backups/restore/{restore_id}/plan").status_code == 401)
        check("the archive proxy needs agent auth",
              c.get(f"/api/agent/backups/restore/{restore_id}/archive/0"
                    ).status_code == 401)
        check("reporting a result needs agent auth",
              c.post(f"/api/agent/backups/restore/{restore_id}/result",
                     json={}).status_code == 401)

        enroll_secret = "enrollment-secret"
        agent_id, token = fleet.enroll_agent(db_path, "HOSPITAL-1", enroll_secret,
                                             enroll_secret)
        other_id, other_token = fleet.enroll_agent(db_path, "HR-1", enroll_secret,
                                                   enroll_secret)
        auth = {"Authorization": f"Bearer {agent_id}:{token}"}
        other_auth = {"Authorization": f"Bearer {other_id}:{other_token}"}

        r = c.get(f"/api/agent/backups/restore/{restore_id}/plan", headers=auth)
        check("the target machine's agent gets the plan", r.status_code == 200)
        plan_body = r.get_json()
        check("...carrying the file list", plan_body["archives"][0]["files"])
        check("...and the source machine's derived key",
              plan_body["encryption"]["key"])
        check("...but never the destination credential",
              "shh" not in json.dumps(plan_body))
        # The property the whole brokering design exists for.
        check("another machine's agent cannot read the plan",
              c.get(f"/api/agent/backups/restore/{restore_id}/plan",
                    headers=other_auth).status_code == 404)
        check("...nor stream its archives",
              c.get(f"/api/agent/backups/restore/{restore_id}/archive/0",
                    headers=other_auth).status_code == 404)
        check("an archive index outside the plan is refused",
              c.get(f"/api/agent/backups/restore/{restore_id}/archive/99",
                    headers=auth).status_code == 404)

        r = c.post(f"/api/agent/backups/restore/{restore_id}/result",
                   headers=auth, json={"restored": 1, "bytes_restored": 10})
        check("the agent can report a result", r.status_code == 200)
        check("...closing the restore", r.get_json()["status"] == "succeeded")
        check("a finished restore no longer hands out its plan",
              c.get(f"/api/agent/backups/restore/{restore_id}/plan",
                    headers=auth).status_code == 409)

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
