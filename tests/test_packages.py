"""Unit tests for packages.py -- the deployment core, with no Flask involved.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
Under pytest, conftest.py wraps `check` so a false condition fails the test properly.

The emphasis is on the ways this module can be wrong SILENTLY:

  * a recipe that validates but can never work (a downloaded payload the command line
    never references, a winget package carrying its own install command),
  * a scheduler that loses a target (dispatched twice, stuck `pending` after its window
    closes, still `in_flight` after the command expired), and
  * a blob store that hands the agent a hash it did not compute, or unlinks a file some
    other package still points at.

The scheduler tests drive `now` explicitly rather than sleeping, so retry/backoff/window
behaviour is asserted at exact times instead of approximately.
"""
import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import packages

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def audit_actions(db_path):
    with fleet.get_conn(db_path) as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


def make_package(db_path, name="7-Zip", **overrides):
    """A minimal valid file-backed package."""
    kwargs = dict(
        name=name,
        source={"kind": packages.SOURCE_UPLOAD, "sha256": "a" * 64,
                "file_name": "7z.msi", "file_size": 1234},
        install_command="msiexec.exe",
        install_args='/i "{file}" /qn /norestart',
        actor="op@x.com",
    )
    kwargs.update(overrides)
    return packages.create_package(db_path, **kwargs)


def target_row(db_path, deployment_id, machine):
    with packages.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM deployment_targets WHERE deployment_id = ? AND machine = ?",
            (deployment_id, machine)).fetchone()
    return dict(row) if row else None


def deployment_status(db_path, deployment_id):
    with packages.get_conn(db_path) as conn:
        return conn.execute("SELECT status FROM deployments WHERE id = ?",
                            (deployment_id,)).fetchone()["status"]


def finish(db_path, command_id, success, output="done"):
    """Complete a command the way an agent would, claiming it first."""
    with packages.get_conn(db_path) as conn:
        agent_id = conn.execute("SELECT claimed_by FROM commands WHERE id = ?",
                                (command_id,)).fetchone()["claimed_by"]
    fleet.complete_command(db_path, command_id, agent_id, success, output)


def claim_for(db_path, machine):
    """Enroll-free claim: the scheduler only cares that the row moved to `claimed`."""
    return fleet.claim_commands(db_path, f"agent-{machine}", machine)


def soon():
    """A `now` that is definitely not behind the real clock.

    A deployment created with no window is due at int(time.time()), so ticking a
    just-created deployment with a `now` captured seconds earlier is a coin flip on
    whether the test crossed a second boundary. Blocks that create a deployment and
    expect it to dispatch immediately step off this instead of the shared t0.
    """
    return int(time.time()) + 1


