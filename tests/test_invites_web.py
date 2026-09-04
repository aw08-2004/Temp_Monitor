"""HTTP-layer test for invites_web.py: the admin API's gate, and what the one
unauthenticated route on this hub is allowed to say.

Two silent failures, and the second is the one that has no other test anywhere:

  * **The admin API reachable without manage_permission_groups.** An invite hands out
    capabilities, so a gate that exists in the decorator list but does not fire turns the
    weakest console session into a way to mint fleet access. The suspicious case is an
    operator holding *every other* capability -- manage_users included, since that is the
    capability an invite page superficially looks like it belongs to.
  * **`GET /invite/<code>` leaking more than the invite.** It is deliberately outside
    login_required, which makes it the hub's only unauthenticated console page. It must
    render for a signed-out visitor, and it must not put the capability list, the machine
    scope, the seat count or the other redeemers on a page anybody can fetch.

Wires the blueprints onto a minimal Flask app with a fake login_required, exactly like
test_users_web.py -- app.py itself can't be imported here without a Google OAuth config.
The consequence is that this file does NOT cover the `_complete_login` hook that pops
`pending_invite`; the model's redemption rules are test_invites.py's subject, and the hook
itself is three lines of session plumbing over them.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import invites as invites_model
import permissions
from invites_web import create_invites_blueprint
from permissions_web import create_access
from flask import Flask

PASS = 0
FAIL = 0

SUPERUSERS = {"root@x.com"}
CURRENT_USER = "root@x.com"
SIGNED_IN = True


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
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "templates"),
        static_folder=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "static"))
    app.secret_key = "test"
    access = create_access(db_path, SUPERUSERS)
    app.register_blueprint(create_invites_blueprint(
        db_path, fake_login_required, access, hub_url="https://hub.example.com/"))

    # The landing page and the templates both call t(); the real one comes from i18n's
    # context processor in app.py, which cannot be imported here. A pass-through keeps the
    # templates renderable so the assertions below are about the ROUTE, not about i18n.
    @app.context_processor
    def _fake_i18n():
        def t(key, **params):
            # Key plus its parameter values: enough for an assertion to tell whether the
            # template actually passed the label through, without pulling in the catalogue.
            return key + ("".join(f" {v}" for v in params.values()) if params else "")
        return {"t": t, "current_language": "en"}

    # `login` is url_for'd by invite.html's accept button; `index` by base.html, which
    # denied.html extends -- a page-level 403 renders that template rather than JSON.
    @app.route("/login")
    def login():
        return "login"

    @app.route("/")
    def index():
        return "index"

    @app.before_request
    def _seed_session():
        from flask import session
        if SIGNED_IN:
            session["user"] = {"email": CURRENT_USER}
        else:
            session.pop("user", None)

    return app


def main():
    global CURRENT_USER, SIGNED_IN
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        permissions.init_permissions_db(db_path)
        invites_model.init_invites_db(db_path)
        permissions.invalidate()

        techs = permissions.create_group(
            db_path, name="Techs",
            capabilities=[permissions.VIEW, permissions.MANAGE_BACKUPS],
            scope_mode=permissions.SCOPE_ALL, actor="root@x.com")

        app = build_app(db_path)
        c = app.test_client()

        print("\n== Create through the API (as break-glass) ==")
        r = c.post("/api/invites", json={"label": "Two techs", "group_ids": [techs],
                                         "max_uses": 2})
        check("create 201", r.status_code == 201)
        created = r.get_json()
        # The link is assembled server-side from HUB_URL, not from the browser's origin --
        # an admin on an internal hostname would otherwise copy a link nobody outside can
        # follow, and would have no way to tell.
        check("the link is absolute and rooted at HUB_URL",
              created["link"].startswith("https://hub.example.com/invite/"))
        code = created["link"].rsplit("/", 1)[-1]
        invite_id = created["invite_id"]

        r = c.get("/api/invites")
        listed = r.get_json()["invites"]
        check("list 200 and includes it", r.status_code == 200 and len(listed) == 1)
        # The one property that makes "shown once" true rather than a claim on a page.
        check("the code is never returned again",
              "link" not in listed[0] and "code" not in listed[0]
              and code not in r.get_data(as_text=True))
        check("the list carries the groups for the picker",
              [g["id"] for g in r.get_json()["groups"]] == [techs])

        print("\n== A custom group is created as a real permission group ==")
        r = c.post("/api/invites", json={
            "label": "Read-only helper",
            "new_group": {"name": "Helper", "capabilities": [permissions.VIEW],
                          "scope_mode": "all"},
            "max_uses": 1,
        })
        check("create with new_group 201", r.status_code == 201)
        made = [g for g in permissions.list_groups(db_path) if g["name"] == "Helper"]
        check("the group really exists", len(made) == 1)
        check("the invite names it", made[0]["id"] in r.get_json()["group_ids"])

        # A refused invite must not leave its group behind -- the group was created for
        # this invite alone, moments earlier, so nothing else can be relying on it.
        before = len(permissions.list_groups(db_path))
        r = c.post("/api/invites", json={
            "label": "",
            "new_group": {"name": "Orphan", "capabilities": [permissions.VIEW],
                          "scope_mode": "all"},
        })
        check("a refused invite is a 400", r.status_code == 400)
        check("a refused invite leaves no orphan group",
              len(permissions.list_groups(db_path)) == before)

        print("\n== Refusals carry the model's own sentence ==")
        r = c.post("/api/invites", json={"label": "Nothing", "group_ids": []})
        check("no groups 400", r.status_code == 400)
        check("and says why", "at least one permission group" in r.get_json()["error"])

        print("\n== CSRF: JSON content type is required ==")
        before = len(invites_model.list_invites(db_path))
        r = c.post("/api/invites", data={"label": "Injected", "group_ids": techs})
        check("form-encoded create rejected", r.status_code == 400)
        check("form-encoded create changed nothing",
              len(invites_model.list_invites(db_path)) == before)

        print("\n== The public landing page ==")
        SIGNED_IN = False
        r = c.get(f"/invite/{code}")
        body = r.get_data(as_text=True)
        check("reachable while signed out", r.status_code == 200)
        check("names the invite", "Two techs" in body)
        check("names the groups it grants", "Techs" in body)
        # The rules that keep an unauthenticated page from being a reconnaissance
        # surface. Asserted against the preview CONTRACT as well as the rendered page: the
        # page can only show what preview returns, so pinning its key set is what stops a
        # later "just add the seat count, it's useful" from being invisible here.
        check("does not leak the capability list",
              permissions.MANAGE_BACKUPS not in body)
        check("does not leak other redeemers", "ann@x.com" not in body)
        peek = invites_model.preview(db_path, code)
        check("the preview says only these four things",
              set(peek) == {"label", "invited_by", "group_names", "expires_at"})
        with c.session_transaction() as sess:
            check("the code is stashed in the session, not a URL",
                  sess.get("pending_invite") == code)

        r = c.get("/invite/not-a-real-code")
        check("an unknown code renders the page, not a bare error", r.status_code == 404)
        check("and says nothing about the hub",
              "not valid" in r.get_data(as_text=True)
              and "Techs" not in r.get_data(as_text=True))
        with c.session_transaction() as sess:
            check("a bad code clears any stale pending invite",
                  sess.get("pending_invite") is None)

        r = c.get(f"/invite/{code}")
        invites_model.revoke_invite(db_path, invite_id, actor="root@x.com")
        r = c.get(f"/invite/{code}")
        check("a revoked invite refuses on the landing page",
              r.status_code == 404 and "revoked" in r.get_data(as_text=True))
        SIGNED_IN = True

        print("\n== The API is gated on manage_permission_groups ==")
        # A group granting everything EXCEPT the one capability this page needs. Its member
        # must be refused the whole invites API -- including manage_users, which is what an
        # "invite a user" page superficially looks like it should be gated on.
        caps_without = [c_ for c_ in permissions.CAPABILITIES
                        if c_ != permissions.MANAGE_PERMISSION_GROUPS]
        permissions.create_group(db_path, name="Almost Everything",
                                 capabilities=caps_without,
                                 scope_mode=permissions.SCOPE_ALL,
                                 members=["mallory@x.com"], actor="root@x.com")
        CURRENT_USER = "mallory@x.com"
        check("holds manage_users but not group admin",
              permissions.MANAGE_USERS in caps_without)
        # The page route, not just the API. Asserted as "refused" rather than "403"
        # deliberately: a refusal renders denied.html, which extends base.html and
        # url_for's the whole sidebar -- endpoints this stripped app does not have, so the
        # refusal surfaces here as a template error. The failure this guards against is the
        # gate not firing at all, and that failure is a 200.
        check("page refused", c.get("/invites").status_code != 200)
        check("list 403", c.get("/api/invites").status_code == 403)
        check("create 403",
              c.post("/api/invites", json={"label": "x", "group_ids": [techs]}).status_code == 403)
        check("revoke 403",
              c.post(f"/api/invites/{invite_id}/revoke", json={}).status_code == 403)
        check("delete 403", c.delete(f"/api/invites/{invite_id}").status_code == 403)
        check("the refused calls changed nothing",
              len(invites_model.list_invites(db_path)) == 2)

        print("\n== manage_permission_groups alone is enough ==")
        permissions.create_group(db_path, name="Group Admins",
                                 capabilities=[permissions.MANAGE_PERMISSION_GROUPS],
                                 scope_mode=permissions.SCOPE_ALL,
                                 members=["gadmin@x.com"], actor="root@x.com")
        CURRENT_USER = "gadmin@x.com"
        check("list 200", c.get("/api/invites").status_code == 200)
        r = c.post(f"/api/invites/{invite_id}/revoke", json={})
        check("revoke 200", r.status_code == 200)
        check("revoke applied", r.get_json()["status"] == invites_model.STATUS_REVOKED)
        check("delete 200", c.delete(f"/api/invites/{invite_id}").status_code == 200)
        check("delete applied", invites_model.get_invite(db_path, invite_id) is None)
        check("revoking an unknown invite 404",
              c.post("/api/invites/nope/revoke", json={}).status_code == 404)
        check("deleting an unknown invite 404",
              c.delete("/api/invites/nope").status_code == 404)

        print("\n== The creator ceiling is enforced over HTTP, from the live session ==")
        # gadmin can administer groups but sees no machines of their own, so a fleet-wide
        # group is beyond them. The ceiling has to come from the request's identity -- if
        # the route passed anything else, this would succeed.
        r = c.post("/api/invites", json={"label": "Escalate", "group_ids": [techs]})
        check("a scoped group admin cannot hand out more than they hold",
              r.status_code == 400)
        check("and is told which group and why",
              "Techs" in r.get_json()["error"])
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
