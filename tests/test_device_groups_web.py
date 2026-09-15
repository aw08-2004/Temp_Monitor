"""Device groups over HTTP (hub 1.114.0): the two gates, scope on every write, redaction.

The silent failures this file exists to catch:

  * **A scoped operator widening a group past their scope.** Every rule and deployment aimed
    at a group inherits its reach, so a site-scoped editor who could save "every PC" would
    have handed a fleet-wide target to whoever aims at it next. Checked on create, on edit
    (both the old and the new definition) and on delete.
  * **A viewer enumerating machines through a definition.** A `machines` selector is a list of
    hostnames; a group a superuser wrote must not leak the names outside a viewer's scope, in
    the definition or in the member list.
  * **Write routes that only check `view`.** The page hides the editor, and the API must refuse
    anyway.
  * **Deleting a group a rule aims at.** 409, naming the rules.

Run from the repo root so `import app` resolves.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-device-groups-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@example.com"

import app
import permissions
import rules

PASS = 0
FAIL = 0
IFRAME = {"Sec-Fetch-Dest": "iframe"}


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


CSRF_TOKEN = "device-groups-test-token"


def client_for(email):
    """A signed-in client that sends the CSRF header, as common.js's fetch interceptor does in a
    browser. Without it every write here is a 403 from login_required -- which would make the
    scope assertions below pass or fail for the wrong reason."""
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
    permissions.create_group(
        app.DB_PATH, name="Group editors",
        capabilities=[permissions.VIEW, permissions.MANAGE_DEVICE_GROUPS],
        machines=["PC-1"], members=["editor@example.com"], actor="root@example.com")


def machines_target(*names):
    return {"include": [{"kind": "machines", "machines": list(names)}], "exclude": []}


def test_gates():
    print("\n-- view reads, manage_device_groups writes --")
    viewer = client_for("viewer@example.com")
    check("a viewer can list groups", viewer.get("/api/device-groups").status_code == 200)
    r = viewer.post("/api/device-groups", json={"name": "Nope", "target": machines_target("PC-1")})
    check("a viewer cannot create one", r.status_code == 403)
    check("the capability exists and has catalog text",
          permissions.MANAGE_DEVICE_GROUPS in permissions.CAPABILITIES)
    page = viewer.get("/device-groups", headers=IFRAME).get_data(as_text=True)
    check("a viewer gets the page without its editor",
          'id="device-groups-root"' in page and 'id="device-group-dialog"' not in page
          and 'data-can-manage="0"' in page)
    page = client_for("editor@example.com").get("/device-groups", headers=IFRAME).get_data(as_text=True)
    check("an editor gets the editor", 'id="device-group-dialog"' in page and 'data-can-manage="1"' in page)
    shell = client_for("viewer@example.com").get("/", headers={"Sec-Fetch-Dest": "document"}).get_data(as_text=True)
    check("the sidebar links the page", 'data-nav-prefix="/device-groups"' in shell)


def test_scope_on_write():
    print("\n-- a writer's scope must cover what the group reaches --")
    editor = client_for("editor@example.com")
    r = editor.post("/api/device-groups", json={"name": "Mine", "target": machines_target("PC-1")})
    check("an editor creates a group inside their scope", r.status_code == 201)
    mine = r.get_json()
    r = editor.post("/api/device-groups", json={"name": "Too wide", "target": machines_target("PC-2")})
    check("...but not one reaching outside it", r.status_code == 400 and "outside your access" in r.get_json()["error"])
    r = editor.post("/api/device-groups", json={"name": "Everything", "target": {"include": [{"kind": "all"}]}})
    check("...including 'every PC', which reaches past it", r.status_code == 400)
    r = editor.put(f"/api/device-groups/{mine['id']}", json={"name": "Mine", "target": machines_target("PC-1", "PC-2")})
    check("an edit cannot widen a group past the editor's scope", r.status_code == 400)

    root = client_for("root@example.com")
    r = root.post("/api/device-groups", json={"name": "Both", "target": machines_target("PC-1", "PC-2")})
    check("a superuser creates a fleet-wide group", r.status_code == 201)
    both = r.get_json()
    r = editor.put(f"/api/device-groups/{both['id']}", json={"name": "Both", "target": machines_target("PC-1")})
    check("a scoped editor cannot take over a group that already reaches past them", r.status_code == 400)
    r = editor.delete(f"/api/device-groups/{both['id']}")
    check("...nor delete it", r.status_code == 400)
    return mine, both


def test_redaction(both):
    print("\n-- what a scoped viewer can learn from a group --")
    viewer = client_for("viewer@example.com")
    listed = next(g for g in viewer.get("/api/device-groups").get_json()["groups"] if g["id"] == both["id"])
    body = str(listed)
    check("the definition hides machines outside the viewer's scope", "PC-2" not in body)
    check("...and says it did", listed["redacted"] is True)
    check("the count is the viewer's share", listed["count"] == 1)
    members = viewer.get(f"/api/device-groups/{both['id']}/members").get_json()
    check("the member list is narrowed too", members["machines"] == ["PC-1"])
    root_view = next(g for g in client_for("root@example.com").get("/api/device-groups").get_json()["groups"]
                     if g["id"] == both["id"])
    check("a superuser sees the whole definition", "PC-2" in str(root_view) and root_view["redacted"] is False)
    r = viewer.post("/api/device-groups/preview", json={"target": machines_target("PC-1", "PC-2")})
    check("a preview is narrowed as well", r.status_code == 200 and r.get_json()["machines"] == ["PC-1"])
    r = viewer.post("/api/device-groups/preview",
                    json={"target": {"include": [{"kind": "group", "group_id": both["id"]}]}})
    check("a preview refuses a nested group", r.status_code == 400)


def test_delete_in_use(mine):
    print("\n-- deleting a group a rule aims at --")
    editor = client_for("editor@example.com")
    original = rules.rules_using_group
    rules.rules_using_group = lambda _db, _gid: [{"id": 7, "name": "Nightly restart"}]
    try:
        r = editor.delete(f"/api/device-groups/{mine['id']}")
        check("is a 409", r.status_code == 409)
        check("...naming the rule", "Nightly restart" in r.get_json()["error"])
    finally:
        rules.rules_using_group = original
    r = editor.delete(f"/api/device-groups/{mine['id']}")
    check("an unused group in scope deletes", r.status_code == 200)
    check("...and 404s afterwards", editor.get(f"/api/device-groups/{mine['id']}/members").status_code == 404)


def test_audit():
    print("\n-- every write is audited at security level --")
    with sqlite3.connect(app.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM audit_log WHERE action LIKE 'device_group_%'").fetchall()
    actions = {r["action"] for r in rows}
    check(f"create and delete were recorded ({sorted(actions)})",
          {"device_group_created", "device_group_deleted"} <= actions)


def main():
    setup()
    test_gates()
    mine, both = test_scope_on_write()
    test_redaction(both)
    test_delete_in_use(mine)
    test_audit()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