def main():
    # A directory, not mkstemp: the modules here follow fleet.py's idiom of `with
    # get_conn(...)` (which commits but does not close), so connections outlive the
    # calls and Windows refuses to unlink an open file. rmtree(ignore_errors) at the end
    # is the honest cleanup for that, rather than pretending we can delete it.
    workdir = tempfile.mkdtemp(prefix="pkg-tests-")
    db_path = os.path.join(workdir, "packages.db")
    blob_dir = os.path.join(workdir, "blobs")
    try:
        fleet.init_fleet_db(db_path)
        packages.init_packages_db(db_path)

        # ------------------------------------------------------------------ recipe
        print("\n== Recipe validation ==")
        pkg_id = make_package(db_path)
        pkg = packages.get_package(db_path, pkg_id)
        check("package round-trips", pkg["name"] == "7-Zip")
        check("default success codes are 0 and 3010",
              pkg["success_exit_codes"] == [0, 3010])
        check("absent detection normalizes to 'none'",
              pkg["detection"] == {"kind": packages.DETECT_NONE})
        check("source rides along with the package",
              pkg["source"]["kind"] == packages.SOURCE_UPLOAD
              and pkg["source"]["sha256"] == "a" * 64)
        check("creating a package is audited",
              "create_package" in audit_actions(db_path))

        check("a duplicate name is refused",
              raises(ValueError, make_package, db_path, name="7-zip"))
        check("a nameless package is refused",
              raises(ValueError, make_package, db_path, name="   "))

        # The failure this catches: an operator uploads an installer, forgets to
        # reference it, and every machine 'succeeds' having installed nothing.
        check("a file-backed package that never references {file} is refused",
              raises(ValueError, make_package, db_path, name="No placeholder",
                     install_command="setup.exe", install_args="/S"))
        check("{file} in the command alone is enough",
              packages.get_package(db_path, make_package(
                  db_path, name="Exe installer",
                  install_command="{file}", install_args="/VERYSILENT"))["install_args"]
              == "/VERYSILENT")

        check("a winget package with an install command is refused",
              raises(ValueError, packages.create_package, db_path, name="Winget w/ cmd",
                     source={"kind": packages.SOURCE_WINGET, "ref": "7zip.7zip"},
                     install_command="msiexec.exe"))
        winget_id = packages.create_package(
            db_path, name="Winget ok",
            source={"kind": packages.SOURCE_WINGET, "ref": "7zip.7zip"},
            install_args="--scope machine")
        check("a winget package needs no {file}",
              packages.get_package(db_path, winget_id)["install_command"] == "")
        check("winget carries no hash (it has its own trust chain)",
              packages.get_package(db_path, winget_id)["source"]["sha256"] is None)

        check("a URL source must be http(s)",
              raises(ValueError, packages.create_package, db_path, name="Bad url",
                     source={"kind": packages.SOURCE_URL, "ref": "ftp://x/y.msi"},
                     install_command="{file}"))
        check("a UNC source must start with a double backslash",
              raises(ValueError, packages.create_package, db_path, name="Bad unc",
                     source={"kind": packages.SOURCE_UNC, "ref": "C:/share/y.msi"},
                     install_command="{file}"))
        check("an upload source without a hash is refused",
              raises(ValueError, packages.create_package, db_path, name="No hash",
                     source={"kind": packages.SOURCE_UPLOAD}, install_command="{file}"))
        check("a malformed hash is refused",
              raises(ValueError, packages.create_package, db_path, name="Bad hash",
                     source={"kind": packages.SOURCE_UPLOAD, "sha256": "zz"},
                     install_command="{file}"))
        check("a timeout below the floor is refused",
              raises(ValueError, make_package, db_path, name="Fast", timeout_seconds=5))

        print("\n== Exit codes & detection ==")
        check("exit codes accept a comma-separated string",
              packages.validate_exit_codes("0, 3010,1641") == [0, 1641, 3010])
        check("exit codes are de-duplicated and sorted",
              packages.validate_exit_codes([3010, 0, 0]) == [0, 3010])
        check("an empty exit-code set is refused",
              raises(ValueError, packages.validate_exit_codes, []))
        check("a non-numeric exit code is refused",
              raises(ValueError, packages.validate_exit_codes, ["zero"]))

        check("file_exists needs a path",
              raises(ValueError, packages.validate_detection, {"kind": "file_exists"}))
        check("registry rule needs root, key and name",
              raises(ValueError, packages.validate_detection,
                     {"kind": "registry_value", "root": "HKLM", "key": "SOFTWARE\\X"}))
        check("registry root is validated",
              raises(ValueError, packages.validate_detection,
                     {"kind": "registry_value", "root": "HKXX", "key": "k", "name": "n"}))
        rule = packages.validate_detection(
            {"kind": "registry_value", "root": "hklm", "key": "SOFTWARE\\X",
             "name": "DisplayVersion", "equals": "24.09", "extra": "ignored"})
        check("registry root is upper-cased", rule["root"] == "HKLM")
        # Unknown keys must not survive: the agent evaluates this object, so anything
        # that rides along becomes part of the grammar without ever being validated.
        check("unknown detection keys are dropped", "extra" not in rule)
        check("equals is preserved when given", rule["equals"] == "24.09")
        check("an absent equals means 'must merely exist'",
              "equals" not in packages.validate_detection(
                  {"kind": "registry_value", "root": "HKLM", "key": "k", "name": "n"}))
        check("an empty equals is kept (an exact empty-string match)",
              packages.validate_detection(
                  {"kind": "registry_value", "root": "HKLM", "key": "k", "name": "n",
                   "equals": ""})["equals"] == "")
        check("installed_version needs a product name",
              raises(ValueError, packages.validate_detection,
                     {"kind": "installed_version"}))
        check("min_version must be dotted numbers",
              raises(ValueError, packages.validate_detection,
                     {"kind": "installed_version", "name": "7-Zip",
                      "min_version": "v24-beta"}))
        check("an unknown detection kind is refused",
              raises(ValueError, packages.validate_detection, {"kind": "run_script"}))

        # ------------------------------------------------------------------ blobs
        print("\n== Blob store ==")
        payload = b"MZ fake installer bytes"
        sha, size = packages.store_blob(blob_dir, io.BytesIO(payload), max_bytes=1024)
        import hashlib
        check("the hub computes the hash itself, from the bytes written",
              sha == hashlib.sha256(payload).hexdigest())
        check("size is reported", size == len(payload))
        check("the blob lands at its content address",
              os.path.exists(packages.blob_path(blob_dir, sha)))
        again, _ = packages.store_blob(blob_dir, io.BytesIO(payload), max_bytes=1024)
        check("re-uploading identical content is a no-op at the same address",
              again == sha)
        check("no .part files are left behind",
              not [f for f in os.listdir(blob_dir) if f.endswith(".part")])
        check("an oversized upload is refused",
              raises(ValueError, packages.store_blob, blob_dir, io.BytesIO(payload), 4))
        check("an oversized upload leaves no partial blob",
              not [f for f in os.listdir(blob_dir) if f.endswith(".part")])
        check("an empty upload is refused",
              raises(ValueError, packages.store_blob, blob_dir, io.BytesIO(b""), 1024))

        shared_a = packages.create_package(
            db_path, name="Shared A",
            source={"kind": packages.SOURCE_UPLOAD, "sha256": sha, "file_name": "a.msi"},
            install_command="{file}")
        shared_b = packages.create_package(
            db_path, name="Shared B",
            source={"kind": packages.SOURCE_UPLOAD, "sha256": sha, "file_name": "b.msi"},
            install_command="{file}")
        check("a blob shared by two packages is referenced",
              packages.blob_is_referenced(db_path, sha))
        packages.delete_package(db_path, shared_a, blob_root_dir=blob_dir)
        check("deleting one of two packages keeps the shared blob",
              os.path.exists(packages.blob_path(blob_dir, sha)))
        packages.delete_package(db_path, shared_b, blob_root_dir=blob_dir)
        check("deleting the last referrer unlinks the blob",
              not os.path.exists(packages.blob_path(blob_dir, sha)))
        check("deleting an unknown package raises",
              raises(KeyError, packages.delete_package, db_path, "nope"))

        # Refcounting has to survive a package holding SEVERAL blobs: replacing one slot
        # must collect that slot's old blob and leave the other one alone. Getting this
        # wrong deletes a file a live package still points at -- a deploy that then fails
        # with a 410 on every machine at once.
        first, _ = packages.store_blob(blob_dir, io.BytesIO(b"first payload"), 1024)
        second, _ = packages.store_blob(blob_dir, io.BytesIO(b"second payload"), 1024)
        third, _ = packages.store_blob(blob_dir, io.BytesIO(b"third payload"), 1024)
        two_blobs = packages.create_package(
            db_path, name="Two blobs",
            sources=[{"name": "app", "kind": packages.SOURCE_UPLOAD, "sha256": first},
                     {"name": "config", "kind": packages.SOURCE_UPLOAD, "sha256": second}],
            steps=[{"kind": "run", "command": "{app}", "args": "/config {config}"}])
        packages.update_package(
            db_path, two_blobs, blob_root_dir=blob_dir,
            sources=[{"name": "app", "kind": packages.SOURCE_UPLOAD, "sha256": third},
                     {"name": "config", "kind": packages.SOURCE_UPLOAD, "sha256": second}])
        check("replacing one payload unlinks only that payload's blob",
              not os.path.exists(packages.blob_path(blob_dir, first)))
        check("the payload that did not change keeps its blob",
              os.path.exists(packages.blob_path(blob_dir, second)))
        packages.delete_package(db_path, two_blobs, blob_root_dir=blob_dir)
        check("deleting a package collects every one of its blobs",
              not os.path.exists(packages.blob_path(blob_dir, second))
              and not os.path.exists(packages.blob_path(blob_dir, third)))

        # ------------------------------------------------------------------ update
        print("\n== Updating a package ==")
        upd_id = make_package(db_path, name="Updatable")
        packages.update_package(db_path, upd_id, version="24.09", actor="op@x.com")
        updated = packages.get_package(db_path, upd_id)
        check("a partial update leaves other fields alone",
              updated["version"] == "24.09" and updated["install_command"] == "msiexec.exe")
        check("the source survives an update that doesn't mention it",
              updated["source"]["sha256"] == "a" * 64)
        # The pair (source kind, command line) is re-validated together, so switching to
        # winget without clearing the command line has to fail rather than ship a package
        # whose command is silently ignored.
        check("switching to winget while keeping msiexec is refused",
              raises(ValueError, packages.update_package, db_path, upd_id,
                     source={"kind": packages.SOURCE_WINGET, "ref": "7zip.7zip"}))
        packages.update_package(db_path, upd_id, install_command="",
                                source={"kind": packages.SOURCE_WINGET, "ref": "7zip.7zip"})
        check("switching to winget works once the command is cleared",
              packages.get_package(db_path, upd_id)["source"]["kind"]
              == packages.SOURCE_WINGET)
        check("updating an unknown package raises",
              raises(KeyError, packages.update_package, db_path, "nope", version="1"))

        # ------------------------------------------------------------------ deploy
        print("\n== Creating a deployment ==")
        deploy_pkg = make_package(db_path, name="Deployable")
        check("a deployment with no targets is refused",
              raises(ValueError, packages.create_deployment, db_path,
                     package_id=deploy_pkg, machines=[], created_by="op@x.com"))
        check("a deployment for an unknown package raises",
              raises(KeyError, packages.create_deployment, db_path,
                     package_id="nope", machines=["PC1"], created_by="op@x.com"))
        check("an already-closed window is refused",
              raises(ValueError, packages.create_deployment, db_path,
                     package_id=deploy_pkg, machines=["PC1"], created_by="op@x.com",
                     window_end=1))
        check("a window that ends before it starts is refused",
              raises(ValueError, packages.create_deployment, db_path,
                     package_id=deploy_pkg, machines=["PC1"], created_by="op@x.com",
                     window_start=9_000_000_000, window_end=8_000_000_000))
        check("max_attempts is bounded",
              raises(ValueError, packages.create_deployment, db_path,
                     package_id=deploy_pkg, machines=["PC1"], created_by="op@x.com",
                     max_attempts=99))

        dep = packages.create_deployment(
            db_path, package_id=deploy_pkg, machines=["PC1", "pc1", "PC2", " "],
            created_by="op@x.com", max_attempts=2, retry_backoff_seconds=600)
        loaded = packages.get_deployment(db_path, dep)
        check("duplicate machine names collapse (case-insensitively)",
              loaded["target_total"] == 2)
        check("a new deployment starts 'scheduled'",
              loaded["status"] == packages.DEPLOY_SCHEDULED)
        check("the package name is joined in for display",
              loaded["package_name"] == "Deployable")
        check("creating a deployment is audited",
              "create_deployment" in audit_actions(db_path))

        # -------------------------------------------------------------- scheduling
        print("\n== Scheduler: the happy path ==")
        # The scheduler's `now` has to share a clock with the command queue, which stamps
        # created_at/expires_at from time.time() -- so this anchors at the real clock and
        # steps forward from there, rather than at an arbitrary far-future epoch that
        # would make every freshly queued command look already expired.
        t0 = int(time.time())
        reconciled, dispatched = packages.tick(db_path, now=t0, ttl_seconds=900)
        check("both targets are dispatched on the first tick", dispatched == 2)
        check("nothing to reconcile on the first tick", reconciled == 0)
        pc1 = target_row(db_path, dep, "PC1")
        check("a dispatched target is in flight",
              pc1["status"] == packages.TARGET_IN_FLIGHT)
        check("a dispatched target records its attempt", pc1["attempts"] == 1)
        check("a dispatched target records its command id", bool(pc1["command_id"]))
        check("the deployment rolls up to 'running'",
              deployment_status(db_path, dep) == packages.DEPLOY_RUNNING)

        # The property that matters most: a second tick before the agent answers must
        # not queue the same install again.
        check("a second tick does not re-dispatch an in-flight target",
              packages.tick(db_path, now=t0 + 1, ttl_seconds=900)[1] == 0)

        claimed = claim_for(db_path, "PC1")
        check("the agent receives one deploy_package command",
              len(claimed) == 1 and claimed[0]["type"] == packages.COMMAND_TYPE)
        params = claimed[0]["params"]
        check("params snapshot the recipe, not a pointer to it",
              params["install_command"] == "msiexec.exe"
              and params["success_exit_codes"] == [0, 3010])
        check("params carry the deployment id for roll-up",
              params["deployment_id"] == dep)
        check("params carry a download URL addressed by hash",
              params["source"]["download_url"].endswith("/api/agent/packages/" + "a" * 64))

        finish(db_path, pc1["command_id"], success=True)
        packages.tick(db_path, now=t0 + 10, ttl_seconds=900)
        check("a successful command marks the target succeeded",
              target_row(db_path, dep, "PC1")["status"] == packages.TARGET_SUCCEEDED)
        check("a succeeded target has no next attempt scheduled",
              target_row(db_path, dep, "PC1")["next_attempt_at"] is None)

        print("\n== Scheduler: failure, backoff, exhaustion ==")
        pc2 = target_row(db_path, dep, "PC2")
        claim_for(db_path, "PC2")
        finish(db_path, pc2["command_id"], success=False, output="msiexec exited 1603")
        packages.tick(db_path, now=t0 + 20, ttl_seconds=900)
        pc2 = target_row(db_path, dep, "PC2")
        check("a failed attempt goes back to pending, not failed (retries remain)",
              pc2["status"] == packages.TARGET_PENDING)
        check("the failure reason is recorded on the target",
              "1603" in (pc2["last_error"] or ""))
        check("the first retry waits one backoff interval",
              pc2["next_attempt_at"] == t0 + 20 + 600)
        check("a target in backoff is not dispatched early",
              packages.tick(db_path, now=t0 + 100, ttl_seconds=900)[1] == 0)

        packages.tick(db_path, now=t0 + 20 + 600, ttl_seconds=900)
        pc2 = target_row(db_path, dep, "PC2")
        check("the retry is dispatched once the backoff elapses",
              pc2["status"] == packages.TARGET_IN_FLIGHT and pc2["attempts"] == 2)

        claim_for(db_path, "PC2")
        finish(db_path, pc2["command_id"], success=False, output="msiexec exited 1603")
        packages.tick(db_path, now=t0 + 700, ttl_seconds=900)
        check("the final attempt failing marks the target failed",
              target_row(db_path, dep, "PC2")["status"] == packages.TARGET_FAILED)
        check("every target terminal rolls the deployment up to complete",
              deployment_status(db_path, dep) == packages.DEPLOY_COMPLETE)

        print("\n== Scheduler: an offline machine (TTL expiry) ==")
        off_dep = packages.create_deployment(
            db_path, package_id=deploy_pkg, machines=["OFFLINE1"],
            created_by="op@x.com", max_attempts=1, retry_backoff_seconds=600)
        packages.tick(db_path, now=soon(), ttl_seconds=60)
        row = target_row(db_path, off_dep, "OFFLINE1")
        check("a command is queued even for a machine that never answers",
              row["status"] == packages.TARGET_IN_FLIGHT)
        # Nobody claims it; the queue's own TTL retires it. That expiry IS the signal
        # the scheduler reads, rather than a second notion of delivery beside it.
        packages.tick(db_path, now=t0 + 3600, ttl_seconds=60)
        row = target_row(db_path, off_dep, "OFFLINE1")
        check("an expired command spends the attempt and fails the target",
              row["status"] == packages.TARGET_FAILED)
        check("the expiry reason is human-readable",
              "expired" in (row["last_error"] or ""))

        print("\n== Scheduler: windows ==")
        future = packages.create_deployment(
            db_path, package_id=deploy_pkg, machines=["PC9"], created_by="op@x.com",
            window_start=t0 + 10_000, window_end=t0 + 20_000)
        check("nothing dispatches before the window opens",
              packages.tick(db_path, now=t0, ttl_seconds=900)[1] == 0)
        check("the deployment stays 'scheduled' until its window opens",
              deployment_status(db_path, future) == packages.DEPLOY_SCHEDULED)
        check("it dispatches once the window is open",
              packages.tick(db_path, now=t0 + 10_001, ttl_seconds=900)[1] == 1)

        missed = packages.create_deployment(
            db_path, package_id=deploy_pkg, machines=["PC10"], created_by="op@x.com",
            window_start=t0 + 10_000, window_end=t0 + 20_000)
        packages.tick(db_path, now=t0 + 30_000, ttl_seconds=900)
        check("a target whose window closed is retired, not left pending forever",
              target_row(db_path, missed, "PC10")["status"] == packages.TARGET_EXPIRED)
        check("a fully-expired deployment reads complete",
              deployment_status(db_path, missed) == packages.DEPLOY_COMPLETE)

        print("\n== Cancel & retry ==")
        cancel_dep = packages.create_deployment(
            db_path, package_id=deploy_pkg, machines=["PC11", "PC12"],
            created_by="op@x.com")
        packages.tick(db_path, now=soon(), ttl_seconds=900)   # both in flight
        with packages.get_conn(db_path) as conn:
            conn.execute("UPDATE deployment_targets SET status = ?, command_id = NULL "
                         "WHERE deployment_id = ? AND machine = ?",
                         (packages.TARGET_PENDING, cancel_dep, "PC12"))
        packages.cancel_deployment(db_path, cancel_dep, actor="op@x.com")
        check("cancelling stops pending targets",
              target_row(db_path, cancel_dep, "PC12")["status"]
              == packages.TARGET_CANCELLED)
        # A command already on its way must keep its real outcome -- a row claiming
        # nothing happened while an installer runs is a record that lies.
        check("cancelling leaves an in-flight target alone",
              target_row(db_path, cancel_dep, "PC11")["status"]
              == packages.TARGET_IN_FLIGHT)
        check("a cancelled deployment dispatches nothing further",
              packages.tick(db_path, now=t0 + 10, ttl_seconds=900)[1] == 0)
        check("cancellation is sticky through roll-up",
              deployment_status(db_path, cancel_dep) == packages.DEPLOY_CANCELLED)

        requeued = packages.retry_deployment_failures(db_path, dep, actor="op@x.com")
        check("retry requeues only the failed target", requeued == 1)
        pc2 = target_row(db_path, dep, "PC2")
        check("a requeued target gets a fresh attempt budget",
              pc2["status"] == packages.TARGET_PENDING and pc2["attempts"] == 0)
        check("a requeued target's stale error is cleared", pc2["last_error"] is None)
        check("the succeeded target is not requeued",
              target_row(db_path, dep, "PC1")["status"] == packages.TARGET_SUCCEEDED)
        check("retrying an unknown deployment raises",
              raises(KeyError, packages.retry_deployment_failures, db_path, "nope"))

        print("\n== Deleting a package mid-flight ==")
        doomed_pkg = make_package(db_path, name="Doomed")
        doomed_dep = packages.create_deployment(
            db_path, package_id=doomed_pkg, machines=["PC13"], created_by="op@x.com")
        packages.delete_package(db_path, doomed_pkg, blob_root_dir=blob_dir)
        packages.tick(db_path, now=soon(), ttl_seconds=900)
        row = target_row(db_path, doomed_dep, "PC13")
        check("a target whose package vanished fails with a real reason",
              row["status"] == packages.TARGET_FAILED and "deleted" in (row["last_error"] or ""))
        check("deployment history survives its package being deleted",
              packages.get_deployment(db_path, doomed_dep) is not None)

        print("\n== Machine lifecycle ==")
        life_dep = packages.create_deployment(
            db_path, package_id=deploy_pkg, machines=["OLDNAME", "KEEPER"],
            created_by="op@x.com")
        packages.rename_machine(db_path, "OLDNAME", "NEWNAME")
        check("a merged machine's target follows the rename",
              target_row(db_path, life_dep, "NEWNAME") is not None
              and target_row(db_path, life_dep, "OLDNAME") is None)
        # A merge where both hostnames are already targets must not collide on the
        # (deployment_id, machine) primary key.
        packages.rename_machine(db_path, "KEEPER", "NEWNAME")
        check("a rename onto an existing target drops the duplicate instead of raising",
              target_row(db_path, life_dep, "KEEPER") is None)
        packages.forget_machine(db_path, "NEWNAME")
        check("deleting a machine drops its targets",
              target_row(db_path, life_dep, "NEWNAME") is None)
        check("a deployment left with no targets does not sit 'running' forever",
              deployment_status(db_path, life_dep) != packages.DEPLOY_RUNNING)

        print("\n== Command params for the agent ==")
        winget_pkg = packages.get_package(db_path, winget_id)
        winget_dep = {"id": "dep-x"}
        params = packages.build_command_params(winget_pkg, winget_dep,
                                               hub_url="https://hub.example.com/")
        check("a winget payload carries its id, not a download URL",
              params["source"]["id"] == "7zip.7zip"
              and "download_url" not in params["source"])
        upload_pkg = packages.get_package(db_path, deploy_pkg)
        params = packages.build_command_params(upload_pkg, winget_dep,
                                               hub_url="https://hub.example.com/")
        check("an absolute download URL is built from the hub URL",
              params["source"]["download_url"]
              == "https://hub.example.com/api/agent/packages/" + "a" * 64)
        check("the hash the agent verifies rides in the params",
              params["source"]["sha256"] == "a" * 64)

        print("\n== Multi-step packages: the recipe ==")
        # The motivating shape, end to end: a driver pack that downloads, unpacks and
        # installs. Three programs, three exit codes, one package.
        drivers = packages.create_package(
            db_path, name="Dell drivers",
            sources=[{"name": "pack", "kind": packages.SOURCE_URL,
                      "ref": "https://downloads.dell.com/pack.zip"}],
            steps=[{"kind": "extract", "archive": "{pack}", "save_as": "unpacked"},
                   {"kind": "pnputil", "path": "{unpacked}"}],
            actor="op@x.com")
        stored = packages.get_package(db_path, drivers)
        check("a step-based package keeps its steps in order",
              [s["kind"] for s in stored["steps"]] == ["extract", "pnputil"])
        check("a named payload keeps its slot name",
              stored["sources"][0]["name"] == "pack")
        # 259 is a driver install that simply ran out of INFs. It is defaulted by the HUB
        # so the editor can show it, rather than being hidden in the agent -- that is the
        # 3010 lesson applied one layer down.
        check("a pnputil step defaults to pnputil's own exit codes",
              stored["steps"][1]["success_exit_codes"]
              == list(packages.DEFAULT_PNPUTIL_EXIT_CODES))
        check("pnputil recurses by default", stored["steps"][1]["subdirs"] is True)

        # The property that makes a typo cheap: it fails at save time, not at 2am on 200
        # machines with a literal brace pair in an installer's arguments.
        check("a step naming a variable nothing provides is refused",
              raises(ValueError, packages.create_package, db_path, name="Typo",
                     sources=[{"name": "pack", "kind": packages.SOURCE_URL,
                               "ref": "https://x/pack.zip"}],
                     steps=[{"kind": "run", "command": "setup.exe", "args": "{unpacked}"}]))
        # Ordering is part of that: a variable a LATER step binds is not available yet.
        check("a step may not use a variable a later step binds",
              raises(ValueError, packages.create_package, db_path, name="Backwards",
                     sources=[{"name": "pack", "kind": packages.SOURCE_URL,
                               "ref": "https://x/pack.zip"}],
                     steps=[{"kind": "pnputil", "path": "{unpacked}"},
                            {"kind": "extract", "archive": "{pack}",
                             "save_as": "unpacked"}]))
        check("a payload no step ever opens is refused",
              raises(ValueError, packages.create_package, db_path, name="Unused payload",
                     sources=[{"name": "pack", "kind": packages.SOURCE_URL,
                               "ref": "https://x/pack.zip"}],
                     steps=[{"kind": "winget", "id": "7zip.7zip"}]))
        check("a package cannot carry both steps and a command line",
              raises(ValueError, packages.create_package, db_path, name="Both",
                     sources=[{"kind": packages.SOURCE_UPLOAD, "sha256": "c" * 64}],
                     install_command="setup.exe", install_args="{file}",
                     steps=[{"kind": "run", "command": "setup.exe", "args": "{file}"}]))
        check("a winget payload cannot back a step-based package",
              raises(ValueError, packages.create_package, db_path, name="Winget payload",
                     sources=[{"name": "app", "kind": packages.SOURCE_WINGET,
                               "ref": "7zip.7zip"}],
                     steps=[{"kind": "run", "command": "x.exe", "args": "{app}"}]))
        check("two payloads may not share a name",
              raises(ValueError, packages.create_package, db_path, name="Clashing",
                     sources=[{"name": "pack", "kind": packages.SOURCE_URL, "ref": "https://x/a"},
                              {"name": "pack", "kind": packages.SOURCE_URL, "ref": "https://x/b"}],
                     steps=[{"kind": "run", "command": "a.exe", "args": "{pack}"}]))
        check("more than one payload needs steps to say what to do with them",
              raises(ValueError, packages.create_package, db_path, name="Two payloads",
                     sources=[{"name": "a", "kind": packages.SOURCE_URL, "ref": "https://x/a"},
                              {"name": "b", "kind": packages.SOURCE_URL, "ref": "https://x/b"}],
                     install_command="setup.exe", install_args="{file}"))

        # A literal MSI product code is not a variable. Getting this wrong would reject
        # every uninstall-then-reinstall package ever written, which is why the grammar
        # excludes hyphens and uppercase rather than treating any brace pair as a name.
        guid = packages.create_package(
            db_path, name="Reinstall Office",
            sources=[{"name": "installer", "kind": packages.SOURCE_URL,
                      "ref": "https://x/setup.msi"}],
            steps=[{"kind": "run", "command": "msiexec.exe",
                    "args": "/x {90160000-008C-0000-1000-0000000FF1CE} /qn"},
                   {"kind": "run", "command": "msiexec.exe", "args": '/i "{installer}" /qn'}],
            actor="op@x.com")
        check("an MSI product code is left alone, not read as a variable",
              packages.get_package(db_path, guid)["steps"][0]["args"].startswith("/x {901"))

        print("\n== Multi-step packages: editing ==")
        # Steps and payloads are validated TOGETHER, so removing the payload a step uses
        # has to fail rather than leave the step pointing at nothing.
        check("dropping a payload a step still uses is refused",
              raises(ValueError, packages.update_package, db_path, drivers,
                     sources=[{"name": "other", "kind": packages.SOURCE_URL,
                               "ref": "https://x/other.zip"}]))
        converted = packages.update_package(
            db_path, drivers, steps=[], install_command="setup.exe",
            install_args='"{file}" /S', actor="op@x.com")
        check("a package can be converted back to one command",
              converted["steps"] == [] and converted["install_command"] == "setup.exe")

        print("\n== Multi-step packages: what the agent receives ==")
        step_params = packages.build_command_params(stored, {"id": "dep-y"},
                                                    hub_url="https://hub.example.com")
        check("the steps ride in the params as a snapshot",
              [s["kind"] for s in step_params["steps"]] == ["extract", "pnputil"])
        check("every payload rides with its slot name",
              [s["name"] for s in step_params["sources"]] == ["pack"])
        # The legacy projection. An agent that predates steps reads `source` and
        # `install_command` -- so a multi-step package deliberately gives it nothing it can
        # run, and that agent fails the deploy instead of silently running step one.
        check("a step package offers an old agent no command to run",
              step_params["install_command"] == ""
              and step_params["source"]["kind"] == "multi")
        legacy_params = packages.build_command_params(
            packages.get_package(db_path, deploy_pkg), {"id": "dep-z"},
            hub_url="https://hub.example.com")
        check("a one-command package still reaches an old agent unchanged",
              legacy_params["source"]["kind"] == packages.SOURCE_UPLOAD
              and legacy_params["install_command"] == "msiexec.exe"
              and legacy_params["steps"] == [])

        print("\n== Upgrading a database written before steps ==")
        # The migration is the risky part of this feature: every hub that already deploys
        # packages runs it, on rows nobody will look at again until a deploy goes out. So
        # the OLD schema is built here by hand rather than trusted to be gone.
        old_db = os.path.join(workdir, "old.db")
        fleet.init_fleet_db(old_db)
        with packages.get_conn(old_db) as conn:
            conn.execute(
                "CREATE TABLE packages (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "description TEXT, version TEXT, install_command TEXT NOT NULL, "
                "install_args TEXT, timeout_seconds INTEGER NOT NULL, "
                "success_exit_codes TEXT NOT NULL, detection_json TEXT NOT NULL, "
                "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
                "created_by TEXT, updated_by TEXT)")
            conn.execute(
                "CREATE TABLE package_sources (id TEXT PRIMARY KEY, package_id TEXT NOT NULL, "
                "kind TEXT NOT NULL, ref TEXT, sha256 TEXT, file_name TEXT, "
                "file_size INTEGER, created_at INTEGER NOT NULL)")
            # The constraint the new schema has to replace: one payload per package.
            conn.execute("CREATE UNIQUE INDEX idx_package_sources_package "
                         "ON package_sources(package_id)")
            conn.execute(
                "INSERT INTO packages(id, name, install_command, install_args, "
                "timeout_seconds, success_exit_codes, detection_json, created_at, "
                "updated_at) VALUES ('old1', 'Legacy', 'msiexec.exe', '/i \"{file}\" /qn', "
                "900, '[0, 3010]', '{\"kind\": \"none\"}', 1, 1)")
            conn.execute(
                "INSERT INTO package_sources(id, package_id, kind, sha256, file_name, "
                "created_at) VALUES ('s1', 'old1', 'upload', ?, 'old.msi', 1)", ("d" * 64,))

        packages.init_packages_db(old_db)
        migrated = packages.get_package(old_db, "old1")
        check("an existing package survives the migration",
              migrated is not None and migrated["install_command"] == "msiexec.exe")
        check("its unnamed payload becomes the default slot",
              migrated["sources"][0]["name"] == packages.DEFAULT_SOURCE_NAME)
        check("a package written before steps reads back with none",
              migrated["steps"] == [])
        check("`source` still answers for callers that predate the list",
              migrated["source"]["sha256"] == "d" * 64)
        # The old UNIQUE(package_id) index would refuse this outright.
        packages.update_package(
            old_db, "old1",
            sources=[{"name": "payload", "kind": packages.SOURCE_UPLOAD, "sha256": "d" * 64},
                     {"name": "config", "kind": packages.SOURCE_URL,
                      "ref": "https://x/config.xml"}],
            install_command="", install_args="",
            steps=[{"kind": "run", "command": "msiexec.exe", "args": '/i "{payload}" /qn'},
                   {"kind": "powershell", "script": 'Copy-Item "{config}" C:\\ProgramData\\'}],
            actor="op@x.com")
        check("a migrated package can then take a second payload",
              len(packages.get_package(old_db, "old1")["sources"]) == 2)
        packages.init_packages_db(old_db)
        check("running the migration twice changes nothing",
              len(packages.get_package(old_db, "old1")["sources"]) == 2)

        print("\n== Favorites & the command taxonomy ==")
        check("deploy_package is a known command type",
              packages.COMMAND_TYPE in fleet.ALL_COMMANDS)
        check("deploy_package cannot be saved as a favorite",
              raises(ValueError, fleet.create_favorite, db_path, "op@x.com",
                     "sneaky", packages.COMMAND_TYPE, {}))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
