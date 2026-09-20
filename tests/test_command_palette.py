"""The sidebar sections and the Ctrl+K palette (hub 1.114.0).

The silent failures this file exists to catch:

  * **A section heading over nothing.** Sections render only when a link inside them will, and
    the has_* sets that decide that are a second copy of the links' own gates. Change a link's
    gate without its section's and an operator gets an "Operate" heading with no links under
    it -- or, worse, a link whose section never renders at all.
  * **A palette that enumerates more than its caller may see.** It must read the same scoped
    /api/machines as Inventory, and match on the same fields, or it becomes a second, looser
    way to list the fleet.
  * **A shortcut that eats a terminal's keystroke.** Ctrl+K is "kill to end of line" in most
    shells, and the remote viewer forwards every key to the PC. The guard for both is one line
    and nothing would notice it going.
  * **Utilities with no CSS behind them.** The palette is built from Tailwind classes; if
    app.css was not rebuilt, the dialog renders as an unstyled box at the top-left corner.

Run from the repo root.
"""
import os
import re
import sys

from flask import Blueprint, Flask, render_template_string

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))

import i18n  # noqa: E402
import permissions  # noqa: E402

PASS = 0
FAIL = 0

STATIC = os.path.join(ROOT, "hub", "static")
TEMPLATES = os.path.join(ROOT, "hub", "templates")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def code_only(js):
    """JS with its // line comments removed -- the module header explains its rules by naming
    the very things the checks below assert are absent."""
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


def _register_sidebar_stubs(app):
    """base.html includes the shared sidebar, which url_for()s every other page. Same helper
    as test_mobile_nav.py and test_audit_web.py."""
    for endpoint in ("index", "inventory_page", "alerts_page", "tools_page",
                     "remote_page", "settings_page", "permissions_page", "logout"):
        app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "", methods=["GET"])
    for name, endpoint in (("packages", "packages_page"), ("backups", "backups_page"),
                           ("invites", "invites_page"),
                           ("users", "users_page"), ("audit", "audit_page"),
                           ("bios", "firmware_page"), ("rules", "rules_page"),
                           ("patches", "patches_page"),
                           ("events", "events_page"),
                           ("apitokens", "download_page"),
                           ("sharing", "sharing_page"),
                           ("provisioning", "provisioning_page"),
                           ("location", "fleet_map_page"),
                           ("policy", "policy_page"),
                           ("device_groups", "device_groups_page"),
                           ("watchdogs", "watchdogs_page")):
        bp = Blueprint(name, __name__)
        bp.add_url_rule(f"/_stub/{name}", endpoint, lambda: "", methods=["GET"])
        app.register_blueprint(bp)


def render_with(capabilities, signed_in=True):
    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    app.secret_key = "test"
    _register_sidebar_stubs(app)

    @app.route("/_page")
    def page():
        return render_template_string('{% extends "base.html" %}{% block content %}hi{% endblock %}')

    @app.before_request
    def _seed_session():
        from flask import session
        if signed_in:
            session["user"] = {"email": "op@x.com"}

    @app.context_processor
    def _nav_context():
        context = {"cap": permissions, "hub_version": "test",
                   "user_capabilities": set(capabilities),
                   "open_alert_count": 0, "is_superuser": False,
                   "latest_agent_version": None}
        context.update(i18n.template_context("en"))
        return context

    return app.test_client().get("/_page").get_data(as_text=True)


def sections(body):
    """{section id: [link hrefs]} for every section actually rendered."""
    found = {}
    for chunk in body.split('class="sidebar__section"')[1:]:
        sid = re.search(r'aria-labelledby="(nav-section-[a-z]+)"', chunk)
        # A section ends where the next one (or the nav) does.
        chunk = chunk.split("</nav>", 1)[0]
        hrefs = re.findall(r'<a class="sidebar__link[^"]*"\s+data-nav-prefix="[^"]+"\s+href="([^"]+)"', chunk)
        if sid:
            found[sid.group(1)] = hrefs
    return found


# ------------------------------------------------------------------------ sections
def test_every_rendered_section_has_links():
    print("\n-- a section renders exactly when it has a link for you --")
    cases = (
        ("every capability", set(permissions.CAPABILITIES),
         {"nav-section-fleet", "nav-section-operate", "nav-section-admin"}),
        ("view only", {permissions.VIEW},
         {"nav-section-fleet", "nav-section-operate", "nav-section-admin"}),
        ("no capabilities", set(), {"nav-section-fleet"}),
        ("manage_users only", {permissions.MANAGE_USERS},
         {"nav-section-fleet", "nav-section-admin"}),
        ("remote_control only", {permissions.REMOTE_CONTROL},
         {"nav-section-fleet", "nav-section-operate"}),
        ("manage_firmware only", {permissions.MANAGE_FIRMWARE},
         {"nav-section-fleet", "nav-section-operate"}),
        ("view_audit_log only", {permissions.VIEW_AUDIT_LOG},
         {"nav-section-fleet", "nav-section-admin"}),
    )
    for label, caps, expected in cases:
        found = sections(render_with(caps))
        check(f"{label}: sections are {sorted(expected)} (got {sorted(found)})",
              set(found) == expected)
        empty = [sid for sid, hrefs in found.items() if not hrefs]
        check(f"{label}: no rendered section is empty ({empty})", not empty)

    body = render_with(set(permissions.CAPABILITIES))
    check("only one nav element is rendered", body.count("sidebar__nav") == 1)
    check("each section is a labelled group",
          body.count('role="group"') == 3 and body.count('class="sidebar__section-label"') == 3)
    check("section headings come from the catalogue",
          all(word in body for word in ("Fleet", "Operate", "Administration")))


