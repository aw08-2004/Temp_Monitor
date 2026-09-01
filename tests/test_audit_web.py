"""HTTP-layer test for audit_web.py: the Audit Log API and its two-capability gate.

Wires the blueprint onto a minimal Flask app with a fake login_required, exactly like
test_users_web.py -- app.py itself can't be imported here without a Google OAuth config.

What this module is really pinning down is the perimeter, because getting it wrong is a
disclosure rather than a bug: view_audit_log opens the tab, view_security_audit is a
modifier that widens which LEVELS come back, and the widening happens in SQL. So the
assertions below don't just check a status code -- they walk the whole log, with search
terms and an explicit ?level=security, and insist no security row is ever in the JSON.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import i18n
from audit_web import create_audit_blueprint
from permissions_web import create_access
from flask import Blueprint, Flask

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


def _register_sidebar_stubs(app):
    """base.html includes the shared sidebar, which url_for()s every other page. app.py
    defines those; this minimal app must stand them up or rendering /audit is a BuildError
    that has nothing to do with the audit log. Stubbing them also means the sidebar really
    is exercised here -- including the capability gate on the Audit Log link itself."""
    for endpoint in ("index", "inventory_page", "alerts_page", "tools_page",
                     "remote_page", "settings_page", "permissions_page", "logout"):
        app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "", methods=["GET"])
    for name, endpoint in (("packages", "packages_page"), ("backups", "backups_page"),
                           ("users", "users_page"), ("bios", "firmware_page"),
                           ("rules", "rules_page"),
                           ("patches", "patches_page"),
                           ("apitokens", "download_page"),
                           ("sharing", "sharing_page")):
        bp = Blueprint(name, __name__)
        bp.add_url_rule(f"/_stub/{name}", endpoint, lambda: "", methods=["GET"])
        app.register_blueprint(bp)


def build_app(db_path):
    app = Flask(__name__, template_folder=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "templates"))
    app.secret_key = "test"
    access = create_access(db_path, SUPERUSERS)
    app.register_blueprint(create_audit_blueprint(db_path, fake_login_required, access))
    _register_sidebar_stubs(app)

    # app.py's context processor supplies these to every render; without them the shared
    # sidebar has no capability set and the Audit Log link would never appear.
    @app.context_processor
    def _nav_context():
        from flask import session
        email = (session.get("user") or {}).get("email")
        context = {"cap": permissions, "hub_version": "test",
                   "user_capabilities": permissions.effective_permissions(
                       db_path, email, superusers=SUPERUSERS)["capabilities"],
                   "open_alert_count": 0, "is_superuser": email in SUPERUSERS,
                   "latest_agent_version": None}
        # The real t(), in English -- not a stub. A no-op t() here would let a page ship
        # with a mistyped key that this test renders happily.
        context.update(i18n.template_context("en"))
        return context

    @app.before_request
    def _seed_session():
        from flask import session
        session["user"] = {"email": CURRENT_USER}

    return app, access


def seed(db_path):
    """One row per level, plus a security row whose target invites a search for it."""
    fleet.audit(db_path, "ann@x.com", "backup_key_reveal", "PC-secret")
    fleet.audit(db_path, "ann@x.com", "remote_session_start", "PC-secret")
    fleet.audit(db_path, "bob@x.com", "alert.dismiss", "PC-secret")
    fleet.audit(db_path, "agent:PC-1", "complete_command", "PC-1")


def all_entries(c, query=""):
    """Page through /api/audit to exhaustion and return every entry the caller was given.
    Paging matters here: a perimeter that only holds on the first page is not a perimeter.
    """
    entries, cursor, guard = [], None, 0
    while guard < 20:
        guard += 1
        url = "/api/audit?limit=1" + (("&" + query) if query else "")
        if cursor:
            url += f"&before_ts={cursor['ts']}&before_id={cursor['id']}"
        body = c.get(url).get_json()
        entries.extend(body["entries"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    return entries


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        permissions.init_permissions_db(db_path)
        fleet.init_fleet_db(db_path)
        permissions.invalidate()
        seed(db_path)

        app, access = build_app(db_path)
        c = app.test_client()

        print("\n== A superuser sees the whole trail ==")
        check("GET /audit 200", c.get("/audit").status_code == 200)
        check("the page renders", "Audit Log" in c.get("/audit").get_data(as_text=True))
        body = c.get("/api/audit").get_json()
        check("GET /api/audit 200 with every level",
              {e["level"] for e in body["entries"]} == set(fleet.AUDIT_LEVELS))
        check("can_view_security is reported to the page", body["can_view_security"] is True)
        check("actors 200", c.get("/api/audit/actors").status_code == 200)

        print("\n== No audit capability: refused everywhere ==")
        caps_without_audit = [x for x in permissions.CAPABILITIES
                              if x not in (permissions.VIEW_AUDIT_LOG,
                                           permissions.VIEW_SECURITY_AUDIT)]
        permissions.create_group(db_path, name="Almost Everything",
                                 capabilities=caps_without_audit,
                                 scope_mode=permissions.SCOPE_ALL,
                                 members=["mallory@x.com"], actor="root@x.com")
        CURRENT_USER = "mallory@x.com"
        check("page 403", c.get("/audit").status_code == 403)
        check("api 403", c.get("/api/audit").status_code == 403)
        check("actors 403", c.get("/api/audit/actors").status_code == 403)

        print("\n== view_security_audit alone grants nothing ==")
        permissions.create_group(db_path, name="Security Only",
                                 capabilities=[permissions.VIEW_SECURITY_AUDIT],
                                 scope_mode=permissions.SCOPE_ALL,
                                 members=["solo@x.com"], actor="root@x.com")
        CURRENT_USER = "solo@x.com"
        check("the modifier does not open the page", c.get("/audit").status_code == 403)
        check("the modifier does not open the api", c.get("/api/audit").status_code == 403)

        print("\n== view_audit_log alone: everything except security rows ==")
        permissions.create_group(db_path, name="Auditors",
                                 capabilities=[permissions.VIEW_AUDIT_LOG],
                                 scope_mode=permissions.SCOPE_ALL,
                                 members=["aud@x.com"], actor="root@x.com")
        CURRENT_USER = "aud@x.com"
        body = c.get("/api/audit").get_json()
        check("the tab opens", c.get("/audit").status_code == 200)
        check("no security row in the first page",
              all(e["level"] != fleet.LEVEL_SECURITY for e in body["entries"]))
        check("can_view_security is False", body["can_view_security"] is False)
        check("the offered levels exclude security",
              fleet.LEVEL_SECURITY not in body["levels"])
        check("no security row anywhere in the paged-through log",
              all(e["level"] != fleet.LEVEL_SECURITY for e in all_entries(c)))
        check("...nor when searching for a security row's target",
              all(e["level"] != fleet.LEVEL_SECURITY for e in all_entries(c, "q=PC-secret"))
              and len(all_entries(c, "q=PC-secret")) == 1)
        # Asking for a level you may not read is answered honestly with nothing, rather
        # than a 403 that would itself confirm the rows exist.
        r = c.get("/api/audit?level=security")
        check("?level=security is 200 and empty, not 403",
              r.status_code == 200 and r.get_json()["entries"] == [])
        check("an actor seen only in security rows is not enumerable",
              "ann@x.com" not in c.get("/api/audit/actors").get_json()["actors"])

        print("\n== Both capabilities: security rows appear ==")
        permissions.create_group(db_path, name="Security Auditors",
                                 capabilities=[permissions.VIEW_AUDIT_LOG,
                                               permissions.VIEW_SECURITY_AUDIT],
                                 scope_mode=permissions.SCOPE_ALL,
                                 members=["sec@x.com"], actor="root@x.com")
        CURRENT_USER = "sec@x.com"
        body = c.get("/api/audit").get_json()
        check("security rows are returned",
              any(e["level"] == fleet.LEVEL_SECURITY for e in body["entries"]))
        check("...and the level filter can narrow to them",
              all(e["level"] == fleet.LEVEL_SECURITY
                  for e in c.get("/api/audit?level=security").get_json()["entries"]))
        check("the security-only actor is now enumerable",
              "ann@x.com" in c.get("/api/audit/actors").get_json()["actors"])

        print("\n== Query params are clamped and validated ==")
        check("limit is clamped to the page maximum",
              len(c.get("/api/audit?limit=99999").get_json()["entries"]) <= 200)
        check("a non-numeric limit falls back to the default",
              c.get("/api/audit?limit=abc").status_code == 200)
        r = c.get("/api/audit?from=garbage")
        check("a bad date is a 400 with a usable message",
              r.status_code == 400 and "YYYY-MM-DD" in r.get_json()["error"])
        check("a bare from-date is accepted",
              c.get("/api/audit?from=2000-01-01").status_code == 200)
        # A bare to-date must cover the whole day, or "from X to X" is an empty range.
        check("from and to on the same day cover that day",
              len(c.get("/api/audit?from=1970-01-01&to=1970-01-01").get_json()["entries"]) == 0
              and c.get("/api/audit?from=2000-01-01&to=2099-01-01").get_json()["entries"])
        check("an unknown level narrows to nothing rather than widening",
              c.get("/api/audit?level=nonsense").get_json()["entries"] == [])

        print("\n== Paging over HTTP returns each row once ==")
        # The log has grown past the seed by now: creating the permission groups above is
        # itself audited, which is exactly the sort of interleaving paging has to survive.
        ids = [e["id"] for e in all_entries(c)]
        in_one_page = [e["id"] for e in c.get("/api/audit?limit=200").get_json()["entries"]]
        check("paging one row at a time returns each row exactly once",
              sorted(ids) == sorted(set(ids)) and len(ids) > 4)
        check("...and the same rows a single large page returns",
              sorted(ids) == sorted(in_one_page))
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
