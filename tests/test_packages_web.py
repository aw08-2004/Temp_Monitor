"""HTTP-layer test for packages_web.py using a minimal Flask app + test client.
Avoids app.py's Google-OAuth boot requirement by wiring the blueprint directly,
exactly like test_fleet_web.py.

Unlike the other *_web modules, this one does NOT sign every operator in as a
break-glass superuser. Package deployment is the first feature whose whole point is
aiming code at a chosen set of machines, so "which machines may this operator deploy to"
is part of the endpoint contract, not an orthogonal concern covered elsewhere. The run
therefore switches between a superuser and a genuinely scoped operator.
"""
import functools
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import packages
import permissions
import settings
from packages_web import create_packages_blueprint
from permissions_web import create_access
from flask import Flask

PASS = 0
FAIL = 0

# Which operator the fake session gate reports. Mutable so a test can switch identity.
CURRENT_USER = "root@x.com"


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
    workdir = tempfile.mkdtemp(prefix="pkgweb-tests-")
    log_dir = os.path.join(workdir, "logs")
    os.makedirs(log_dir)
    db_path = os.path.join(log_dir, "hub.db")
    try:
        fleet.init_fleet_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        packages.init_packages_db(db_path)
        permissions.init_permissions_db(db_path)
        permissions.invalidate()

        # A scoped operator: may deploy, but only to HOSPITAL-1.
        permissions.create_group(
            db_path, name="Hospital IT",
            capabilities=[permissions.VIEW, permissions.DEPLOY_PACKAGES],
            machines=["HOSPITAL-1"], members=["hospital@x.com"], actor="root@x.com")
        # An operator who can see the console but must not deploy at all.
        permissions.create_group(
            db_path, name="Viewers", capabilities=[permissions.VIEW],
            machines=["HOSPITAL-1", "HR-1"], members=["viewer@x.com"], actor="root@x.com")

        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(create_packages_blueprint(
            db_path, log_dir, fake_login_required,
            create_access(db_path, {"root@x.com"}), hub_url="https://hub.example.com"))

        @app.before_request
        def _seed_session():
            from flask import session
            session["user"] = {"email": CURRENT_USER}

        c = app.test_client()

        print("\n== Upload ==")
        payload = b"MZ pretend installer"
        r = c.post("/api/packages/upload",
                   data={"file": (io.BytesIO(payload), "seven-zip.msi")},
                   content_type="multipart/form-data")
        check("upload 201", r.status_code == 201)
        body = r.get_json()
        import hashlib
        sha = hashlib.sha256(payload).hexdigest()
        check("the hub returns the hash it computed", body["sha256"] == sha)
        check("upload reports the size", body["file_size"] == len(payload))
        check("upload keeps the base filename", body["file_name"] == "seven-zip.msi")
        check("the blob is on disk",
              os.path.exists(packages.blob_path(packages.blob_root(log_dir), sha)))
        check("uploading is audited",
              any(r_["action"] == "upload_package_file" for r_ in _audit(db_path)))
        # Uploading stores bytes and nothing else. That inertness is what lets this one
        # endpoint accept multipart (which a cross-site HTML form CAN produce) without
        # handing a CSRF a way to run anything -- see the module docstring.
        check("uploading creates no package", packages.list_packages(db_path) == [])
        r = c.post("/api/packages/upload", data={}, content_type="multipart/form-data")
        check("upload with no file 400", r.status_code == 400)

        print("\n== Package CRUD ==")
        r = c.post("/api/packages", json={
            "name": "7-Zip", "version": "24.09",
            "source": {"kind": "upload", "sha256": sha, "file_name": "seven-zip.msi",
                       "file_size": len(payload)},
            "install_command": "msiexec.exe",
            "install_args": '/i "{file}" /qn /norestart',
            "detection": {"kind": "file_exists", "path": "C:\\Program Files\\7-Zip\\7z.exe"},
        })
        check("create 201", r.status_code == 201)
        pkg = r.get_json()
        check("the created package comes back whole",
              pkg["name"] == "7-Zip" and pkg["source"]["sha256"] == sha)
        package_id = pkg["id"]

        r = c.post("/api/packages", json={"name": "Broken", "source": {"kind": "upload",
                   "sha256": sha}, "install_command": "setup.exe", "install_args": "/S"})
        check("a package that never references {file} is refused with 400",
              r.status_code == 400 and "{file}" in r.get_json()["error"])

        r = c.get("/api/packages")
        listing = r.get_json()
        check("list 200", r.status_code == 200)
        check("the list carries one package", len(listing["packages"]) == 1)
        # The form renders from these, so drift between server and JS is impossible.
        check("the list is self-describing about detection kinds",
              [k["name"] for k in listing["detection_kinds"]]
              == list(packages.DETECTION_KINDS))
        check("the list carries the upload limit", listing["max_upload_mb"] == 512)
        check("the list carries retry defaults",
              listing["defaults"]["max_attempts"] == 3)

        r = c.put(f"/api/packages/{package_id}", json={"version": "25.00"})
        check("update 200", r.status_code == 200 and r.get_json()["version"] == "25.00")
        check("unknown package 404", c.get("/api/packages/nope").status_code == 404)
        check("updating an unknown package 404",
              c.put("/api/packages/nope", json={"version": "1"}).status_code == 404)

        print("\n== CSRF: a form post cannot define a package ==")
        # get_json(silent=True) yields None for a form body, so `source` is missing and
        # the request dies in validation. This is what stops a cross-site form POST from
        # defining a package on a signed-in operator's behalf.
        r = c.post("/api/packages", data={"name": "Evil", "install_command": "{file}"})
        check("form-encoded package create rejected", r.status_code == 400)
        check("form-encoded create wrote nothing",
              len(packages.list_packages(db_path)) == 1)

        print("\n== Deployments: scope enforcement ==")
        r = c.post("/api/deployments", json={"package_id": package_id,
                                             "machines": ["HOSPITAL-1", "HR-1"]})
        check("a superuser may deploy fleet-wide", r.status_code == 201)
        deployment_id = r.get_json()["id"]
        check("the deployment reports both targets",
              r.get_json()["target_total"] == 2)

        CURRENT_USER = "hospital@x.com"
        r = c.post("/api/deployments", json={"package_id": package_id,
                                             "machines": ["HOSPITAL-1"]})
        check("a scoped operator may deploy inside their scope", r.status_code == 201)
        scoped_deployment = r.get_json()["id"]

        # All-or-nothing: quietly installing on nine of the ten machines you asked for
        # is worse than refusing the request.
        before = len(packages.list_deployments(db_path))
        r = c.post("/api/deployments", json={"package_id": package_id,
                                             "machines": ["HOSPITAL-1", "HR-1"]})
        check("a partly out-of-scope deployment is refused entirely",
              r.status_code == 403)
        check("the refusal names the machine the operator cannot reach",
              "HR-1" in r.get_json()["error"])
        check("nothing was created by the refused request",
              len(packages.list_deployments(db_path)) == before)

        r = c.post("/api/deployments", json={"package_id": package_id, "machines": []})
        check("a deployment with no machines is a 400, not a 403", r.status_code == 400)

        # Reads are scoped too: a fleet-wide deploy must not leak hostnames the operator
        # is not allowed to see.
        r = c.get(f"/api/deployments/{deployment_id}")
        machines = [t["machine"] for t in r.get_json()["targets"]]
        check("a scoped operator sees only their own targets in a fleet-wide deploy",
              machines == ["HOSPITAL-1"])
        check("the untouched total still reflects reality",
              r.get_json()["target_total"] == 2)
        r = c.get("/api/deployments?machine=HR-1")
        check("filtering by an out-of-scope machine is refused", r.status_code == 403)

        CURRENT_USER = "viewer@x.com"
        r = c.get("/api/packages")
        check("view alone does not grant access to packages", r.status_code == 403)
        r = c.post("/api/deployments", json={"package_id": package_id,
                                             "machines": ["HOSPITAL-1"]})
        check("view alone cannot deploy even inside scope", r.status_code == 403)
        r = c.post("/api/packages/upload",
                   data={"file": (io.BytesIO(b"x"), "x.msi")},
                   content_type="multipart/form-data")
        check("view alone cannot upload a payload", r.status_code == 403)

        print("\n== Cancel & retry ==")
        CURRENT_USER = "root@x.com"
        r = c.post(f"/api/deployments/{scoped_deployment}/cancel")
        check("cancel 200", r.status_code == 200)
        check("the cancelled deployment reports its status",
              r.get_json()["status"] == packages.DEPLOY_CANCELLED)
        check("cancelling an unknown deployment 404",
              c.post("/api/deployments/nope/cancel").status_code == 404)
        r = c.post(f"/api/deployments/{scoped_deployment}/retry")
        check("retry 200 and reports what it requeued",
              r.status_code == 200 and r.get_json()["requeued"] == 1)

        print("\n== Agent payload download ==")
        agent_id, token = fleet.enroll_agent(db_path, "HOSPITAL-1", "s", "s")
        auth = {"Authorization": f"Bearer {agent_id}:{token}"}

        r = c.get(f"/api/agent/packages/{sha}")
        check("download without agent auth 401", r.status_code == 401)
        r = c.get(f"/api/agent/packages/{sha}", headers=auth)
        check("an enrolled agent gets the payload", r.status_code == 200)
        check("the bytes served are the bytes uploaded", r.data == payload)

        # The blob store must not be a general read primitive that happens to sit behind
        # agent auth: a hash no package references is simply not there.
        orphan = "b" * 64
        os.makedirs(os.path.dirname(
            packages.blob_path(packages.blob_root(log_dir), orphan)), exist_ok=True)
        with open(packages.blob_path(packages.blob_root(log_dir), orphan), "wb") as fh:
            fh.write(b"not a package")
        r = c.get(f"/api/agent/packages/{orphan}", headers=auth)
        check("a blob no package references is a 404 even when it exists on disk",
              r.status_code == 404)
        r = c.get("/api/agent/packages/not-a-hash", headers=auth)
        check("a malformed digest is a 404, not a 500", r.status_code == 404)
        r = c.get(f"/api/agent/packages/{sha}",
                  headers={"Authorization": "Bearer bogus:token"})
        check("a bad agent token 401", r.status_code == 401)

        # A row that survives its file (a half-restored backup) is a hub-side problem;
        # saying 410 rather than 404 is what tells the operator where to look.
        os.remove(packages.blob_path(packages.blob_root(log_dir), sha))
        r = c.get(f"/api/agent/packages/{sha}", headers=auth)
        check("a missing payload file reports 410, not 404", r.status_code == 410)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


def _audit(db_path):
    with fleet.get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]


if __name__ == "__main__":
    sys.exit(main())
