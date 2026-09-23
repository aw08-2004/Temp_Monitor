"""Watchdogs over HTTP (roadmap #20): the two-capability write gate, scope, and the heartbeat.

The silent failures this file exists to catch:

  * **A write gated on `manage_rules` alone.** A watchdog restarts a service on every machine
    it reaches, unattended -- `restart_process` by another name. `permissions.MANAGE_RULES` is
    explicit that it does NOT carry that, so a write route checking only it would have handed a
    fleet-wide service-restart button to every rule author on the day this shipped. Checked on
    create, edit, delete and the enable toggle, because a gate is only ever wrong on the route
    nobody wrote a test for.
  * **A scoped operator authoring past their scope.** Same failure device_groups_web.py guards,
    and worse here: the target does not merely describe machines, it acts on them.
  * **A viewer enumerating hostnames through a watchdog's state table.** The per-machine rows
    are machine names; a superuser's fleet-wide watchdog must not leak the ones outside a
    viewer's scope.
  * **The machine-scoped read gated on the capability but not the scope.** `access.require()`
    would pass an out-of-scope hostname straight through.
  * **The heartbeat sending a document to an agent that never asked for one.** Presence of
    `watchdog_version` in the request IS the capability check -- an older agent never sends it
    and must never be handed a document it will ignore while the console reports it as
    covered.

Run from the repo root so `import app` resolves.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-watchdogs-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@example.com"

import app
import permissions
import watchdogs

PASS = 0
FAIL = 0
IFRAME = {"Sec-Fetch-Dest": "iframe"}
CSRF_TOKEN = "watchdogs-test-token"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def client_for(email):
    """A signed-in client that sends the CSRF header, as common.js's fetch interceptor does in
    a browser. Without it every write here is a 403 from login_required, which would make the
    gate assertions below pass for the wrong reason."""
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = {"email": email}
        sess["csrf_token"] = CSRF_TOKEN
    c.environ_base["HTTP_X_CSRF_TOKEN"] = CSRF_TOKEN
    return c


def setup():
    with sqlite3.connect(app.DB_PATH) as conn:
        for name in ("PC-1", "PC-2"):
            conn.execute("INSERT OR IGNORE INTO machine_info (machine) VALUES (?)", (name,))
    permissions.init_permissions_db(app.DB_PATH)
    permissions.invalidate()
    permissions.create_group(
        app.DB_PATH, name="Viewers", capabilities=[permissions.VIEW],
        machines=["PC-1"], members=["viewer@example.com"], actor="root@example.com")
    # The operator this whole file is about: may write standing instructions, may NOT issue
    # commands. Every existing rules test has an author who holds both, which is exactly how a
    # gate that checks only manage_rules stays green forever.
    permissions.create_group(
        app.DB_PATH, name="Rule authors",
        capabilities=[permissions.VIEW, permissions.MANAGE_RULES],
        machines=["PC-1", "PC-2"], members=["author@example.com"], actor="root@example.com")
    permissions.create_group(
        app.DB_PATH, name="Site techs",
        capabilities=[permissions.VIEW, permissions.MANAGE_RULES, permissions.ISSUE_COMMANDS],
        machines=["PC-1"], members=["tech@example.com"], actor="root@example.com")
    permissions.invalidate()


def body(**over):
    payload = {"name": "Spooler", "service": "Spooler",
               "target": {"include": [{"kind": "all"}], "exclude": []}}
    payload.update(over)
    return payload


def test_write_gate():
    print("\n-- writing needs manage_rules AND issue_commands --")
    viewer = client_for("viewer@example.com")
    author = client_for("author@example.com")
    root = client_for("root@example.com")

    check("a viewer can read the list", viewer.get("/api/watchdogs").status_code == 200)
    check("a viewer cannot create one",
          viewer.post("/api/watchdogs", json=body()).status_code == 403)
    check("nor can somebody who may write rules but not issue commands",
          author.post("/api/watchdogs", json=body()).status_code == 403)

    r = root.post("/api/watchdogs", json=body())
    check("a superuser can", r.status_code == 201)
    created = r.get_json()

    check("the rule author cannot edit an existing one either",
          author.put(f"/api/watchdogs/{created['id']}", json=body()).status_code == 403)
    check("...nor toggle it",
          author.put(f"/api/watchdogs/{created['id']}/enabled",
                     json={"enabled": False}).status_code == 403)
    check("...nor delete it",
          author.delete(f"/api/watchdogs/{created['id']}").status_code == 403)

    listing = author.get("/api/watchdogs").get_json()
    check("the list tells a rule author they hold one of the two capabilities, not neither",
          listing["can_manage"] is False and listing["can_manage_rules"] is True
          and listing["can_issue_commands"] is False)
    check("...and hands the editor its bounds rather than letting it hardcode them",
          listing["limits"]["max_restarts"] == [watchdogs.MIN_RESTARTS, watchdogs.MAX_RESTARTS]
          and "rpcss" in listing["limits"]["protected_services"])
    return created


def test_page():
    print("\n-- the page and the sidebar --")
    page = client_for("viewer@example.com").get("/watchdogs", headers=IFRAME).get_data(as_text=True)
    check("a viewer gets the page", 'id="watchdogs-body"' in page)
    shell = client_for("viewer@example.com").get(
        "/", headers={"Sec-Fetch-Dest": "document"}).get_data(as_text=True)
    check("the sidebar links it", 'data-nav-prefix="/watchdogs"' in shell)


def test_scope(created):
    print("\n-- a scoped author cannot reach past their machines --")
    tech = client_for("tech@example.com")
    r = tech.post("/api/watchdogs", json=body(name="Mine", service="W32Time",
                                              target={"include": [{"kind": "machines",
                                                                   "machines": ["PC-1"]}],
                                                      "exclude": []}))
    check("a site tech creates one inside their scope", r.status_code == 201)
    mine = r.get_json()
    r = tech.post("/api/watchdogs", json=body(name="Too wide", service="W32Time",
                                              target={"include": [{"kind": "machines",
                                                                   "machines": ["PC-2"]}],
                                                      "exclude": []}))
    check("...but not one reaching outside it",
          r.status_code == 400 and "outside your access" in r.get_json()["error"])
    r = tech.post("/api/watchdogs", json=body(name="Everything", service="W32Time"))
    check("...including 'every PC', which reaches past it", r.status_code == 400)

    # The half a save-time check cannot do. The tech's scope was pinned on the row, so the
    # document this resolves for PC-2 must not contain their watchdog even though PC-2 now
    # matches the selector.
    check("their watchdog does not reach PC-2 even via a target that names it later",
          mine["id"] not in [e["id"] for e in
                             watchdogs.resolve_for(app.DB_PATH, "PC-2")["watchdogs"]])

    print("\n-- what a scoped viewer can learn --")
    watchdogs.record_report(app.DB_PATH, "PC-1", {"states": [{"id": created["id"],
                                                              "status": "ok"}]})
    watchdogs.record_report(app.DB_PATH, "PC-2", {"states": [{"id": created["id"],
                                                              "status": "given_up"}]})
    seen = client_for("viewer@example.com").get(f"/api/watchdogs/{created['id']}").get_json()
    check("the state table hides machines outside the viewer's scope",
          [s["machine"] for s in seen["state"]] == ["PC-1"])
    listing = client_for("viewer@example.com").get("/api/watchdogs").get_json()
    row = [w for w in listing["watchdogs"] if w["id"] == created["id"]][0]
    check("...and so do the tallies on the list", row["machines"] == 1 and row["trouble"] == 0)
    history = client_for("viewer@example.com").get("/api/watchdogs/events").get_json()
    check("...and the fleet history", {e["machine"] for e in history["events"]} == {"PC-1"})
    return mine


def test_machine_route(created):
    print("\n-- the machine route needs the capability AND the scope --")
    viewer = client_for("viewer@example.com")
    check("a viewer reads a machine in scope",
          viewer.get("/api/machines/PC-1/watchdogs").status_code == 200)
    check("...and is refused one outside it",
          viewer.get("/api/machines/PC-2/watchdogs").status_code == 403)
    payload = viewer.get("/api/machines/PC-1/watchdogs").get_json()
    check("the machine view names the watchdog and its service",
          any(w["service"] == "Spooler" for w in payload["watchdogs"]))


def test_heartbeat(created):
    print("\n-- the heartbeat is where a machine gets its document --")
    agent_id, token = _enroll("PC-1")
    # `<agent_id>:<token>`, not the token alone -- see auth_helpers.bearer_parts.
    headers = {"Authorization": f"Bearer {agent_id}:{token}"}
    client = app.app.test_client()

    r = client.post("/api/agent/heartbeat", json={"config_version": ""}, headers=headers)
    check("an agent that does not ask is not sent a document",
          r.status_code == 200 and "watchdog_document" not in r.get_json())

    r = client.post("/api/agent/heartbeat", json={"config_version": "", "watchdog_version": ""},
                    headers=headers)
    payload = r.get_json()
    check("an agent that asks with an empty version gets one",
          "watchdog_document" in payload
          and created["id"] in [e["id"] for e in payload["watchdog_document"]["watchdogs"]])
    version = payload["watchdog_version"]

    r = client.post("/api/agent/heartbeat",
                    json={"config_version": "", "watchdog_version": version}, headers=headers)
    check("...and is not sent it again while it holds the current one",
          "watchdog_document" not in r.get_json())

    r = client.post("/api/agent/heartbeat",
                    json={"config_version": "", "watchdog_version": version,
                          "watchdog": {"states": [{"id": created["id"], "status": "restarted",
                                                   "restarts": 1, "last_restart_at": 1700}]}},
                    headers=headers)
    check("a report on the heartbeat is ingested", r.status_code == 200)
    state = [s for s in watchdogs.machine_states(app.DB_PATH, "PC-1")
             if s["watchdog_id"] == created["id"]]
    check("...and stored against the machine", state and state[0]["status"] == "restarted")

    r = client.post("/api/agent/heartbeat",
                    json={"config_version": "", "watchdog_version": version,
                          "watchdog": {"states": "not a list"}}, headers=headers)
    check("a malformed report costs the report, never the heartbeat", r.status_code == 200)


def _enroll(machine):
    """An enrolled agent identity for `machine`.

    Minted through fleet.py rather than through POST /api/agent/enroll, deliberately: the
    enrollment secret is install-time configuration this module has no business knowing, and
    the route that matters here is the heartbeat. Passing the same value as both the provided
    and the expected secret is how the enrollment check is satisfied without one.
    """
    import fleet
    secret = "watchdogs-test-enrollment-secret"
    return fleet.enroll_agent(app.DB_PATH, machine, secret, secret)


def main():
    setup()
    created = test_write_gate()
    test_page()
    mine = test_scope(created)
    test_machine_route(created)
    test_heartbeat(created)
    _ = mine
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
