"""HTTP-layer test for permissions_web.py: the Access gates and the admin API.

Wires the blueprints onto a minimal Flask app with a fake login_required, exactly like
test_fleet_web.py / test_settings_web.py -- app.py itself can't be imported here without
a Google OAuth config.

The fleet and settings blueprints are mounted too, because the point of this module is
not "does CRUD work" (test_permissions.py covers the model) but "does a scoped operator
actually get refused". A gate that exists in the decorator list but doesn't fire is the
failure mode worth spending a test on.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet
import permissions
import settings
import users
from fleet_web import create_fleet_blueprint
from settings_web import create_settings_blueprint
from permissions_web import create_access, create_permissions_blueprint
from flask import Flask

PASS = 0
FAIL = 0

SUPERUSERS = {"root@x.com"}
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


def build_app(db_path):
    app = Flask(__name__, template_folder=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))
    app.secret_key = "test"
    access = create_access(db_path, SUPERUSERS)
    app.register_blueprint(create_permissions_blueprint(db_path, fake_login_required, access))
    app.register_blueprint(create_fleet_blueprint(db_path, "enroll-secret",
                                                  fake_login_required, access))
    app.register_blueprint(create_settings_blueprint(db_path, fake_login_required, access))

    @app.before_request
    def _seed_session():
        from flask import session
        session["user"] = {"email": CURRENT_USER}

    return app, access


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        permissions.init_permissions_db(db_path)
        fleet.init_fleet_db(db_path)
        settings.init_settings_db(db_path)
        users.init_users_db(db_path)
        settings.invalidate()
        permissions.invalidate()

        app, access = build_app(db_path)
        c = app.test_client()

        print("\n== /api/permissions/me as break-glass ==")
        r = c.get("/api/permissions/me")
        check("200", r.status_code == 200)
        me = r.get_json()
        check("reports superuser", me["superuser"] is True)
        check("reports every capability",
              set(me["capabilities"]) == set(permissions.CAPABILITIES))
        check("null machines means unrestricted", me["machines"] is None)

        print("\n== Creating groups through the API ==")
        r = c.post("/api/permissions/groups", json={
            "name": "Hospital IT",
            "capabilities": [permissions.VIEW, permissions.ISSUE_COMMANDS],
            "machines": ["PC-1", "PC-2"],
            "members": ["ann@x.com"],
        })
        check("create 201", r.status_code == 201)
        hospital = r.get_json()["id"]
        check("response carries the stored group",
              r.get_json()["machines"] == ["PC-1", "PC-2"])

        r = c.post("/api/permissions/groups", json={"name": "Hospital IT"})
        check("duplicate name 400", r.status_code == 400)
        r = c.post("/api/permissions/groups", json={"name": "Bad", "capabilities": ["nope"]})
        check("unknown capability 400", r.status_code == 400)

        c.post("/api/permissions/groups", json={
            "name": "HR IT",
            "capabilities": [permissions.VIEW, permissions.MANAGE_SETTINGS],
            "machines": ["HR-9"],
            "members": ["bob@x.com"],
        })
        check("both groups listed", len(c.get("/api/permissions/groups").get_json()) == 2)

        print("\n== CSRF: JSON content type is required ==")
        # A cross-site HTML form can't set Content-Type: application/json, and
        # get_json(silent=True) yields None for anything else -- so the body reads as
        # empty and the create is rejected on a missing name. This is what stops a form
        # POST from granting capabilities to a signed-in admin's session.
        before = len(c.get("/api/permissions/groups").get_json())
        r = c.post("/api/permissions/groups", data={"name": "Injected"})
        check("form-encoded create rejected", r.status_code == 400)
        check("form-encoded create changed nothing",
              len(c.get("/api/permissions/groups").get_json()) == before)

        print("\n== A scoped operator: capabilities ==")
        CURRENT_USER = "ann@x.com"
        me = c.get("/api/permissions/me").get_json()
        check("not a superuser", me["superuser"] is False)
        check("only the group's capabilities",
              set(me["capabilities"]) == {permissions.VIEW, permissions.ISSUE_COMMANDS})
        check("only the group's machines", me["machines"] == ["PC-1", "PC-2"])
        check("reports the group she is in",
              [g["name"] for g in me["groups"]] == ["Hospital IT"])

        print("\n== A scoped operator is refused the admin API ==")
        check("list groups 403", c.get("/api/permissions/groups").status_code == 403)
        check("create group 403",
              c.post("/api/permissions/groups", json={"name": "Self Promotion"}).status_code == 403)
        check("read one group 403",
              c.get(f"/api/permissions/groups/{hospital}").status_code == 403)
        check("update group 403",
              c.put(f"/api/permissions/groups/{hospital}",
                    json={"capabilities": list(permissions.CAPABILITIES)}).status_code == 403)
        check("delete group 403",
              c.delete(f"/api/permissions/groups/{hospital}").status_code == 403)
        check("capability vocabulary 403",
              c.get("/api/permissions/capabilities").status_code == 403)
        check("the refused update really didn't apply",
              permissions.get_group(db_path, hospital)["capabilities"]
              == [permissions.VIEW, permissions.ISSUE_COMMANDS])
        check("/api/permissions/me is still allowed -- it reveals only her own access",
              c.get("/api/permissions/me").status_code == 200)

        print("\n== Settings are gated on manage_settings ==")
        check("GET settings 403 without the capability",
              c.get("/api/settings").status_code == 403)
        check("POST settings 403 without the capability",
              c.post("/api/settings",
                     json={"updates": {"data.retention_days": 1}}).status_code == 403)
        check("the refused write didn't land",
              settings.get(db_path, "data.retention_days") == 30)
        CURRENT_USER = "bob@x.com"      # HR IT holds manage_settings
        check("GET settings 200 with the capability",
              c.get("/api/settings").status_code == 200)

        print("\n== Issuing a command: capability AND scope ==")
        CURRENT_USER = "bob@x.com"      # view + manage_settings, but NOT issue_commands
        r = c.post("/api/fleet/commands", json={"machine": "HR-9", "type": "restart",
                                                "params": {}})
        check("no issue_commands capability -> 403", r.status_code == 403)
        check("nothing queued", fleet.list_commands(db_path) == [])

        CURRENT_USER = "ann@x.com"      # issue_commands, but only over PC-1/PC-2
        r = c.post("/api/fleet/commands", json={"machine": "HR-9", "type": "restart",
                                                "params": {}})
        check("right capability, wrong machine -> 403", r.status_code == 403)
        check("still nothing queued", fleet.list_commands(db_path) == [])

        r = c.post("/api/fleet/commands", json={"machine": "PC-1", "type": "restart",
                                                "params": {}})
        check("in-scope command accepted", r.status_code == 201)
        pc1_command = r.get_json()["command_id"]

        CURRENT_USER = "root@x.com"
        r = c.post("/api/fleet/commands", json={"machine": "HR-9", "type": "restart",
                                                "params": {}})
        check("break-glass can command any machine", r.status_code == 201)
        hr9_command = r.get_json()["command_id"]

        print("\n== Reads are scoped, not just writes ==")
        CURRENT_USER = "ann@x.com"
        listed = c.get("/api/fleet/commands").get_json()
        check("command list hides out-of-scope machines",
              {row["machine"] for row in listed} == {"PC-1"})
        check("her own command is visible",
              c.get(f"/api/fleet/commands/{pc1_command}").status_code == 200)
        # 404 rather than 403: distinguishing them would confirm the id exists.
        check("an out-of-scope command reads as 404, not 403",
              c.get(f"/api/fleet/commands/{hr9_command}").status_code == 404)
        check("its streamed output is 404 too",
              c.get(f"/api/fleet/commands/{hr9_command}/output").status_code == 404)
        check("asking for another machine's command list is refused",
              c.get("/api/fleet/commands?machine=HR-9").status_code == 403)

        print("\n== Agent status is filtered ==")
        fleet.enroll_agent(db_path, "PC-1", "enroll-secret", "enroll-secret")
        fleet.enroll_agent(db_path, "HR-9", "enroll-secret", "enroll-secret")
        CURRENT_USER = "ann@x.com"
        status = c.get("/api/fleet/status").get_json()
        check("only in-scope agents are listed",
              {row["machine"] for row in status} == {"PC-1"})
        CURRENT_USER = "root@x.com"
        status = c.get("/api/fleet/status").get_json()
        check("break-glass sees the whole fleet",
              {row["machine"] for row in status} == {"PC-1", "HR-9"})

        print("\n== login_allowed ==")
        with app.test_request_context():
            check("a superuser may sign in", access.login_allowed("root@x.com"))
            check("a group member may sign in", access.login_allowed("ann@x.com"))
            check("case is normalised", access.login_allowed("Ann@X.COM"))
            check("someone in no group may NOT sign in",
                  access.login_allowed("stranger@x.com") is False)

        print("\n== A user whose last group is deleted loses access immediately ==")
        CURRENT_USER = "root@x.com"
        groups = {g["name"]: g["id"] for g in c.get("/api/permissions/groups").get_json()}
        check("delete 200",
              c.delete(f"/api/permissions/groups/{groups['Hospital IT']}").status_code == 200)
        CURRENT_USER = "ann@x.com"
        check("she now holds nothing",
              c.get("/api/permissions/me").get_json()["capabilities"] == [])
        check("and can no longer issue commands",
              c.post("/api/fleet/commands",
                     json={"machine": "PC-1", "type": "restart"}).status_code == 403)
        with app.test_request_context():
            check("nor sign in again", access.login_allowed("ann@x.com") is False)

        print("\n== Capability vocabulary is served to an admin ==")
        CURRENT_USER = "root@x.com"
        doc = c.get("/api/permissions/capabilities").get_json()
        check("every capability is described",
              [item["name"] for item in doc["capabilities"]] == list(permissions.CAPABILITIES))
        check("each carries a label and a description",
              all(item["label"] and item["description"] for item in doc["capabilities"]))
        check("scope modes are served too",
              doc["scope_modes"] == list(permissions.SCOPE_MODES))

        print("\n== Member picker directory search ==")
        # Seed a couple of registered users to search against.
        users.create_user(db_path, "ann@x.com", full_name="Ann Adams", username="aadams",
                           phone="555-0100", notes="secret note", actor="root@x.com")
        users.create_user(db_path, "zoe@x.com", full_name="Zoe Zhang", username="zzhang",
                           actor="root@x.com")
        # A group holding manage_permission_groups but NOT manage_users -- to prove the
        # picker is gated on the former, so an admin who can't edit profiles can still pick.
        c.post("/api/permissions/groups", json={
            "name": "Group Admins",
            "capabilities": [permissions.MANAGE_PERMISSION_GROUPS],
            "machines": [], "members": ["carol@x.com"],
        })

        CURRENT_USER = "root@x.com"
        r = c.get("/api/permissions/directory?q=ann")
        check("directory search 200 for an admin", r.status_code == 200)
        found = r.get_json()["users"]
        check("search matches by name", [u["email"] for u in found] == ["ann@x.com"])
        check("picker returns only email/name/username",
              set(found[0].keys()) == {"email", "full_name", "username"})
        check("picker does NOT leak profile fields like phone/notes",
              "phone" not in found[0] and "notes" not in found[0])
        check("empty query returns the whole directory",
              {u["email"] for u in c.get("/api/permissions/directory").get_json()["users"]}
              == {"ann@x.com", "zoe@x.com"})

        CURRENT_USER = "carol@x.com"    # manage_permission_groups, but not manage_users
        check("a group-admin without manage_users can still use the picker",
              c.get("/api/permissions/directory?q=zoe").status_code == 200)

        CURRENT_USER = "bob@x.com"      # view + manage_settings, but not manage_permission_groups
        check("someone without manage_permission_groups is refused the picker",
              c.get("/api/permissions/directory").status_code == 403)
        CURRENT_USER = "root@x.com"     # the 404 checks below need an admin

        print("\n== 404s ==")
        check("unknown group GET 404",
              c.get("/api/permissions/groups/nope").status_code == 404)
        check("unknown group PUT 404",
              c.put("/api/permissions/groups/nope", json={"name": "x"}).status_code == 404)
        check("unknown group DELETE 404",
              c.delete("/api/permissions/groups/nope").status_code == 404)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
