"""Pins the desktop sidebar rail, and the one content width every page now shares.

Two style fixes with the same subject -- how much of the window the page itself gets.

  1. .app-shell__content used to cap at 1280px for everyone and 1600px for Packages
     alone, which made every other tab look like it had been indented by accident. There
     is now one cap, and no per-page opt-in class to drift out of sync with it.
  2. The sidebar collapses to a 64px icon rail on desktop, not just into a drawer below
     900px. The state is an attribute on <html> so base.html's inline head script can set
     it before first paint; CSS reads it, common.js writes it.

Like test_mobile_nav.py, this asserts the joins rather than the behaviour -- there is no
browser here. What it can catch cheaply is the whole failure mode of a three-file feature
wired together by nothing but a string: rename the attribute in one of them and the button
goes dead silently, on desktop only, with every other test still green.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import i18n
import permissions
from flask import Blueprint, Flask, render_template_string

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "hub")
STATIC = os.path.join(HUB, "static")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def _register_sidebar_stubs(app):
    """base.html includes the shared sidebar, which url_for()s every other page. Same
    helper as test_mobile_nav.py -- without it rendering is a BuildError unrelated to
    anything under test here."""
    for endpoint in ("index", "inventory_page", "alerts_page", "tools_page",
                     "remote_page", "settings_page", "permissions_page", "logout"):
        app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "", methods=["GET"])
    for name, endpoint in (("packages", "packages_page"), ("backups", "backups_page"),
                           ("users", "users_page"), ("audit", "audit_page"),
                           ("bios", "firmware_page"), ("rules", "rules_page"),
                           ("apitokens", "download_page")):
        bp = Blueprint(name, __name__)
        bp.add_url_rule(f"/_stub/{name}", endpoint, lambda: "", methods=["GET"])
        app.register_blueprint(bp)


def build_app():
    app = Flask(__name__, template_folder=os.path.join(HUB, "templates"),
                static_folder=STATIC)
    app.secret_key = "test"
    _register_sidebar_stubs(app)

    @app.route("/_shell")
    def shell():
        return render_template_string('{% extends "base.html" %}{% block content %}hi{% endblock %}')

    @app.before_request
    def _seed_session():
        from flask import session
        session["user"] = {"email": "root@x.com"}

    @app.context_processor
    def _nav_context():
        context = {"cap": permissions, "hub_version": "test",
                   "user_capabilities": set(permissions.CAPABILITIES),
                   "open_alert_count": 3, "is_superuser": True,
                   "latest_agent_version": "8.8.8",
                   # The rail shrinks this notice to a warning square, so render it.
                   "hub_update_available": True, "latest_hub_version": "9.9.9"}
        context.update(i18n.template_context("en"))
        return context

    return app


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def css(name):
    return read(STATIC, "css", name)


def test_one_content_width():
    print("\n-- every page gets the same width, with no per-page opt-in left behind --")
    base = css("base.css")

    widths = re.findall(r"\.app-shell__content[^{]*\{[^}]*?max-width:\s*(\d+px)", base, re.S)
    check(f"only one content width is defined (found {widths})", len(set(widths)) == 1)
    check("...and it is the wide one Packages used to keep to itself", widths == ["1600px"])
    # The modifier is what made Packages special. If it comes back, it comes back with a
    # template using it -- and the narrow-by-default look comes back with it.
    check("the per-page wide modifier is gone from the CSS",
          "app-shell__content--wide" not in base)

    templates = os.path.join(HUB, "templates")
    users = []
    for dirpath, _dirs, files in os.walk(templates):
        for name in files:
            if "app-shell__content--wide" in read(dirpath, name):
                users.append(name)
    check(f"...and no template still opts into it (found {users})", not users)


def test_collapsed_state_is_set_before_paint():
    print("\n-- the collapsed rail is applied by the head script, not after load --")
    body = build_app().test_client().get("/_shell").get_data(as_text=True)

    head = body.split("</head>", 1)[0]
    check("the head script reads the sidebar preference", "tempmonitor:sidebar" in head)
    check("...and sets the attribute the CSS keys off",
          re.search(r"setAttribute\(\s*'data-sidebar'\s*,\s*'collapsed'\s*\)", head) is not None)
    # If this ever moves into common.js the sidebar paints open and snaps shut on every
    # single page load, which is exactly the flash the inline script exists to prevent.
    check("...before any stylesheet or deferred script could paint",
          head.index("tempmonitor:sidebar") < head.index("css/base.css"))


def test_toggle_markup():
    print("\n-- the toggle is rendered and wired to the sidebar --")
    body = build_app().test_client().get("/_shell").get_data(as_text=True)

    check("the collapse button is rendered", 'id="nav-collapse"' in body)
    brand = body.split('class="sidebar__brand"', 1)[1].split("</div>", 1)[0]
    check("...inside the brand row, where the rail still has room for it",
          'id="nav-collapse"' in brand)
    check("...pointing at the sidebar", 'aria-controls="app-sidebar"' in body)
    check("...and starting expanded", 'aria-expanded="true"' in body)
    check("it is labelled from the catalogue, not hardcoded English",
          "Collapse navigation" in body)

    # Every nav label is wrapped rather than a bare text node: the rail hides the span, and
    # common.js reads it back for the tooltip. A link that loses its wrapper keeps its text
    # on a 64px rail and blows the column open.
    links = re.findall(r'<a class="sidebar__link.*?</a>', body, re.S)
    check(f"every nav link is rendered ({len(links)} of them)", len(links) >= 10)
    unwrapped = [l for l in links if "sidebar__link-label" not in l]
    check(f"...and every one wraps its label ({len(unwrapped)} bare)", not unwrapped)


def test_css_and_js_agree_on_the_attribute():
    print("\n-- the three files that share this state still spell it the same way --")
    base = css("base.css")
    components = css("components.css")
    tokens = css("tokens.css")
    js = read(STATIC, "js", "common.js")

    check("the grid narrows on the collapsed attribute", '[data-sidebar="collapsed"]' in base)
    check("...to the collapsed-width token", "--sidebar-width-collapsed" in base)
    check("...which is actually defined", "--sidebar-width-collapsed:" in tokens)

    check("the rail's own styling keys off the same attribute",
          '[data-sidebar="collapsed"]' in components)
    check("...hiding the link labels", ".sidebar__link-label" in components)
    check("...and styling the toggle", ".sidebar__collapse" in components)
    # The button is about spending width on the page; below the breakpoint there is no
    # width to spend and the drawer is the answer instead.
    check("the toggle is hidden on mobile, where the drawer takes over",
          re.search(r"@media \(max-width: 900px\) \{\s*\.sidebar__collapse \{ display: none; \}",
                    components) is not None)

    check("the initialiser is registered", "initSidebarCollapse()" in js)
    check("JS writes the attribute the CSS reads",
          "'data-sidebar'" in js and "'collapsed'" in js)
    check("JS and the head script agree on the storage key", "'tempmonitor:sidebar'" in js)
    check("the rail's tooltips come from the label element, not a second list",
          ".sidebar__link-label" in js)


def _desktop_only_blocks(text):
    """Byte ranges of every `@media (min-width: 901px)` block, by brace matching."""
    spans = []
    needle = "@media (min-width: 901px) {"
    at = text.find(needle)
    while at != -1:
        depth, i = 0, at + len(needle) - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        spans.append((at, i))
        at = text.find(needle, i)
    return spans


def test_rail_styling_is_desktop_only():
    """The collapsed preference is one attribute on <html> and it survives a resize, so
    every rule keyed off it has to say so out loud.

    This is a bug that was real, not hypothetical. Unscoped, the rail rules followed the
    operator onto their phone: the drawer slid out at its full 240px with every label
    display:none, a column of unlabelled icons in a space with room for the words. The grid
    rule was worse -- `:root[attr] .app-shell` outweighs the plain `.app-shell` inside the
    mobile query, so it reserved a 64px column for a sidebar that had gone position:fixed."""
    print("\n-- nothing keyed to the collapsed attribute leaks onto mobile --")
    for name in ("base.css", "components.css"):
        text = css(name)
        spans = _desktop_only_blocks(text)
        leaked = []
        at = text.find(':root[data-sidebar="collapsed"]')
        while at != -1:
            if not any(lo < at < hi for lo, hi in spans):
                leaked.append(text[at:text.find("{", at)].strip())
            at = text.find(':root[data-sidebar="collapsed"]', at + 1)
        check(f"{name}: every collapsed rule is inside a min-width query "
              f"({len(leaked)} loose)", not leaked)
        if leaked:
            for sel in leaked:
                print(f"       loose: {sel}")

    # The two breakpoints are complements of one another; drift and there is a 1px window
    # where the page has neither a rail nor a drawer.
    check("the desktop query complements the mobile one",
          "@media (min-width: 901px)" in css("components.css")
          and "@media (max-width: 900px)" in css("components.css"))


def test_locales_carry_both_labels():
    print("\n-- the button's two states are translated everywhere --")
    for lang in ("en", "de", "es"):
        with open(os.path.join(HUB, "locales", f"{lang}.json"), encoding="utf-8") as fh:
            nav = json.load(fh).get("nav", {})
        check(f"{lang}: nav.collapse", bool(nav.get("collapse")))
        check(f"{lang}: nav.expand", bool(nav.get("expand")))


if __name__ == "__main__":
    test_one_content_width()
    test_collapsed_state_is_set_before_paint()
    test_toggle_markup()
    test_css_and_js_agree_on_the_attribute()
    test_rail_styling_is_desktop_only()
    test_locales_carry_both_labels()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
