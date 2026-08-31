"""Tests the hub's per-client version advertisement (app.get_advertised_version
and the latest_version it echoes from /api/report).

Only the C# agent train (3.x) is served now: the Python companion was removed from
the repo, so there is no 2.x release left to advertise. A pre-agent client must still
never be handed a 3.x number -- it would try to install an agent build as if it were a
Python script -- so it gets nothing at all and has to be migrated by hand. The wire
field is still called companion_version, because renaming it would break every agent
already in the field. These tests pin that routing.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import threading
import time
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

# app.py resolves LOG_DIR/DB_PATH relative to the cwd at import time, so run it
# against a throwaway directory rather than the real logs/temp_v2.db.
_TMPDIR = tempfile.mkdtemp(prefix="hub-version-test-")
# See test_alerts.py: app resolves its DB from HUB_LOG_DIR, so declare this module's dir
# before importing app to keep a standalone run off the real logs/.
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
# Pinned rather than inherited from a developer's .env: the hub-update endpoints below are
# gated on manage_settings, and ALLOWED_EMAILS membership is the break-glass grant that
# hands a session every capability. Set before importing app, which reads it at import time.
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app

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


def set_agent_version(agent, beta=None):
    """Seed the per-channel version cache.

    A dict since roadmap #21 -- the hub answers for both trains at once. `beta` defaults to
    None ("nothing published on beta"), which is the state of almost every real fleet and the
    one every assertion below assumes.
    """
    app.latest_agent_version = {app.channels.STABLE: agent, app.channels.BETA: beta}


def test_version_compare():
    print("\n-- cmp_versions --")
    check("2.10.1 > 2.9.9 (numeric, not lexical)", app.cmp_versions("2.10.1", "2.9.9") > 0)
    check("3.0.0 > 2.10.1", app.cmp_versions("3.0.0", "2.10.1") > 0)
    check("2.8 == 2.8.0 (zero padded)", app.cmp_versions("2.8", "2.8.0") == 0)
    check("suffix ignored", app.cmp_versions("3.0.1-rc1", "3.0.1") == 0)
    check("garbage sorts lowest", app.cmp_versions("garbage", "0.0.1") < 0)


def test_pre_agent_clients_get_nothing():
    print("\n-- pre-agent clients (2.x) are never served a version --")
    set_agent_version("3.0.1")
    check("2.8.0 -> None (companion.py is gone from main)",
          app.get_advertised_version("2.8.0") is None)
    check("2.10.0 -> None", app.get_advertised_version("2.10.0") is None)
    check("never handed an agent build", app.get_advertised_version("2.8.0") != "3.0.1")

    print("\n-- the last companion release is terminal too --")
    check(f"{app.COMPANION_FINAL_VERSION} -> None (waits to be migrated by hand)",
          app.get_advertised_version(app.COMPANION_FINAL_VERSION) is None)

    print("\n-- clients that report no usable version are treated as pre-agent --")
    check("None -> None", app.get_advertised_version(None) is None)
    check("'' -> None", app.get_advertised_version("") is None)
    check("garbage -> None", app.get_advertised_version("garbage") is None)


def test_agent_train():
    print("\n-- agent train (3.x) gets the latest agent --")
    set_agent_version("3.0.1")
    check("3.0.0 -> 3.0.1 (the regression this fixes)",
          app.get_advertised_version("3.0.0") == "3.0.1")
    check("3.0.1 -> 3.0.1 (no nudge)", app.get_advertised_version("3.0.1") == "3.0.1")
    check("agent never pushed back onto 2.x",
          app.get_advertised_version("3.0.0") != app.COMPANION_FINAL_VERSION)

    print("\n-- a newer agent release rolls forward without a hub change --")
    set_agent_version("3.4.0")
    check("3.0.1 -> 3.4.0", app.get_advertised_version("3.0.1") == "3.4.0")
    check("2.8.0 still -> None", app.get_advertised_version("2.8.0") is None)


def test_unknown_train():
    print("\n-- nothing known yet: omit rather than guess --")
    set_agent_version(None)
    check("agent client with no manifest read yet -> None",
          app.get_advertised_version("3.0.0") is None)
    check("pre-agent client -> None", app.get_advertised_version("2.8.0") is None)

    set_agent_version("3.0.1")
    check("agent client served once the manifest is read",
          app.get_advertised_version("3.0.1") == "3.0.1")
    check("pre-agent client still -> None", app.get_advertised_version("2.8.0") is None)


def test_report_endpoint():
    print("\n-- /api/report echoes the agent train only --")
    set_agent_version("3.0.1")
    client = app.app.test_client()

    def report(version):
        payload = {"machine": "version-test-box", "temp": 42.0}
        if version is not None:
            payload["companion_version"] = version
        resp = client.post("/api/report", json=payload)
        return resp.status_code, resp.get_json()

    status, body = report("2.8.0")
    check("pre-agent client: 200", status == 200)
    check("pre-agent client: latest_version omitted", "latest_version" not in body)

    status, body = report(app.COMPANION_FINAL_VERSION)
    check("last companion release: 200", status == 200)
    check("last companion release: latest_version omitted", "latest_version" not in body)

    status, body = report("3.0.0")
    check("agent: 200", status == 200)
    check("agent: latest_version=3.0.1", body.get("latest_version") == "3.0.1")

    status, body = report(None)
    check("no version field: 200", status == 200)
    check("no version field: latest_version omitted", "latest_version" not in body)

    set_agent_version(None)
    status, body = report("3.0.0")
    check("unknown train: 200", status == 200)
    check("unknown train: latest_version omitted", "latest_version" not in body)


def test_hub_self_update():
    print("\n-- hub self-update: parse_hub_version --")
    check("parses double-quoted", app.parse_hub_version('HUB_VERSION = "1.14.0"\n') == "1.14.0")
    check("parses single-quoted", app.parse_hub_version("HUB_VERSION = '2.0.3'") == "2.0.3")
    check("ignores non-anchored text", app.parse_hub_version('X_HUB_VERSION = "9.9.9"') is None)
    check("none when absent", app.parse_hub_version("nothing here") is None)
    check("first match wins",
          app.parse_hub_version('HUB_VERSION = "1.0.0"\nHUB_VERSION = "2.0.0"') == "1.0.0")

    print("\n-- hub self-update: update decision --")
    check("remote ahead triggers", app.cmp_versions("999.0.0", app.HUB_VERSION) > 0)
    check("same version no update", app.cmp_versions(app.HUB_VERSION, app.HUB_VERSION) == 0)
    check("older remote does not trigger", app.cmp_versions("0.0.1", app.HUB_VERSION) < 0)

    print("\n-- hub self-update: tri-state enable flag --")
    # The gate moved out of start_hub_update_watcher() and into hub_auto_update_enabled(),
    # which the loop re-reads every tick. The watcher thread now always starts, because a
    # thread that was never started can't notice the setting being switched on at runtime.
    import settings as _settings
    saved_env = app.HUB_AUTO_UPDATE_ENV
    try:
        for env_flag in (False, True):
            app.HUB_AUTO_UPDATE_ENV = env_flag
            _settings.reset(app.DB_PATH, ["hub.auto_update"])
            check(f"unset setting follows .env ({env_flag})",
                  app.hub_auto_update_enabled() is env_flag)
            _settings.set_many(app.DB_PATH, {"hub.auto_update": True})
            check(f"explicit True overrides .env ({env_flag})",
                  app.hub_auto_update_enabled() is True)
            _settings.set_many(app.DB_PATH, {"hub.auto_update": False})
            check(f"explicit False overrides .env ({env_flag})",
                  app.hub_auto_update_enabled() is False)
    finally:
        app.HUB_AUTO_UPDATE_ENV = saved_env
        _settings.reset(app.DB_PATH, ["hub.auto_update"])

    print("\n-- hub self-update: watcher starts regardless of the flag --")
    # Stub the fetch so the loop never touches the network.
    orig_fetch = app.fetch_remote_hub_version
    app.fetch_remote_hub_version = lambda: None
    app.hub_update_watcher_thread = None
    try:
        app.start_hub_update_watcher()
        check("watcher thread alive so a runtime toggle can take effect",
              app.hub_update_watcher_thread is not None and app.hub_update_watcher_thread.is_alive())
    finally:
        app.fetch_remote_hub_version = orig_fetch

    print("\n-- hub self-update: perform_hub_update resets a checkout to origin/main --")
    # The hub's code lives under hub/ now; the .git that selects the git strategy sits at the
    # worktree root, one level up from the code dir passed to perform_hub_update.
    import subprocess as _sp

    def _git(cwd, *args):
        _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=cwd, capture_output=True, text=True, check=True)
    try:
        base = tempfile.mkdtemp(prefix="hub-selfupdate-")
        origin = os.path.join(base, "origin")
        os.makedirs(os.path.join(origin, "hub"))
        _git(origin, "init", "-b", "main")
        with open(os.path.join(origin, "hub", "app.py"), "w") as f:
            f.write('HUB_VERSION = "1.0.0"\n')
        open(os.path.join(origin, "hub", "requirements.txt"), "w").close()
        _git(origin, "add", "-A")
        _git(origin, "commit", "-m", "v1")
        work = os.path.join(base, "work")
        _sp.run(["git", "clone", origin, work], capture_output=True, text=True, check=True)
        # origin advances; the hub's checkout must fast-follow via reset --hard.
        with open(os.path.join(origin, "hub", "app.py"), "w") as f:
            f.write('HUB_VERSION = "2.0.0"\n')
        _git(origin, "commit", "-am", "v2")
        ok = app.perform_hub_update(os.path.join(work, "hub"))
        with open(os.path.join(work, "hub", "app.py")) as f:
            pulled = f.read()
        check("perform_hub_update returned True", ok is True)
        check("checkout advanced to origin/main (2.0.0)", app.parse_hub_version(pulled) == "2.0.0")
    except Exception as e:
        check(f"perform_hub_update dry run (unexpected error: {e})", False)

    print("\n-- hub self-update: archive path replaces files in a non-clone install --")
    # The installer no longer clones, so most hubs have no .git and update from the
    # branch archive instead. Serve a synthetic zip rather than hitting the network.
    import io as _io, zipfile as _zf

    def _make_archive(version="2.0.0", omit=()):
        # The archive lays the hub's code + assets under a hub/ subdir of the single
        # <repo>-<branch>/ root -- there is no allowlist anymore, so this is just a small
        # representative set (the entrypoints the sanity floor checks, plus mirrored dirs).
        files = {"app.py": f'HUB_VERSION = "{version}"\n', "wsgi.py": "", "requirements.txt": ""}
        dirs = ("templates", "static")
        buf = _io.BytesIO()
        with _zf.ZipFile(buf, "w") as z:
            for name, body in files.items():
                if name in omit:
                    continue
                z.writestr(f"Temp_Monitor-main/hub/{name}", body)
            for d in dirs:
                if d in omit:
                    continue
                z.writestr(f"Temp_Monitor-main/hub/{d}/keep.txt", "x")
        return buf.getvalue()

    class _Resp:
        def __init__(self, content): self.content = content
        def raise_for_status(self): pass

    orig_get = app.requests.get
    orig_install = app._install_requirements
    try:
        # Happy path: the archive's hub/ is mirrored into the code dir -- files replaced,
        # a stale file inside a mirrored dir removed, a stale top-level module pruned -- while
        # operator state one level up in the install root is left untouched.
        state_root = tempfile.mkdtemp(prefix="hub-archive-state-")
        code_dir = os.path.join(state_root, "hub")
        os.makedirs(code_dir)
        with open(os.path.join(code_dir, "app.py"), "w") as f:
            f.write('HUB_VERSION = "1.0.0"\n')
        with open(os.path.join(code_dir, "gone.py"), "w") as f:
            f.write("# removed upstream -- must be pruned")
        os.makedirs(os.path.join(code_dir, "templates"))
        with open(os.path.join(code_dir, "templates", "gone.html"), "w") as f:
            f.write("stale")
        # Operator data lives in the install root, one level above the code dir.
        with open(os.path.join(state_root, ".env"), "w") as f:
            f.write("HUB_URL=https://example.test\n")
        os.makedirs(os.path.join(state_root, "logs"))
        with open(os.path.join(state_root, "logs", "temp_v2.db"), "w") as f:
            f.write("dbcontents")

        app.requests.get = lambda *a, **k: _Resp(_make_archive())
        app._install_requirements = lambda d: None
        ok = app.perform_hub_update(code_dir)
        with open(os.path.join(code_dir, "app.py")) as f:
            pulled = f.read()

        check("archive update returned True", ok is True)
        check("app.py advanced to 2.0.0", app.parse_hub_version(pulled) == "2.0.0")
        check("stale template removed by dir mirror",
              not os.path.exists(os.path.join(code_dir, "templates", "gone.html")))
        check("stale top-level module pruned",
              not os.path.exists(os.path.join(code_dir, "gone.py")))
        check(".env in install root preserved", os.path.exists(os.path.join(state_root, ".env")))
        check("logs/ in install root preserved",
              open(os.path.join(state_root, "logs", "temp_v2.db")).read() == "dbcontents")

        # Fail-closed: an archive whose hub/ is missing an entrypoint must not be applied,
        # or the hub loses a module and crash-loops on restart.
        state2 = tempfile.mkdtemp(prefix="hub-archive-bad-")
        code2 = os.path.join(state2, "hub")
        os.makedirs(code2)
        with open(os.path.join(code2, "app.py"), "w") as f:
            f.write('HUB_VERSION = "1.0.0"\n')
        app.requests.get = lambda *a, **k: _Resp(_make_archive(omit=("app.py",)))
        ok_bad = app.perform_hub_update(code2)
        with open(os.path.join(code2, "app.py")) as f:
            untouched = f.read()
        check("incomplete archive refused", ok_bad is False)
        check("live tree untouched after refusal",
              app.parse_hub_version(untouched) == "1.0.0")

        # Dispatch: a .git at the worktree root (the parent of the code dir) alone decides
        # which strategy runs. A developer's checkout must never be overwritten by the archive.
        orig_git_fn, orig_archive_fn = app._perform_hub_update_git, app._perform_hub_update_archive
        try:
            app._perform_hub_update_git = lambda root: "git"
            app._perform_hub_update_archive = lambda d: "archive"
            clone_root = tempfile.mkdtemp(prefix="hub-dispatch-clone-")
            os.makedirs(os.path.join(clone_root, ".git"))
            clone_code = os.path.join(clone_root, "hub")
            os.makedirs(clone_code)
            sparse_code = os.path.join(tempfile.mkdtemp(prefix="hub-dispatch-sparse-"), "hub")
            os.makedirs(sparse_code)
            check("code dir under a .git worktree routes to git",
                  app.perform_hub_update(clone_code) == "git")
            check("code dir with no .git parent routes to archive",
                  app.perform_hub_update(sparse_code) == "archive")
        finally:
            app._perform_hub_update_git = orig_git_fn
            app._perform_hub_update_archive = orig_archive_fn
    except Exception as e:
        check(f"archive update dry run (unexpected error: {e})", False)
    finally:
        app.requests.get = orig_get
        app._install_requirements = orig_install


def test_hub_update_notice():
    """The console half: the watcher's cached view of main, and the two endpoints the
    sidebar notice reads and acts on.

    The point of the first block is that the version read is NOT behind the auto-update
    gate any more. It used to be, and a hub with self-update off could therefore never
    tell anyone that a release existed -- which is exactly the deployment the notice is
    for. So: cached either way, applied only when enabled.
    """
    import settings as _settings

    saved_latest = app.latest_hub_version
    saved_env = app.HUB_AUTO_UPDATE_ENV
    orig_fetch = app.fetch_remote_hub_version
    orig_perform = app.perform_hub_update

    def _tick():
        """One pass of hub_update_watcher's body, without its sleep."""
        applied.clear()
        app.hub_update_watcher_thread = None
        thread = threading.Thread(target=app.hub_update_watcher, daemon=True)
        thread.start()
        # The loop caches, decides, then sleeps for 15 minutes; a moment is plenty.
        time.sleep(0.5)

    applied = []
    try:
        print("\n-- hub update notice: the version read is not behind the auto-update gate --")
        app.fetch_remote_hub_version = lambda: "999.0.0"
        app.perform_hub_update = lambda code_dir: applied.append(code_dir) or False
        _settings.set_many(app.DB_PATH, {"hub.auto_update": False})
        app.latest_hub_version = None
        _tick()
        check("watcher caches main's version with self-update off",
              app.get_latest_hub_version() == "999.0.0")
        check("...and does not install it", applied == [])
        check("hub_update_available() sees the newer version",
              app.hub_update_available() is True)

        _settings.set_many(app.DB_PATH, {"hub.auto_update": True})
        _tick()
        check("with self-update on it still installs", applied == [app.HUB_CODE_DIR])
        _settings.set_many(app.DB_PATH, {"hub.auto_update": False})

        print("\n-- hub update notice: availability --")
        app.latest_hub_version = None
        check("unknown remote is not an update", app.hub_update_available() is False)
        app.latest_hub_version = app.HUB_VERSION
        check("same version is not an update", app.hub_update_available() is False)
        app.latest_hub_version = "0.0.1"
        check("older remote is not an update", app.hub_update_available() is False)
        app.latest_hub_version = "999.0.0"
        check("newer remote is an update", app.hub_update_available() is True)

        print("\n-- hub update notice: /api/hub/version --")
        client = app.app.test_client()
        check("signed out -> not served", client.get("/api/hub/version").status_code != 200)

        with client.session_transaction() as sess:
            sess["user"] = {"email": "nobody@example.com"}
        check("signed in without manage_settings -> 403",
              client.get("/api/hub/version").status_code == 403)
        check("...and cannot trigger an update either",
              client.post("/api/hub/update", json={}).status_code == 403)

        with client.session_transaction() as sess:
            sess["user"] = {"email": "tester@example.com"}
        resp = client.get("/api/hub/version")
        body = resp.get_json()
        check("manage_settings -> 200", resp.status_code == 200)
        check("reports the running version", body["current"] == app.HUB_VERSION)
        check("reports main's version", body["latest"] == "999.0.0")
        check("reports update_available", body["update_available"] is True)
        check("reports the auto-update setting", body["auto_update"] is False)
        check("reports idle status", body["status"] == "idle")

        print("\n-- hub update notice: POST /api/hub/update --")
        app.latest_hub_version = app.HUB_VERSION
        resp = client.post("/api/hub/update", json={})
        check("nothing newer -> 409", resp.status_code == 409)

        app.latest_hub_version = "999.0.0"
        # Stub the worker rather than the update itself: the real one ends in os._exit,
        # which would take the test runner with it.
        orig_worker = app._hub_update_worker
        started = []
        app._hub_update_worker = lambda target: started.append(target)
        try:
            resp = client.post("/api/hub/update", json={})
            body = resp.get_json()
            time.sleep(0.2)  # the worker runs on its own thread
            check("update accepted -> 202", resp.status_code == 202)
            check("names the target version", body["to"] == "999.0.0")
            check("worker started with the target version", started == ["999.0.0"])

            import fleet as _fleet
            hit = _fleet.list_audit(app.DB_PATH, action="hub.update", limit=200)["entries"]
            check("audited", len(hit) == 1)
            check("audited at security level -- this runs code pulled from main",
                  hit and hit[0]["level"] == _fleet.LEVEL_SECURITY)
            check("attributed to the signed-in operator",
                  hit and hit[0]["actor"] == "tester@example.com")

            # The state left behind by that POST is what makes the next one a 409, and
            # what the notice reads to say "updating" instead of offering the button again.
            check("status is running", client.get("/api/hub/version").get_json()["status"] == "running")
            check("a second update while one runs -> 409",
                  client.post("/api/hub/update", json={}).status_code == 409)
        finally:
            app._hub_update_worker = orig_worker
            app._set_hub_update_state("idle")
    finally:
        app.fetch_remote_hub_version = orig_fetch
        app.perform_hub_update = orig_perform
        app.latest_hub_version = saved_latest
        app.HUB_AUTO_UPDATE_ENV = saved_env
        _settings.reset(app.DB_PATH, ["hub.auto_update"])


