"""HTTP-layer test for auth_web.py -- editing the sign-in providers from the console.

Two things are worth a test here, and they are the two that would be expensive to get
wrong:

  * **the gate.** This endpoint is break-glass-only, deliberately NOT `manage_settings`:
    whoever configures the identity provider decides who this hub believes you are, so a
    group holding every capability -- including manage_settings and
    manage_permission_groups -- must still be refused.
  * **the rollback.** If the new configuration cannot be registered, the hub must be left
    signing people in exactly as it was: in .env, in os.environ, and in the live clients.
    A failure here costs the ability to sign in, and the console that would fix it is the
    one that just broke.
"""
import functools
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import authconfig
import envfile
import fleet
import permissions
from auth_web import create_auth_blueprint
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


# Stands in for app.configure_oauth. `fail_on` makes it refuse a configuration the way
# Authlib would refuse an unreachable discovery URL -- at registration time, after .env has
# already been written.
class FakeReconfigure:
    def __init__(self):
        self.applied = []
        self.fail_on = None

    def __call__(self, config):
        if self.fail_on and config.get("oidc_issuer") == self.fail_on:
            raise RuntimeError("could not fetch the discovery document")
        self.applied.append(dict(config))
        return config


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    env_fd, env_path = tempfile.mkstemp(suffix=".env")
    os.close(env_fd)
    saved_environ = {k: os.environ.get(k) for k in authconfig.FIELD_NAMES}
    try:
        fleet.init_fleet_db(db_path)
        permissions.init_permissions_db(db_path)
        permissions.invalidate()

        # A known starting configuration, in both .env and the live process.
        start = authconfig.load({
            "GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gsecret",
            "OIDC_CLIENT_ID": "oid", "OIDC_CLIENT_SECRET": "osecret",
            "OIDC_ISSUER": "https://old.example.com",
        })
        authconfig.save(env_path, start)

        reconfigure = FakeReconfigure()
        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, SUPERUSERS)
        app.register_blueprint(create_auth_blueprint(
            db_path, fake_login_required, access, env_path, reconfigure))

        @app.before_request
        def _seed_session():
            from flask import session
            session["user"] = {"email": CURRENT_USER}

        c = app.test_client()

        print("\n== A break-glass admin can read the configuration ==")
        r = c.get("/api/auth/providers")
        check("200", r.status_code == 200)
        doc = r.get_json()
        check("Google is reported enabled", doc["google_enabled"] is True)
        check("OIDC is reported enabled", doc["oidc_enabled"] is True)
        check("the issuer is shown", doc["oidc_issuer"] == "https://old.example.com")
        check("the break-glass list is named", doc["superusers"] == ["root@x.com"])

        print("\n== ...but never the secrets ==")
        body = r.get_data(as_text=True)
        check("no Google secret in the response", "gsecret" not in body)
        check("no OIDC secret in the response", "osecret" not in body)
        check("only a flag saying one is set", doc["google_client_secret_set"] is True)

        print("\n== The gate is break-glass, NOT manage_settings ==")
        # The distinction this whole module exists for. A group holding literally every
        # capability must still be refused: manage_settings is meant to be delegable, and
        # pointing the hub at an attacker-controlled issuer must not ride along with it.
        permissions.create_group(
            db_path, name="Everything But Break-Glass",
            capabilities=list(permissions.CAPABILITIES),
            scope_mode=permissions.SCOPE_ALL, members=["almost@x.com"],
            actor="root@x.com")
        CURRENT_USER = "almost@x.com"
        check("a holder of EVERY capability is refused the read",
              c.get("/api/auth/providers").status_code == 403)
        check("...and the write",
              c.put("/api/auth/providers", json={"oidc_issuer": "https://evil.example.com"}
                    ).status_code == 403)
        check("the refusal explains why it is not delegable",
              "ALLOWED_EMAILS" in c.get("/api/auth/providers").get_json()["error"])
        check("and nothing was applied", not any(
            cfg.get("oidc_issuer") == "https://evil.example.com"
            for cfg in reconfigure.applied))
        check("...nor written to .env",
              envfile.read_all(env_path)["OIDC_ISSUER"] == "https://old.example.com")
        CURRENT_USER = "root@x.com"

        print("\n== A valid change is written, applied and audited ==")
        r = c.put("/api/auth/providers", json={"oidc_issuer": "https://new.example.com",
                                               "oidc_display_name": "Microsoft"})
        check("200", r.status_code == 200)
        check("the live clients were re-registered",
              reconfigure.applied[-1]["oidc_issuer"] == "https://new.example.com")
        check("the discovery URL was re-derived, not left on the old tenant",
              reconfigure.applied[-1]["oidc_metadata_url"]
              == "https://new.example.com/.well-known/openid-configuration")
        check(".env was updated",
              envfile.read_all(env_path)["OIDC_ISSUER"] == "https://new.example.com")
        check("os.environ was updated too (no restart needed)",
              os.environ.get("OIDC_ISSUER") == "https://new.example.com")
        check("the response names what changed",
              "oidc_issuer" in r.get_json()["changed"])

        with fleet.get_conn(db_path) as conn:
            rows = [dict(x) for x in conn.execute(
                "SELECT action, actor, detail_json FROM audit_log "
                "WHERE action = 'auth.providers.update'")]
        check("the change is audited", len(rows) == 1)
        check("...attributed to the operator", rows[0]["actor"] == "root@x.com")
        check("...naming the changed fields",
              "oidc_issuer" in rows[0]["detail_json"])
        # A secret in an audit row is a credential in the database, and from there in
        # every hub-database backup.
        check("...and containing NO secret value",
              "osecret" not in rows[0]["detail_json"]
              and "gsecret" not in rows[0]["detail_json"])

        print("\n== A secret sent back as the placeholder is not saved verbatim ==")
        r = c.put("/api/auth/providers",
                  json={"oidc_client_secret": authconfig.UNCHANGED,
                        "oidc_display_name": "Entra"})
        check("200", r.status_code == 200)
        check("the stored secret is untouched",
              envfile.read_all(env_path)["OIDC_CLIENT_SECRET"] == "osecret")
        check("the placeholder never reached .env",
              authconfig.UNCHANGED not in envfile.read_all(env_path).values())
        check("the field the admin did change was applied",
              envfile.read_all(env_path)["OIDC_DISPLAY_NAME"] == "Entra")

        print("\n== A config leaving no provider is refused ==")
        r = c.put("/api/auth/providers", json={
            "google_client_id": "", "google_client_secret": "",
            "oidc_client_id": "", "oidc_client_secret": "",
            "oidc_issuer": "", "oidc_metadata_url": "",
        })
        check("400", r.status_code == 400)
        check("...and says it cannot be undone from this page",
              "cannot be fixed from this page" in r.get_json()["error"])
        check("Google is still configured", os.environ.get("GOOGLE_CLIENT_ID") == "gid")
        check("...and OIDC still is too", os.environ.get("OIDC_CLIENT_ID") == "oid")

        print("\n== An http:// issuer is refused ==")
        r = c.put("/api/auth/providers", json={"oidc_issuer": "http://plain.example.com"})
        check("400", r.status_code == 400)
        check("nothing was written",
              envfile.read_all(env_path)["OIDC_ISSUER"] == "https://new.example.com")

        print("\n== A registration failure ROLLS BACK completely ==")
        # The path that matters most: .env has already been written when Authlib rejects
        # the new issuer. Everything must go back, or the hub is left unable to sign
        # anybody in -- including whoever would fix it.
        before_env = dict(envfile.read_all(env_path))
        applied_before = len(reconfigure.applied)
        reconfigure.fail_on = "https://broken.example.com"
        r = c.put("/api/auth/providers", json={"oidc_issuer": "https://broken.example.com"})
        check("400", r.status_code == 400)
        check("the error explains what to check",
              "discovery document" in r.get_json()["error"])
        check(".env is byte-for-byte back to what it was",
              envfile.read_all(env_path) == before_env)
        check("os.environ is restored",
              os.environ.get("OIDC_ISSUER") == "https://new.example.com")
        # The rollback itself re-registers, so the LAST applied config must be the old one.
        check("the live clients were put back on the previous configuration",
              reconfigure.applied[-1]["oidc_issuer"] == "https://new.example.com")
        check("...which took an extra apply, i.e. a real rollback happened",
              len(reconfigure.applied) == applied_before + 1)
        reconfigure.fail_on = None

        print("\n== A no-op save is honest about changing nothing ==")
        r = c.put("/api/auth/providers", json={"oidc_display_name": "Entra"})
        check("200", r.status_code == 200)
        check("no fields reported changed", r.get_json()["changed"] == [])
        with fleet.get_conn(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) c FROM audit_log WHERE action='auth.providers.update'"
            ).fetchone()["c"]
        check("and it writes no audit row", count == 2)   # the two real changes only

        print("\n== Turning ONE provider off is allowed while the other remains ==")
        r = c.put("/api/auth/providers",
                  json={"google_client_id": "", "google_client_secret": ""})
        check("200", r.status_code == 200)
        check("Google is off", r.get_json()["google_enabled"] is False)
        check("...removed from .env, not blanked",
              "GOOGLE_CLIENT_ID" not in envfile.read_all(env_path))
        check("OIDC still signs people in", r.get_json()["oidc_enabled"] is True)
    finally:
        for key, value in saved_environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in (env_path,):
            try:
                os.remove(path)
            except OSError:
                pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
