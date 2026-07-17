"""Tests the hub's per-client version advertisement (app.get_advertised_version
and the latest_version it echoes from /api/report).

The fleet runs two update trains that share one companion_version field, so a
single global "latest version" strands one of them: advertise 2.10.1 and every
3.x agent stops updating; advertise 3.0.1 and every 2.x companion tries to
install an agent build as if it were companion.py. These tests pin the routing.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# app.py resolves LOG_DIR/DB_PATH relative to the cwd at import time, so run it
# against a throwaway directory rather than the real logs/temp_v2.db.
_TMPDIR = tempfile.mkdtemp(prefix="hub-version-test-")
os.chdir(_TMPDIR)

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


def set_trains(companion, agent):
    app.latest_companion_version = companion
    app.latest_agent_version = agent


def test_version_compare():
    print("\n-- cmp_versions --")
    check("2.10.1 > 2.9.9 (numeric, not lexical)", app.cmp_versions("2.10.1", "2.9.9") > 0)
    check("3.0.0 > 2.10.1", app.cmp_versions("3.0.0", "2.10.1") > 0)
    check("2.8 == 2.8.0 (zero padded)", app.cmp_versions("2.8", "2.8.0") == 0)
    check("suffix ignored", app.cmp_versions("3.0.1-rc1", "3.0.1") == 0)
    check("garbage sorts lowest", app.cmp_versions("garbage", "0.0.1") < 0)


def test_companion_train():
    print("\n-- companion train (2.x) climbs to the migration release --")
    set_trains("2.10.1", "3.0.1")
    check("2.8.0 -> 2.10.1 (stepping stone)", app.get_advertised_version("2.8.0") == "2.10.1")
    check("2.10.0 -> 2.10.1", app.get_advertised_version("2.10.0") == "2.10.1")
    check("never advertised an agent build", app.get_advertised_version("2.8.0") != "3.0.1")

    print("\n-- clients that report no usable version fall back to companion --")
    check("None -> 2.10.1", app.get_advertised_version(None) == "2.10.1")
    check("'' -> 2.10.1", app.get_advertised_version("") == "2.10.1")
    check("garbage -> 2.10.1", app.get_advertised_version("garbage") == "2.10.1")


def test_companion_final_is_terminal():
    print("\n-- a companion at the migration release is done taking hints --")
    set_trains("2.10.1", "3.0.1")
    check("2.10.1 -> None (waits to be replaced by the agent)",
          app.get_advertised_version("2.10.1") is None)
    check("2.10.1 never handed the agent version (would hammer GitHub every 5s)",
          app.get_advertised_version("2.10.1") != "3.0.1")

    print("\n-- ...even if companion.py on main moves past it --")
    set_trains("2.11.0", "3.0.1")
    check("2.10.1 still -> None (no companion-to-companion update)",
          app.get_advertised_version("2.10.1") is None)
    check("2.8.0 still climbs toward the ladder", app.get_advertised_version("2.8.0") == "2.11.0")


def test_agent_train():
    print("\n-- agent train (3.x) gets the latest agent --")
    set_trains("2.10.1", "3.0.1")
    check("3.0.0 -> 3.0.1 (the regression this fixes)",
          app.get_advertised_version("3.0.0") == "3.0.1")
    check("3.0.1 -> 3.0.1 (no nudge)", app.get_advertised_version("3.0.1") == "3.0.1")
    check("agent never pushed back onto 2.x", app.get_advertised_version("3.0.0") != "2.10.1")

    print("\n-- a newer agent release rolls forward without a hub change --")
    set_trains("2.10.1", "3.4.0")
    check("3.0.1 -> 3.4.0", app.get_advertised_version("3.0.1") == "3.4.0")
    check("2.8.0 still -> 2.10.1", app.get_advertised_version("2.8.0") == "2.10.1")


def test_unknown_trains():
    print("\n-- nothing known yet: omit rather than guess --")
    set_trains(None, None)
    check("companion client -> None", app.get_advertised_version("2.8.0") is None)
    check("agent client -> None", app.get_advertised_version("3.0.0") is None)

    set_trains("2.10.1", None)
    check("agent client with no manifest read yet -> None (not 2.10.1)",
          app.get_advertised_version("3.0.0") is None)
    check("companion client still served", app.get_advertised_version("2.8.0") == "2.10.1")

    set_trains(None, "3.0.1")
    check("agent client served with no companion read", app.get_advertised_version("3.0.1") == "3.0.1")
    check("companion client with no companion read -> None", app.get_advertised_version("2.8.0") is None)


def test_report_endpoint():
    print("\n-- /api/report echoes the right train --")
    set_trains("2.10.1", "3.0.1")
    client = app.app.test_client()

    def report(version):
        payload = {"machine": "version-test-box", "temp": 42.0}
        if version is not None:
            payload["companion_version"] = version
        resp = client.post("/api/report", json=payload)
        return resp.status_code, resp.get_json()

    status, body = report("2.8.0")
    check("old companion: 200", status == 200)
    check("old companion: latest_version=2.10.1", body.get("latest_version") == "2.10.1")

    status, body = report("2.10.1")
    check("companion at migration release: 200", status == 200)
    check("companion at migration release: latest_version omitted",
          "latest_version" not in body)

    status, body = report("3.0.0")
    check("agent: 200", status == 200)
    check("agent: latest_version=3.0.1", body.get("latest_version") == "3.0.1")

    status, body = report(None)
    check("no version field: 200", status == 200)
    check("no version field: latest_version=2.10.1", body.get("latest_version") == "2.10.1")

    set_trains(None, None)
    status, body = report("3.0.0")
    check("unknown trains: 200", status == 200)
    check("unknown trains: latest_version omitted", "latest_version" not in body)


def test_hub_self_update():
    print("\n-- hub self-update: parse_hub_version --")
    check("parses double-quoted", app.parse_hub_version('HUB_VERSION = "1.14.0"\n') == "1.14.0")
    check("parses single-quoted", app.parse_hub_version("HUB_VERSION = '2.0.3'") == "2.0.3")
    check("ignores non-anchored text", app.parse_hub_version('X_HUB_VERSION = "9.9.9"') is None)
    check("none when absent", app.parse_hub_version("nothing here") is None)
    check("first match wins",
          app.parse_hub_version('HUB_VERSION = "1.0.0"\nHUB_VERSION = "2.0.0"') == "1.0.0")

    print("\n-- hub self-update: update decision --")
    check("remote ahead triggers", app.cmp_versions("1.15.0", app.HUB_VERSION) > 0)
    check("same version no update", app.cmp_versions(app.HUB_VERSION, app.HUB_VERSION) == 0)

    print("\n-- hub self-update: watcher flag gating --")
    saved_flag = app.HUB_AUTO_UPDATE
    app.hub_update_watcher_thread = None
    app.HUB_AUTO_UPDATE = False
    app.start_hub_update_watcher()
    check("disabled -> no watcher thread", app.hub_update_watcher_thread is None)
    # Enabled: stub the fetch so the loop never touches the network, then confirm it runs.
    orig_fetch = app.fetch_remote_hub_version
    app.fetch_remote_hub_version = lambda: None
    app.HUB_AUTO_UPDATE = True
    try:
        app.start_hub_update_watcher()
        check("enabled -> watcher thread alive",
              app.hub_update_watcher_thread is not None and app.hub_update_watcher_thread.is_alive())
    finally:
        app.fetch_remote_hub_version = orig_fetch
        app.HUB_AUTO_UPDATE = saved_flag

    print("\n-- hub self-update: perform_hub_update pulls a clone up to origin/main --")
    import subprocess as _sp

    def _git(cwd, *args):
        _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=cwd, capture_output=True, text=True, check=True)
    try:
        base = tempfile.mkdtemp(prefix="hub-selfupdate-")
        origin = os.path.join(base, "origin")
        os.makedirs(origin)
        _git(origin, "init", "-b", "main")
        with open(os.path.join(origin, "app.py"), "w") as f:
            f.write('HUB_VERSION = "1.0.0"\n')
        open(os.path.join(origin, "requirements.txt"), "w").close()
        _git(origin, "add", "-A")
        _git(origin, "commit", "-m", "v1")
        work = os.path.join(base, "work")
        _sp.run(["git", "clone", origin, work], capture_output=True, text=True, check=True)
        # origin advances; the hub's clone must fast-follow via reset --hard.
        with open(os.path.join(origin, "app.py"), "w") as f:
            f.write('HUB_VERSION = "2.0.0"\n')
        _git(origin, "commit", "-am", "v2")
        ok = app.perform_hub_update(work)
        with open(os.path.join(work, "app.py")) as f:
            pulled = f.read()
        check("perform_hub_update returned True", ok is True)
        check("clone advanced to origin/main (2.0.0)", app.parse_hub_version(pulled) == "2.0.0")
    except Exception as e:
        check(f"perform_hub_update dry run (unexpected error: {e})", False)


if __name__ == "__main__":
    test_version_compare()
    test_companion_train()
    test_companion_final_is_terminal()
    test_agent_train()
    test_unknown_trains()
    test_report_endpoint()
    test_hub_self_update()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
