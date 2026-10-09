"""Pins HUB_UPDATE_MODE=image: a hub running from a container image never updates itself.

The silent failure this exists to catch: an in-container self-update *looks* like it worked.
The archive swap rewrites hub/ inside the container's own layer, pip writes into the image's
site-packages, os._exit hands control to Docker's restart policy, and the hub comes back
reporting the new version. Then the next `docker compose up -d` (a host reboot with a changed
compose file, a pulled image, a volume tweak) recreates the container from the image, and the
hub is quietly back on the old version while the audit log says it was updated. Nothing errors.

So every route by which an update can start must refuse in image mode -- the watcher, the
operator's POST, perform_hub_update itself, and wsgi.py's boot-time self-heal -- even when the
hub.auto_update setting carried over from the Windows install says "on". And the Dockerfile
must actually set the mode, or all of the above is dead code in production.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile
import threading
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "hub"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import console_session

_TMPDIR = tempfile.mkdtemp(prefix="hub-update-mode-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"
os.environ["HUB_SKIP_REMOTE_CHECKS"] = "1"
# Mixed case and padding on purpose: the Dockerfile sets "image", but an operator overriding
# it in compose should not have to match case to get the safe behaviour.
os.environ["HUB_UPDATE_MODE"] = " Image "

import app
import settings

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


def test_mode_parsed():
    print("\n-- HUB_UPDATE_MODE parse --")
    check("' Image ' resolves to image", app.HUB_UPDATE_MODE == "image")


def test_auto_update_forced_off():
    print("\n-- hub_auto_update_enabled is False whatever the setting says --")
    saved_env = app.HUB_AUTO_UPDATE_ENV
    try:
        # A DB restored from the Windows install may carry this, and .env may too.
        app.HUB_AUTO_UPDATE_ENV = True
        settings.set_many(app.DB_PATH, {"hub.auto_update": True})
        check("setting on + env on -> still False", app.hub_auto_update_enabled() is False)
    finally:
        app.HUB_AUTO_UPDATE_ENV = saved_env
        settings.reset(app.DB_PATH, ["hub.auto_update"])


def test_perform_refuses():
    print("\n-- perform_hub_update refuses in image mode --")
    called = []
    orig_git, orig_archive = app._perform_hub_update_git, app._perform_hub_update_archive
    app._perform_hub_update_git = lambda root: called.append("git") or True
    app._perform_hub_update_archive = lambda d: called.append("archive") or True
    try:
        check("returns False", app.perform_hub_update(app.HUB_CODE_DIR) is False)
        check("neither strategy ran", called == [])
    finally:
        app._perform_hub_update_git, app._perform_hub_update_archive = orig_git, orig_archive


def test_watcher_reads_but_never_installs():
    # Asserts on the read, not on the cached value: the watcher app.py started at import is
    # still making its first real GitHub read, and whatever main says would land over a
    # seeded "999.0.0" on a thread this test does not own. That the read is cached is
    # test_versions.py's job; this module only cares that nothing installs.
    print("\n-- the watcher still reads main's version, and never installs it --")
    orig_fetch, orig_perform = app.fetch_remote_hub_version, app.perform_hub_update
    fetched, applied = [], []
    try:
        app.fetch_remote_hub_version = lambda: fetched.append(1) or "999.0.0"
        app.perform_hub_update = lambda code_dir: applied.append(code_dir) or False
        settings.set_many(app.DB_PATH, {"hub.auto_update": True})
        threading.Thread(target=app.hub_update_watcher, daemon=True).start()
        time.sleep(0.5)
        check("reads main's version, so the notice can say so", len(fetched) >= 1)
        check("does not call perform_hub_update", applied == [])
    finally:
        app.fetch_remote_hub_version, app.perform_hub_update = orig_fetch, orig_perform
        settings.reset(app.DB_PATH, ["hub.auto_update"])


def test_routes():
    print("\n-- /api/hub/version and POST /api/hub/update --")
    # The getter is stubbed rather than the cache seeded, for the race described above.
    orig_latest = app.get_latest_hub_version
    orig_worker = app._hub_update_worker
    started = []
    app._hub_update_worker = lambda target: started.append(target)
    try:
        app.get_latest_hub_version = lambda: "999.0.0"
        client = app.app.test_client()
        console_session.sign_in(client, "tester@example.com")
        body = client.get("/api/hub/version").get_json()
        check("version reports update_mode=image", body.get("update_mode") == "image")
        check("...and the update as available", body.get("update_available") is True)
        resp = client.post("/api/hub/update", json={})
        time.sleep(0.2)
        check("POST refused with 409 although an update is available", resp.status_code == 409)
        check("no worker started", started == [])
        check("status stays idle", app.get_hub_update_state()["status"] == "idle")

        page = client.get("/").get_data(as_text=True)
        check("sidebar renders the pull-the-image sentence",
              "Pull the new container image" in page)
    finally:
        app._hub_update_worker = orig_worker
        app.get_latest_hub_version = orig_latest
        app._set_hub_update_state("idle")


def test_wsgi_self_heal_skipped():
    print("\n-- wsgi.py does not self-heal from main in image mode --")
    import urllib.request
    import wsgi
    fetched = []
    orig_root, orig_open = wsgi._WORKTREE_ROOT, urllib.request.urlopen
    # A root with no .git, so the dev-checkout guard does not answer first.
    wsgi._WORKTREE_ROOT = tempfile.mkdtemp(prefix="hub-update-mode-root-")
    urllib.request.urlopen = lambda *a, **k: fetched.append(a) or (_ for _ in ()).throw(OSError("no"))
    try:
        healed = wsgi._self_heal_missing_modules(ModuleNotFoundError("No module named 'x'"))
        check("returns False", healed is False)
        check("never reached for the archive", fetched == [])
    finally:
        wsgi._WORKTREE_ROOT, urllib.request.urlopen = orig_root, orig_open


def test_dockerfile_sets_mode():
    print("\n-- hub/Dockerfile actually sets image mode --")
    with open(os.path.join(_REPO, "hub", "Dockerfile"), encoding="utf-8") as fh:
        text = fh.read()
    check("Dockerfile sets HUB_UPDATE_MODE=image", "HUB_UPDATE_MODE=image" in text)
    check("Dockerfile serves with 128 waitress threads", "--threads=128" in text)


def main():
    test_mode_parsed()
    test_auto_update_forced_off()
    test_perform_refuses()
    test_watcher_reads_but_never_installs()
    test_routes()
    test_wsgi_self_heal_skipped()
    test_dockerfile_sets_mode()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
