"""Pins the markup contract the mobile navigation depends on.

Below 900px the sidebar stops being a column of the layout grid and becomes an off-canvas
drawer: components.css does the moving, common.js does the opening. Neither is testable
here -- there is no browser harness in this repo -- but both are wired to the templates by
nothing more than a handful of ids and class names. Rename one and the drawer silently
stops opening, on phones only, with every Python test still green.

So this module asserts the joins rather than the behaviour: that the ids the JS looks up
exist in the rendered HTML, that aria-controls actually points at the element it names,
and that the CSS and JS files still mention the selectors they're supposed to own. That is
a real class of regression caught cheaply; the visual result still needs eyes on a phone.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import i18n
import permissions
from flask import Blueprint, Flask, render_template_string

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "hub", "static")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def _register_sidebar_stubs(app):
    """base.html includes the shared sidebar, which url_for()s every other page -- app.py
    defines those, so this minimal app must stand them up or rendering is a BuildError
    that has nothing to do with the nav. Same helper as test_audit_web.py."""
    for endpoint in ("index", "inventory_page", "alerts_page", "tools_page",
                     "remote_page", "settings_page", "permissions_page", "logout"):
        app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "", methods=["GET"])
    for name, endpoint in (("packages", "packages_page"), ("backups", "backups_page"),
                           ("users", "users_page"), ("audit", "audit_page"),
                           ("bios", "firmware_page"), ("rules", "rules_page"),
                           ("patches", "patches_page"),
                           ("apitokens", "download_page")):
        bp = Blueprint(name, __name__)
        bp.add_url_rule(f"/_stub/{name}", endpoint, lambda: "", methods=["GET"])
        app.register_blueprint(bp)


def build_app():
    app = Flask(__name__, template_folder=os.path.join(ROOT, "hub", "templates"),
                static_folder=STATIC)
    app.secret_key = "test"
    _register_sidebar_stubs(app)

    # A bare page that extends base.html: the shell is what's under test, not any one view.
    @app.route("/_shell")
    def shell():
        return render_template_string('{% extends "base.html" %}{% block content %}hi{% endblock %}')

    @app.before_request
    def _seed_session():
        from flask import session
        session["user"] = {"email": "root@x.com"}

    # Mirrors app.py's inject_nav_context. Every capability is granted so the full nav
    # renders -- the drawer has to work for the operator who can see all ten links.
    @app.context_processor
    def _nav_context():
        context = {"cap": permissions, "hub_version": "test",
                   "user_capabilities": set(permissions.CAPABILITIES),
                   "open_alert_count": 3, "is_superuser": True,
                   "latest_agent_version": "8.8.8"}
        context.update(i18n.template_context("en"))
        return context

    return app


def read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_drawer_markup():
    print("\n-- the drawer's ids and ARIA wiring survive in the rendered page --")
    body = build_app().test_client().get("/_shell").get_data(as_text=True)

    check("the sidebar carries the id the toggle targets", 'id="app-sidebar"' in body)
    check("the hamburger is rendered", 'id="nav-toggle"' in body)
    check("...pointing at the sidebar", 'aria-controls="app-sidebar"' in body)
    check("...and starting closed", 'aria-expanded="false"' in body)
    check("the scrim is present and hidden by default",
          'id="nav-scrim"' in body and 'class="nav-scrim" id="nav-scrim" hidden' in body)
    check("the drawer has its own close button", 'id="nav-close"' in body)
    check("the mobile brand is rendered", 'class="topbar__brand"' in body)


def test_topbar_overflow_markup():
    print("\n-- the topbar overflow panel wraps the secondary chrome --")
    body = build_app().test_client().get("/_shell").get_data(as_text=True)

    check("the overflow button is rendered", 'id="topbar-more"' in body)
    check("...pointing at the panel", 'aria-controls="topbar-meta"' in body)
    check("the panel exists", 'id="topbar-meta"' in body)

    panel = body.split('id="topbar-meta"', 1)[1].split("</div>", 1)[0]
    check("the version badge moved inside the panel", "Hub vtest" in panel)
    check("...along with the agent badge", "Agent v8.8.8" in panel)
    check("...and the signed-in user", "root@x.com" in panel and "Sign out" in panel)
    # These two are glanceable state; putting them behind a tap defeats the point.
    check("the theme toggle stays out of the panel", "theme-toggle" not in panel)


def test_nav_still_intact():
    print("\n-- the drawer is the same nav, not a second copy --")
    body = build_app().test_client().get("/_shell").get_data(as_text=True)

    check("only one nav element is rendered", body.count("sidebar__nav") == 1)
    for label in ("Dashboard", "History", "Asset Inventory", "Alerts", "Audit Log",
                  "Packages", "Backups", "Settings", "Permission Groups", "Users"):
        check(f"{label} is reachable", label in body)
    check("the open-alert badge still renders", "sidebar__badge" in body)


def test_css_and_js_selectors():
    print("\n-- CSS and JS still own the classes the markup hands them --")
    css = read("css", "components.css")
    js = read("js", "common.js")

    check("the drawer's open state is styled", ".sidebar--open" in css)
    check("the scrim is styled", ".nav-scrim" in css)
    check("the overflow panel's open state is styled", ".topbar__meta--open" in css)
    check("the mobile-only controls are styled", ".nav-toggle" in css and ".topbar__more" in css)
    # The rules must live in components.css, which loads last: media queries add no
    # specificity, so a copy in base.css would lose to the base .sidebar on source order.
    check("the breakpoint matches base.css's layout collapse", "max-width: 900px" in css)

    check("the drawer initialiser is registered", "initMobileNav" in js)
    check("the overflow initialiser is registered", "initTopbarMore" in js)
    check("JS and templates agree on the open class", "sidebar--open" in js)
    check("JS and CSS agree on the breakpoint", "(max-width: 900px)" in js)


if __name__ == "__main__":
    test_drawer_markup()
    test_topbar_overflow_markup()
    test_nav_still_intact()
    test_css_and_js_selectors()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