def test_hub_update_notice_hides():
    """The notice must actually disappear when it is marked hidden.

    Both the server (`{% if not hub_update_available %}hidden{% endif %}`) and the poller
    (`els.notice.hidden = ...`) say "gone" by setting the [hidden] attribute -- and that
    attribute only hides an element because of a UA-stylesheet rule that ANY `display`
    declaration outranks. .sidebar__update.banner sets display:flex, so without an explicit
    rule the notice stayed on screen permanently, announcing an update on a hub already
    running the latest version. Pinned here because nothing else in the suite renders CSS.
    """
    print("\n-- hub update notice: [hidden] actually hides --")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static = os.path.join(root, "hub", "static")
    with open(os.path.join(static, "css", "components.css"), encoding="utf-8") as fh:
        css = fh.read()

    check("the notice has a [hidden] rule beating its own display",
          ".sidebar__update.banner[hidden]" in css)
    # Same trap, one element down: the dismiss button is display:flex and common.js hides
    # it while an update is applying.
    check("so does the dismiss button", ".sidebar__update-dismiss[hidden]" in css)

    # A rule that lands BEFORE the display it has to beat loses on source order at equal
    # specificity, which is how this class of bug survives a grep for the selector.
    check("the [hidden] rule comes after the display it overrides",
          css.index(".sidebar__update.banner {") < css.index(".sidebar__update.banner[hidden]"))

    # And the server still marks it hidden when there is nothing to announce -- the CSS
    # above only matters if this attribute is on the element.
    with open(os.path.join(root, "hub", "templates", "partials", "_sidebar.html"),
                 encoding="utf-8") as fh:
        sidebar = fh.read()
    check("the sidebar renders [hidden] when no update is available",
          "{% if not hub_update_available %}hidden{% endif %}" in sidebar)


