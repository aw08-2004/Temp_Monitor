"""HTTP-layer test for users_web.py: the Users admin API and its MANAGE_USERS gate.

Wires the blueprints onto a minimal Flask app with a fake login_required, exactly like
test_permissions_web.py -- app.py itself can't be imported here without a Google OAuth
config.

The point of this module is not "does CRUD work" (test_users.py covers the model) but
"does an operator lacking manage_users actually get refused", and that the gate is
independent of manage_permission_groups.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import users as users_model
from permissions_web import create_access
from users_web import create_users_blueprint
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
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "templates"))
    app.secret_key = "test"
    access = create_access(db_path, SUPERUSERS)
    app.register_blueprint(create_users_blueprint(db_path, fake_login_required, access))

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
        users_model.init_users_db(db_path)
        permissions.init_permissions_db(db_path)
        fleet.init_fleet_db(db_path)
        permissions.invalidate()

        app, access = build_app(db_path)
        c = app.test_client()

        print("\n== Create through the API (as break-glass) ==")
        r = c.post("/api/users", json={
            "email": "Ann@X.com", "full_name": "Ann Adams", "username": "aadams",
        })
        check("create 201", r.status_code == 201)
        check("email normalised in the response", r.get_json()["email"] == "ann@x.com")

        r = c.post("/api/users", json={"email": "not-an-email"})
        check("bad email 400", r.status_code == 400)
        r = c.post("/api/users", json={"email": "ann@x.com"})
        check("duplicate email 400", r.status_code == 400)

        print("\n== Read / update / delete ==")
        r = c.get("/api/users/ann@x.com")
        check("get 200", r.status_code == 200 and r.get_json()["username"] == "aadams")
        check("get unknown 404", c.get("/api/users/ghost@x.com").status_code == 404)

        r = c.put("/api/users/ann@x.com", json={
            "email": "ann@x.com", "full_name": "Ann A. Adams", "title": "Lead",
        })
        check("update 200", r.status_code == 200)
        check("update applied", r.get_json()["full_name"] == "Ann A. Adams")
        check("update whole-record clears an omitted field",
              r.get_json()["username"] is None)
        check("update unknown 404",
              c.put("/api/users/ghost@x.com", json={"full_name": "x"}).status_code == 404)

        print("\n== Search passthrough ==")
        c.post("/api/users", json={"email": "bob@x.com", "full_name": "Bob Barr"})
        r = c.get("/api/users?q=barr")
        check("search filters", [u["email"] for u in r.get_json()] == ["bob@x.com"])

        print("\n== CSRF: JSON content type is required ==")
        before = len(c.get("/api/users").get_json())
        r = c.post("/api/users", data={"email": "injected@x.com"})
        check("form-encoded create rejected", r.status_code == 400)
        check("form-encoded create changed nothing",
              len(c.get("/api/users").get_json()) == before)

        print("\n== The Users API is gated on manage_users, not group admin ==")
        # A group that grants everything EXCEPT manage_users. Its member must still be
        # refused the whole Users API -- that is the point of a separate capability.
        caps_without_users = [c_ for c_ in permissions.CAPABILITIES
                              if c_ != permissions.MANAGE_USERS]
        permissions.create_group(db_path, name="Almost Everything",
                                 capabilities=caps_without_users,
                                 scope_mode=permissions.SCOPE_ALL,
                                 members=["mallory@x.com"], actor="root@x.com")
        CURRENT_USER = "mallory@x.com"
        check("list users 403", c.get("/api/users").status_code == 403)
        check("create user 403",
              c.post("/api/users", json={"email": "x@y.com"}).status_code == 403)
        check("read user 403", c.get("/api/users/ann@x.com").status_code == 403)
        check("update user 403",
              c.put("/api/users/ann@x.com", json={"full_name": "z"}).status_code == 403)
        check("delete user 403", c.delete("/api/users/ann@x.com").status_code == 403)
        check("the refused calls changed nothing",
              users_model.get_user(db_path, "ann@x.com")["full_name"] == "Ann A. Adams")

        print("\n== manage_users alone is enough ==")
        permissions.create_group(db_path, name="User Admins",
                                 capabilities=[permissions.MANAGE_USERS],
                                 scope_mode=permissions.SCOPE_ALL,
                                 members=["uadmin@x.com"], actor="root@x.com")
        CURRENT_USER = "uadmin@x.com"
        check("list users 200 with just manage_users",
              c.get("/api/users").status_code == 200)
        r = c.delete("/api/users/bob@x.com")
        check("delete 200 with just manage_users", r.status_code == 200)
        check("delete applied", users_model.get_user(db_path, "bob@x.com") is None)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
