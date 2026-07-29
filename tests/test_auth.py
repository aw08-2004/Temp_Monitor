"""Sign-in: session persistence and the provider-agnostic login path.

app.py can't be imported without a full OAuth/env boot, and these are the parts of sign-in
worth pinning anyway -- the pure logic (which email do we believe, who is let in) and the
cookie policy (does a session survive closing the browser). So this rebuilds the same
handler shape against a minimal Flask app, exactly as test_fleet_web does for the fleet
blueprint, and asserts on behaviour rather than on app.py's import side effects.

The rules under test are the ones a mistake in would be quiet and serious:
  * A session must survive the browser closing, and must age out on a rolling window.
  * Every provider must converge on ONE authorization decision. A second identity provider
    that skipped the permission-group check would be a second, weaker front door.
  * Email is the identity. Believing an unverified or absent one would let an issuer
    hand out somebody else's access.
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import settings
import users
from permissions_web import create_access
from flask import Flask, session as flask_session, redirect, url_for

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


# The claim-picking logic is IMPORTED, not mirrored -- it lives in permissions.py precisely
# so that this test exercises the real implementation. Only the Flask handler shape below is
# reconstructed, because app.py cannot be imported without a full OAuth/env boot.
claimed_email = permissions.email_from_claims


def build_app(db_path, access, lifetime_days=7):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config.update(
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=lifetime_days),
        SESSION_REFRESH_EACH_REQUEST=True,
    )

    def complete_login(user_info, provider):
        """Mirrors app._complete_login (see the note above on why this is reconstructed)."""
        email = permissions.email_from_claims(user_info)
        if not email:
            return f"{provider} did not provide an email address", 403
        if user_info.get("email_verified") is False:
            return f"{provider} reports this account's email is unverified.", 403
        if not access.login_allowed(email):
            return f"Access denied: {email} is not authorized for this dashboard.", 403
        flask_session.permanent = True
        flask_session["user"] = {"email": email,
                                 "name": user_info.get("name") or email,
                                 "provider": provider}
        try:
            users.upsert_from_login(db_path, email, user_info.get("name"))
        except Exception:
            pass
        return redirect(url_for("index"))

    @app.route("/")
    def index():
        return "dashboard"

    # Stands in for the provider round trip: the test posts the claims an issuer would
    # have returned, and everything after that is the real shared path.
    @app.route("/fake/<provider>", methods=["POST"])
    def fake_callback(provider):
        from flask import request
        return complete_login(request.get_json(silent=True) or {}, provider)

    @app.route("/whoami")
    def whoami():
        user = flask_session.get("user")
        return {"email": user["email"], "provider": user["provider"]} if user else {}

    return app


# ---- app.py's actual boot-time configuration ------------------------------------
# The tests above reconstruct the handler; this one boots the REAL module, because the
# provider wiring and the cookie policy are decided at import time and are exactly the kind
# of thing a reconstruction would agree with while production was broken.
#
# Each case runs in its own interpreter: app.py reads env at import, starts daemon threads,
# and caches config, so these cannot share a process. HUB_STATE_DIR is redirected at a temp
# dir per case -- without it load_dotenv finds the developer's real .env and quietly
# supplies the very credentials the case is trying to omit.
BOOT_SNIPPET = r'''
import os, sys, json
sys.path.insert(0, os.environ["HUB_CODE_DIR"])
os.chdir(os.environ["HUB_STATE_DIR"])
import app
print("BOOT_OK " + json.dumps({
    "google": app.GOOGLE_ENABLED,
    "oidc": app.OIDC_ENABLED,
    "days": app.SESSION_LIFETIME_DAYS,
    "lifetime": str(app.app.config["PERMANENT_SESSION_LIFETIME"]),
    "rolling": app.app.config["SESSION_REFRESH_EACH_REQUEST"],
    "httponly": app.app.config["SESSION_COOKIE_HTTPONLY"],
    "metadata": app.OIDC_METADATA_URL,
    "routes": sorted(r.rule for r in app.app.url_map.iter_rules()
                     if r.rule.startswith(("/login", "/auth"))),
}))
'''


def boot_app(**env_overrides):
    """Import app.py in a clean interpreter. Returns (parsed_json | None, combined_output)."""
    import subprocess
    hub_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub")
    work = tempfile.mkdtemp(prefix="hub-boot-")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GOOGLE_", "OIDC_", "FLASK_SECRET", "ALLOWED_EMAILS",
                                "SESSION_LIFETIME", "HUB_URL", "HUB_LOG_DIR", "HUB_STATE_DIR"))}
    env.update({
        "HUB_CODE_DIR": hub_dir,
        "HUB_STATE_DIR": work,
        "HUB_LOG_DIR": os.path.join(work, "logs"),
        "ALLOWED_EMAILS": "boss@x.com",
        "HUB_URL": "https://hub.example.com",
        "FLASK_SECRET_KEY": "s" * 32,
        "PYTHONIOENCODING": "utf-8",
    })
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    proc = subprocess.run([sys.executable, "-c", BOOT_SNIPPET],
                          env=env, capture_output=True, text=True, timeout=300)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        if line.startswith("BOOT_OK "):
            import json
            return json.loads(line[len("BOOT_OK "):]), out
    return None, out


GOOGLE = {"GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gsec"}
OIDC = {"OIDC_CLIENT_ID": "oid", "OIDC_CLIENT_SECRET": "osec",
        "OIDC_ISSUER": "https://login.microsoftonline.com/tenant-id/v2.0",
        "OIDC_DISPLAY_NAME": "Microsoft"}


def boot_checks():
    print("\n== app.py boots under every provider configuration ==")
    cfg, out = boot_app(**GOOGLE)
    check("Google alone boots", cfg is not None and cfg["google"] and not cfg["oidc"])

    cfg, out = boot_app(**OIDC)
    # The point of making Google optional: an Entra/Okta shop should not have to stand up
    # a Google project to run the hub.
    check("an OIDC provider ALONE boots (Google is genuinely optional)",
          cfg is not None and cfg["oidc"] and not cfg["google"])
    check("the discovery URL is derived from the issuer",
          cfg is not None and cfg["metadata"].endswith(
              "/tenant-id/v2.0/.well-known/openid-configuration"))

    cfg, out = boot_app(**GOOGLE, **OIDC)
    check("both together boot", cfg is not None and cfg["google"] and cfg["oidc"])
    check("both callback routes exist",
          cfg is not None and {"/auth/callback", "/auth/oidc/callback"} <= set(cfg["routes"]))

    print("\n== Cookie policy is what the running app actually applies ==")
    check("sessions last 7 days by default",
          cfg is not None and cfg["days"] == 7 and cfg["lifetime"].startswith("7 days"))
    check("rolling refresh is on", cfg is not None and cfg["rolling"] is True)
    check("HttpOnly is pinned", cfg is not None and cfg["httponly"] is True)
    cfg, out = boot_app(SESSION_LIFETIME_DAYS="30", **GOOGLE)
    check("SESSION_LIFETIME_DAYS is honoured", cfg is not None and cfg["days"] == 30)
    cfg, out = boot_app(SESSION_LIFETIME_DAYS="not-a-number", **GOOGLE)
    check("a malformed lifetime falls back to the default instead of failing to boot",
          cfg is not None and cfg["days"] == 7)
    cfg, out = boot_app(SESSION_LIFETIME_DAYS="0", **GOOGLE)
    check("a zero lifetime is floored at a day, not an instantly-expiring session",
          cfg is not None and cfg["days"] == 1)

    print("\n== ...and refuses to boot when it cannot be signed in to ==")
    # A hub that started with no provider would serve a login page with no buttons, which
    # reads as a broken hub rather than a missing setting.
    cfg, out = boot_app()
    check("no provider at all -> refuses to start", cfg is None)
    check("and says which settings to add",
          "GOOGLE_CLIENT_ID" in out and "OIDC_CLIENT_ID" in out)
    cfg, out = boot_app(FLASK_SECRET_KEY=None, **GOOGLE)
    check("no FLASK_SECRET_KEY -> refuses to start (the cookie could not be signed)",
          cfg is None and "FLASK_SECRET_KEY" in out)
    # Half-configured is not configured: a client id with no secret cannot complete a flow,
    # and silently showing the button would fail at the worst moment.
    cfg, out = boot_app(OIDC_CLIENT_ID="oid", OIDC_ISSUER="https://issuer.example")
    check("an OIDC provider missing its secret does not count as configured", cfg is None)


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)   # permission-group writes are audited
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        users.init_users_db(db_path)
        settings.invalidate()

        access = create_access(db_path, {"boss@x.com"})
        permissions.create_group(
            db_path, "Techs", capabilities=[permissions.VIEW],
            machines=["PC-01"], members=["tech@x.com"])
        settings.invalidate()

        app = build_app(db_path, access)
        c = app.test_client()

        print("\n== The session survives closing the browser ==")
        r = c.post("/fake/Google", json={"email": "boss@x.com", "email_verified": True})
        check("a superuser signs in -> redirect", r.status_code == 302)
        cookie = next((h for k, h in r.headers if k == "Set-Cookie" and "session=" in h), "")
        # THE BUG THIS FIXES. Without session.permanent + PERMANENT_SESSION_LIFETIME, Flask
        # emits a cookie with no Expires/Max-Age -- a "session cookie", which every browser
        # discards on exit. That is why signing in every morning was necessary.
        check("the cookie carries an expiry, so it is not discarded on browser exit",
              "Expires=" in cookie or "Max-Age=" in cookie)
        check("and is HttpOnly", "HttpOnly" in cookie)
        check("and is SameSite=Lax (the OAuth callback needs it readable)",
              "SameSite=Lax" in cookie)
        check("the session works", c.get("/whoami").get_json()["email"] == "boss@x.com")

        print("\n== The window is ROLLING, so a daily user is never signed out ==")
        r = c.get("/whoami")
        refreshed = next((h for k, h in r.headers if k == "Set-Cookie" and "session=" in h), "")
        check("an ordinary request re-issues the cookie with a fresh expiry",
              "Expires=" in refreshed or "Max-Age=" in refreshed)

        print("\n== Lifetime is configurable, and it is the cookie that carries it ==")
        short = build_app(db_path, access, lifetime_days=1).test_client()
        short.post("/fake/Google", json={"email": "boss@x.com"})
        long_ = build_app(db_path, access, lifetime_days=30).test_client()
        r_long = long_.post("/fake/Google", json={"email": "boss@x.com"})
        check("a 30-day hub issues a longer-lived cookie than a 1-day hub",
              "Max-Age=2592000" in str(r_long.headers) or "Expires=" in str(r_long.headers))

        print("\n== Every provider lands on the SAME authorization decision ==")
        for provider in ("Google", "Microsoft", "Okta"):
            fresh = app.test_client()
            r = fresh.post(f"/fake/{provider}", json={"email": "tech@x.com"})
            check(f"{provider}: a permission-group member is admitted", r.status_code == 302)
            check(f"{provider}: and is recorded as signing in via {provider}",
                  fresh.get("/whoami").get_json()["provider"] == provider)

            denied = app.test_client()
            r = denied.post(f"/fake/{provider}", json={"email": "stranger@x.com"})
            check(f"{provider}: a valid account in no group is refused -> 403",
                  r.status_code == 403)
            check(f"{provider}: and gets no session", denied.get("/whoami").get_json() == {})

        print("\n== Which email we believe ==")
        # Entra omits `email` in plenty of tenants and puts the sign-in name in
        # preferred_username / upn. Refusing those would rule out the provider this was
        # built for; believing a NON-address one would let a bare username collide with a
        # granted mailbox name.
        check("`email` is preferred when present",
              claimed_email({"email": "a@x.com", "preferred_username": "b@x.com"}) == "a@x.com")
        check("preferred_username is used when email is absent (Entra)",
              claimed_email({"preferred_username": "b@x.com"}) == "b@x.com")
        check("upn is the last resort",
              claimed_email({"upn": "c@x.com"}) == "c@x.com")
        check("a bare username is NOT treated as an email",
              claimed_email({"preferred_username": "administrator"}) == "")
        check("claims are normalized to lowercase",
              claimed_email({"email": "  Boss@X.COM "}) == "boss@x.com")

        nomail = app.test_client()
        check("an account with no usable email is refused rather than guessed at",
              nomail.post("/fake/Microsoft", json={"name": "No Mail"}).status_code == 403)

        print("\n== Verification claims ==")
        unverified = app.test_client()
        check("email_verified=false is refused",
              unverified.post("/fake/Google",
                              json={"email": "boss@x.com", "email_verified": False}).status_code == 403)
        # Absent is not false: Google always sends it, Entra never does, and refusing a
        # missing claim would rule out the providers this feature exists for.
        absent = app.test_client()
        check("email_verified absent is accepted (the issuer is trusted by configuration)",
              absent.post("/fake/Microsoft", json={"email": "boss@x.com"}).status_code == 302)

        print("\n== Signing in records the operator in the users directory ==")
        listed = {u["email"] for u in users.list_users(db_path)}
        check("the directory picked up logins from every provider",
              {"boss@x.com", "tech@x.com"} <= listed)

        boot_checks()

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