def test_version_format_policy():
    """The mechanical half of VERSIONING.md: MAJOR.MINOR.PATCH, three numeric components,
    no suffixes, and the two-file version pairs in sync.

    None of this is house style. `parse_hub_version` accepts only [\\d.] inside the quotes,
    so a suffixed HUB_VERSION does not raise -- it parses as None and the hub silently
    stops discovering updates fleet-wide. The two `versionLess` copies in the console loop
    over exactly three components. And the four comparators in this repo agree on
    well-formed input while disagreeing on anything else. So the format is pinned here,
    where a bad bump fails a test instead of a deployment.
    """
    import re

    print("\n-- versioning policy: format --")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(*parts):
        # errors="replace": AgentConfig.cs is not UTF-8 (a cp1252 dash in a comment), and
        # this only ever regexes ASCII version strings out of the text.
        with open(os.path.join(root, *parts), encoding="utf-8", errors="replace") as fh:
            return fh.read()

    THREE = re.compile(r"^\d+\.\d+\.\d+$")

    check(f"HUB_VERSION is MAJOR.MINOR.PATCH ({app.HUB_VERSION})",
          bool(THREE.match(app.HUB_VERSION)))
    # Round trip through the parser the self-updater actually uses, against the real file:
    # this is what a hub reads off main to decide whether it is behind.
    check("the declaration in hub/app.py parses back to the running version",
          app.parse_hub_version(read("hub", "app.py")) == app.HUB_VERSION)
    # The demonstration, not a hypothetical: this is why suffixes are banned outright.
    check("a suffixed HUB_VERSION would parse as None (hence: no suffixes)",
          app.parse_hub_version('HUB_VERSION = "1.83.0-rc1"') is None)

    print("\n-- versioning policy: the two-file pairs stay in sync --")
    agent_cs = re.search(r'public const string Version\s*=\s*"([^"]+)"',
                         read("agent", "src", "TempMonitorAgent", "AgentConfig.cs"))
    agent_proj = re.search(r"<Version>([^<]+)</Version>",
                           read("agent", "src", "TempMonitorAgent", "TempMonitorAgent.csproj"))
    check("AgentConfig.cs declares a version", agent_cs is not None)
    check("the csproj declares a version", agent_proj is not None)
    if agent_cs and agent_proj:
        check(f"agent version is MAJOR.MINOR.PATCH ({agent_cs.group(1)})",
              bool(THREE.match(agent_cs.group(1))))
        # release.ps1 writes both; a hand-edit that moves one is the failure this catches.
        check("AgentConfig.Version == csproj <Version>",
              agent_cs.group(1) == agent_proj.group(1))

    dart = re.search(r"const String clientVersion\s*=\s*'([^']+)'",
                     read("app", "lib", "version.dart"))
    pubspec = re.search(r"^version:\s*(\S+)\s*$", read("app", "pubspec.yaml"), re.MULTILINE)
    check("version.dart declares a version", dart is not None)
    check("pubspec.yaml declares a version", pubspec is not None)
    if dart and pubspec:
        check(f"client version is MAJOR.MINOR.PATCH ({dart.group(1)})",
              bool(THREE.match(dart.group(1))))
        # pubspec carries a +build suffix that version.dart deliberately does not; the
        # part before the + is the number operators and the Download page compare.
        check("clientVersion == pubspec version (ignoring +build)",
              dart.group(1) == pubspec.group(1).split("+")[0])

    print("\n-- versioning policy: the agent-capability gates are well-formed --")
    # These constants ARE the compatibility contract -- they name the agent minor that
    # introduced a feature -- and they are read by a comparator that loops exactly three
    # times, so a two- or four-component gate would misfire rather than fail loudly.
    gates = []
    for name in ("fleet-terminal.js", "processes.js"):
        js = read("hub", "static", "js", name)
        gates += re.findall(r"(MIN_[A-Z_]*AGENT)\s*=\s*'([^']+)'", js)
    check(f"found the MIN_*_AGENT gates ({len(gates)})", len(gates) >= 4)
    for gate_name, value in gates:
        check(f"{gate_name} = '{value}' is MAJOR.MINOR.PATCH", bool(THREE.match(value)))

    print("\n-- versioning policy: the policy is written down --")
    policy = read("VERSIONING.md")
    check("VERSIONING.md exists and states the format",
          "MAJOR.MINOR.PATCH" in policy)
    check("README points at it", "VERSIONING.md" in read("README.md"))


if __name__ == "__main__":
    test_version_compare()
    test_pre_agent_clients_get_nothing()
    test_agent_train()
    test_unknown_train()
    test_report_endpoint()
    test_hub_self_update()
    test_hub_update_notice()
    test_hub_update_notice_hides()
    test_version_format_policy()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
