"""HTTP-layer test for directory_web.py -- AD status, the OU list, and "sync now".

Wires the blueprint onto a minimal Flask app with a fake login_required, the same way
test_permissions_web.py does; app.py itself can't be imported here without a full OAuth
boot.

What is worth testing here is NOT the sync (test_directory.py covers that against literal
entries). It is the gating: "sync now" binds to a domain controller with a service
account, and the OU list describes the shape of somebody's internal network. Both need to
be refused to an ordinary operator, and a gate that is in the decorator list but doesn't
fire is the failure worth spending a test on.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import alerts
import directory
import fleet
import permissions
import settings
from directory_web import create_directory_blueprint
from permissions_web import create_access
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
    app.register_blueprint(create_directory_blueprint(db_path, fake_login_required, access))

    @app.before_request
    def _seed_session():
        from flask import session
        session["user"] = {"email": CURRENT_USER}

    return app


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        with directory.get_conn(db_path) as conn:
            conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, "
                         "asset_tag TEXT, serial_number TEXT, model TEXT, updated_at TEXT)")
        fleet.init_fleet_db(db_path)
        alerts.init_alerts_db(db_path)
        settings.init_settings_db(db_path)
        permissions.init_permissions_db(db_path)
        directory.init_directory_db(db_path)
        settings.invalidate()
        permissions.invalidate()

        app = build_app(db_path)
        c = app.test_client()

        print("\n== Status, as break-glass ==")
        r = c.get("/api/directory/status")
        check("200", r.status_code == 200)
        doc = r.get_json()
        check("reports the feature off by default", doc["enabled"] is False)
        check("reports whether the LDAP library is installed",
              isinstance(doc["library_installed"], bool))
        check("carries an install hint for when it is not", "pip install" in doc["library_hint"])
        check("never run yet", doc["last_success"] is None)
        # The credential must be reportable as set/unset without being readable.
        check("says whether the bind password is set",
              isinstance(doc["bind_password_set"], bool))
        check("names the env var to put it in",
              doc["bind_password_env"] == directory.BIND_PASSWORD_ENV)
        # Set a real, recognisable secret and prove it does not appear. Asserting on the
        # actual value beats scanning for the word "password", which the field NAMES
        # legitimately contain.
        os.environ[directory.BIND_PASSWORD_ENV] = "s3cr3t-bind-pw-do-not-leak"
        try:
            r = c.get("/api/directory/status")
            doc = r.get_json()
            check("the password is reported as SET", doc["bind_password_set"] is True)
            check("...but its value never appears in the response",
                  "s3cr3t-bind-pw-do-not-leak" not in r.get_data(as_text=True))
        finally:
            del os.environ[directory.BIND_PASSWORD_ENV]

        print("\n== The OU list ==")
        with directory.get_conn(db_path) as conn:
            conn.execute("INSERT INTO machine_info(machine, ad_ou) VALUES (?, ?)",
                         ("PC-1", "OU=Clinical,DC=corp,DC=local"))
            conn.execute("INSERT INTO machine_info(machine, ad_ou) VALUES (?, ?)",
                         ("PC-2", "OU=Finance,DC=corp,DC=local"))
            conn.execute("INSERT INTO machine_info(machine) VALUES (?)", ("PC-3",))
        r = c.get("/api/directory/ous")
        check("200", r.status_code == 200)
        check("lists the OUs the fleet is in",
              r.get_json()["ous"] == ["OU=Clinical,DC=corp,DC=local",
                                      "OU=Finance,DC=corp,DC=local"])
        check("a machine with no AD record contributes no OU",
              len(r.get_json()["ous"]) == 2)

        print("\n== Sync now is refused when the feature is off ==")
        r = c.post("/api/directory/sync", json={})
        check("400, not a silent no-op", r.status_code == 400)
        check("...and says to enable it first", "turned off" in r.get_json()["error"])

        print("\n== Sync now reports a configuration problem as 400, not 500 ==")
        # Every one of these is something the operator can act on, and the message is
        # written for them to read -- a 500 would send them to the service log instead.
        settings.set_many(db_path, {"directory.enabled": True}, updated_by="root@x.com")
        settings.invalidate()
        r = c.post("/api/directory/sync", json={})
        check("400", r.status_code == 400)
        check("and names the missing setting or library",
              any(hint in r.get_json()["error"]
                  for hint in ("directory.server", "ldap3")))

        print("\n== Gating ==")
        # A group with view + issue_commands but no admin capability: the ordinary
        # operator this must all be closed to.
        permissions.create_group(
            db_path, name="Operators",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            scope_mode=permissions.SCOPE_ALL, members=["ann@x.com"], actor="root@x.com")
        # Settings admin: configures the sync, so may read status and run one.
        permissions.create_group(
            db_path, name="Settings Admins", capabilities=[permissions.MANAGE_SETTINGS],
            scope_mode=permissions.SCOPE_ALL, members=["sam@x.com"], actor="root@x.com")
        # Group admin: scopes groups by OU, so needs the OU list -- but must NOT be able
        # to make the hub bind to a domain controller.
        permissions.create_group(
            db_path, name="Group Admins",
            capabilities=[permissions.MANAGE_PERMISSION_GROUPS],
            scope_mode=permissions.SCOPE_ALL, members=["gil@x.com"], actor="root@x.com")

        CURRENT_USER = "ann@x.com"
        check("an ordinary operator cannot read AD status",
              c.get("/api/directory/status").status_code == 403)
        check("...nor the OU list", c.get("/api/directory/ous").status_code == 403)
        check("...nor trigger a sync",
              c.post("/api/directory/sync", json={}).status_code == 403)

        CURRENT_USER = "gil@x.com"
        check("a group admin CAN read the OU list (they scope groups by OU)",
              c.get("/api/directory/ous").status_code == 200)
        check("...and the status", c.get("/api/directory/status").status_code == 200)
        check("but may NOT make the hub bind to a domain controller",
              c.post("/api/directory/sync", json={}).status_code == 403)

        CURRENT_USER = "sam@x.com"
        check("a settings admin may read status",
              c.get("/api/directory/status").status_code == 200)
        check("...and may run a sync (it is a configuration action)",
              c.post("/api/directory/sync", json={}).status_code == 400)  # 400 = ran, misconfigured
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