def test_the_rail_keeps_the_grouping():
    print("\n-- the collapsed rail turns headings into hairlines, desktop only --")
    css = read(STATIC, "css", "components.css")
    query = css.split("@media (min-width: 901px)", 1)
    check("the collapsed rule for section labels exists",
          ':root[data-sidebar="collapsed"] .sidebar__section-label' in css)
    check("...inside the desktop-only query",
          len(query) == 2 and ':root[data-sidebar="collapsed"] .sidebar__section-label' in query[1])
    check("the active link no longer shifts its label with a border",
          "border-left-color: var(--accent)" not in css)


# ------------------------------------------------------------------------ palette
def test_the_palette_is_rendered_and_loaded():
    print("\n-- the palette's markup and script are where the topbar is --")
    body = render_with(set(permissions.CAPABILITIES))
    check("the topbar search trigger renders", 'id="palette-open"' in body)
    check("...announcing its shortcut", 'aria-keyshortcuts="Control+K Meta+K"' in body)
    check("the dialog renders", '<dialog id="palette"' in body)
    check("...outside the sticky header", body.find('<dialog id="palette"') > body.find("</header>"))
    check("the input is a combobox over the results",
          'role="combobox"' in body and 'aria-controls="palette-results"' in body)
    check("the script is loaded on a classic page", "js/command-palette.js" in body)

    logged_out = render_with(set(), signed_in=False)
    check("no palette without a session", 'id="palette"' not in logged_out
          and 'id="palette-open"' not in logged_out)

    base = read(TEMPLATES, "base.html")
    check("base.html loads it wherever there is a topbar", "shell_mode != 'framed'" in base
          and "js/command-palette.js" in base)
    # The script TAGS, not the names: base.html's comments mention static/js/shell.js above
    # either tag, and a bare find() would compare against the prose.
    check("...before shell.js, which calls into it on every frame load",
          base.find("filename='js/command-palette.js'") < base.find("filename='js/shell.js'"))
    check("shell.js hands each framed document to the palette",
          "FleetPalette.listen(doc)" in read(STATIC, "js", "shell.js"))

    js = read(STATIC, "js", "command-palette.js")
    wanted = set(re.findall(r"getElementById\('([a-z-]+)'\)", js))
    missing = sorted(w for w in wanted if w != "app-sidebar" and f'id="{w}"' not in body)
    check(f"every id the palette looks up is rendered (missing {missing})", not missing)


def test_the_palette_cannot_widen_what_you_see():
    print("\n-- the palette searches the scoped roster, on Inventory's fields --")
    js = read(STATIC, "js", "command-palette.js")
    check("devices come from the scoped roster", "fetch('/api/machines'" in js)
    check("pages come from the rendered sidebar, not a second list",
          "a.sidebar__link[href]" in js)

    def fields(source, name):
        m = re.search(name + r"\s*=\s*\[([^\]]*)\]", source)
        return re.findall(r"'([a-z_]+)'", m.group(1)) if m else None

    inventory = fields(read(STATIC, "js", "inventory.js"), "SEARCH_FIELDS")
    palette = fields(js, "SEARCH_FIELDS")
    check(f"the palette matches exactly Inventory's fields ({palette})",
          inventory is not None and palette == inventory)
    check("names are set as text, never HTML", "innerHTML" not in code_only(js))


def test_the_shortcut_is_not_stolen():
    print("\n-- Ctrl+K leaves a terminal and the remote viewer alone --")
    js = read(STATIC, "js", "command-palette.js")
    guard = js.split("function isShortcut", 1)[1].split("\n    }", 1)[0]
    check("a keystroke something already handled is left alone", "e.defaultPrevented" in guard)
    check("...and so is one typed into a terminal", "closest('.xterm')" in guard)
    check("the remote viewer still cancels the keys it forwards",
          "e.preventDefault()" in read(STATIC, "js", "remote.js"))
    check("Alt and Shift combinations are not the shortcut", "e.altKey" in guard and "e.shiftKey" in guard)


def test_the_utilities_exist_in_the_build():
    print("\n-- every Tailwind class the palette leans on has CSS behind it --")
    path = os.path.join(STATIC, "css", "app.css")
    check("app.css exists", os.path.isfile(path))
    built = read(STATIC, "css", "app.css") if os.path.isfile(path) else ""
    for utility in ("bg-accent-soft", "animate-pulse", r"backdrop\:bg-black", r"mt-\[12vh\]",
                    "size-2", "no-underline", "bg-control", "text-muted"):
        check(f"app.css defines {utility}", utility in built)
    tokens = read(STATIC, "css", "tokens.css")
    check("--accent-soft is defined for both themes", tokens.count("--accent-soft:") == 2)


def main():
    test_every_rendered_section_has_links()
    test_the_rail_keeps_the_grouping()
    test_the_palette_is_rendered_and_loaded()
    test_the_palette_cannot_widen_what_you_see()
    test_the_shortcut_is_not_stolen()
    test_the_utilities_exist_in_the_build()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
