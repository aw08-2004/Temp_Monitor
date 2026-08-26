"""The Tools page: the joins the four tool panels hang off, and the two gates on it.

WHY THIS PAGE EXISTS. Terminal, Backup, Firmware and Network used to be tabs inside
/machine/<name>, which made every one of them a per-PC view: you picked the PC first, even
when the PC was not what you came for. They are the Tools page now -- tool first, machine
second, from a column beside the panels -- and the two standalone fleet pages (/firmware,
/backups) folded into the matching tabs, so each subject has one home instead of two a
navigation apart.

WHAT IS ACTUALLY AT RISK. Almost none of that is Python. The panels reach each other
through element ids, `data-fold-key` attributes and a `?tab=` slug vocabulary spread across
five JS files, three partials and one template, and there is no browser harness in this
repo -- so a rename ships green and breaks a console silently. This module is therefore
mostly a joins test, in the spirit of test_shell.py and test_mobile_nav.py: it asserts that
the ids the JS looks up exist in the rendered HTML and that the two halves of each tab are
gated the way they were before they shared a page.

The behaviour itself -- clicking a machine, a console re-attaching -- still needs eyes.

Run from the repo root so `import app` resolves.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-tools-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@example.com"

import app
import permissions

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "hub", "static")
TEMPLATES = os.path.join(ROOT, "hub", "templates")

DOCUMENT = {"Sec-Fetch-Dest": "document"}
IFRAME = {"Sec-Fetch-Dest": "iframe"}

# The four tabs, by the slug a link carries and the panel id it selects. This table IS the
# contract: machine.html links by slug, tabs.js resolves slug -> panel, and each JS module
# registers itself by panel id.
TABS = (("terminal", "tool-terminal"),
        ("backup", "tool-backup"),
        ("firmware", "tool-firmware"),
        ("network", "tool-network"))


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


def rules_only(css):
    """CSS with its comments removed.

    Every rule in this codebase is explained above itself, often by naming the selector it
    deliberately does NOT use -- so a check for "this selector is gone" run over the raw
    file finds it in the paragraph explaining why it went.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def client_for(email):
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = {"email": email}
    return c


def get(c, path, headers=None):
    return c.get(path, headers=headers or {}).get_data(as_text=True)


# --------------------------------------------------------------------- the page renders
def test_the_page_answers_in_every_shell_mode():
    """Same rule as every other page (see test_shell.py): the shell and the page it frames
    answer on one url, chosen from Sec-Fetch-Dest. Serve the shell to the frame and the
    browser nests chrome forever; serve the bare page to a top-level request and the
    operator loses the sidebar."""
    print("\n-- /tools in all three renderings --")
    c = client_for("root@example.com")

    body = get(c, "/tools", DOCUMENT)
    check("a document request gets the chrome", 'id="app-frames"' in body)
    check("...framing /tools itself", 'src="/tools"' in body)
    check("...and not the page's own script", "js/tools.js" not in body)

    body = get(c, "/tools", IFRAME)
    check("an iframe request gets the page", 'id="tools-root"' in body)
    check("...with its own script", "js/tools.js" in body)
    check("...and no nested frame", 'id="app-frames"' not in body)

    body = get(c, "/tools")
    check("no Sec-Fetch-Dest gets the whole page", 'id="tools-root"' in body
          and 'id="app-sidebar"' in body)


def test_a_deep_link_survives_into_the_frame():
    """machine.html links here as /tools?tab=firmware&machine=PC-1, and the Backups
    exceptions table as ?tab=backup&machine=X. Both halves have to reach the framed
    document, or the link lands on whatever tab the browser used last."""
    print("\n-- ?tab= and ?machine= reach the framed page --")
    c = client_for("root@example.com")
    body = get(c, "/tools?tab=firmware&machine=PC-2", DOCUMENT)
    check("the query survives into the frame src",
          'src="/tools?tab=firmware&amp;machine=PC-2"' in body
          or 'src="/tools?tab=firmware&machine=PC-2"' in body)

    # A fragment could not carry both, which is why this page opted into tabs.js's query
    # mode instead. Pinned so a revert to #hash shows up here.
    tools = read(TEMPLATES, "tools.html")
    check("the tablist asks tabs.js for the query-parameter mode",
          'data-tabs-param="tab"' in tools)
    check("tabs.js still implements that mode", "dataset.tabsParam" in read(STATIC, "js", "tabs.js"))


# --------------------------------------------------------------------------- the joins
def test_the_panel_ids_match_what_the_modules_register():
    """Each tool module finds its panel by a hardcoded id, and tools.html supplies it. The
    ids were deliberately renamed tab-* -> tool-* when the panels moved, precisely so that
    nothing could quietly keep binding to a machine-page id that no longer exists -- which
    only helps if the two sides are checked against each other."""
    print("\n-- every tool module's panel id exists in the page --")
    tools = read(TEMPLATES, "tools.html")
    for slug, panel_id in TABS:
        check(f"tools.html renders the {slug} panel", f'id="{panel_id}"' in tools)
        check(f"...and a tab whose slug is {slug}", f'data-tab-slug="{slug}"' in tools)

    for module, panel_id in (("fleet-terminal.js", "tool-terminal"),
                             ("backup-tab.js", "tool-backup"),
                             ("firmware.js", "tool-firmware"),
                             ("wake.js", "tool-network")):
        check(f"{module} registers {panel_id}",
              f"PANEL_ID = '{panel_id}'" in read(STATIC, "js", module))
    # fleet-pty.js shares fleet-terminal.js's panel rather than owning one.
    check("fleet-pty.js looks up the same terminal panel",
          "getElementById('tool-terminal')" in read(STATIC, "js", "fleet-pty.js"))

    # backup-tab.js draws into a container INSIDE its panel, because the panel also holds
    # the fleet policy and this half replaceChildren()s whatever it is given.
    check("backup-tab.js has a container of its own",
          "getElementById('backup-machine-pane')" in read(STATIC, "js", "backup-tab.js"))
    check("...and tools.html renders it", 'id="backup-machine-pane"' in tools)


def test_every_folded_section_can_remember_itself():
    """collapse.js keys storage off data-fold-key. A <details> without one still folds --
    it just forgets, silently, which is the kind of regression nobody files a bug for."""
    print("\n-- folds carry the key collapse.js stores them under --")
    js = read(STATIC, "js", "collapse.js")
    check("collapse.js namespaces its keys under the house prefix",
          "'tempmonitor:fold:'" in js)
    check("...and finds folds by the attribute", "details[data-fold-key]" in js)

    # Jinja comments talk ABOUT <details> at length, so they are stripped before counting;
    # otherwise the prose explaining the pattern fails the test that enforces it.
    def markup_only(text):
        return re.sub(r"\{#.*?#\}", "", text, flags=re.S)

    sources = {name: read(TEMPLATES, *name.split("/")) for name in (
        "machine.html",
        "partials/_tool_firmware.html", "partials/_tool_firmware_fleet.html",
        "partials/_tool_network.html", "partials/_tool_backup_fleet.html")}
    for name, raw in sources.items():
        text = markup_only(raw)
        opens = len(re.findall(r"<details\b", text))
        keyed = len(re.findall(r'data-fold-key="', text))
        check(f"every <details> in {name} carries a fold key ({keyed}/{opens})",
              opens == keyed and opens > 0)

    # The two that were folded before any of this, and must stay CLOSED by default: the
    # machine does no process sampling at all until one of them is opened.
    machine = markup_only(sources["machine.html"])
    for section in ("process-browser", "sensor-browser"):
        block = machine.split(f'id="{section}"', 1)[1].split(">", 1)[0]
        check(f"{section} still opens closed", "open" not in block)

    # backup-tab.js builds its folds at runtime, so it has to attach them itself.
    check("backup-tab.js attaches its runtime folds",
          "Collapse.attach(card)" in read(STATIC, "js", "backup-tab.js"))

    # Spacing sits on .fold itself, never on an adjacent-sibling pair. This shipped wrong
    # once: `.fold + .fold` measures DOM adjacency, and the per-machine half of a tab is
    # wrapped in .tool-machine-only so it can be hidden while the fleet half stays -- which
    # makes its first fold a :first-child that matches no sibling rule. The result was every
    # gap 24px except the one where the fleet half met the machine half, which was 0.
    # Comments stripped first: the rules below are explained at length in prose that names
    # the very selector it is asserting the absence of.
    css = rules_only(read(STATIC, "css", "components.css"))
    check("fold spacing is a property of the section, not of what precedes it",
          ".fold + .fold" not in css and ".card + .fold" not in css)
    check("...and every fold gets it",
          re.search(r"^\.fold \{[^}]*margin-top:", css, re.M | re.S) is not None)
    # Two rules setting one property is how they drift apart; .sensor-browser carries .fold.
    check("machine.css does not set that margin a second time",
          not re.search(r"^\.sensor-browser \{[^}]*margin-top:",
                        rules_only(read(STATIC, "css", "machine.css")), re.M | re.S))


def test_the_machine_page_hands_off_rather_than_duplicating():
    """The four panels left /machine/<name> entirely -- two entry points to one console is
    how two viewers end up open on one PC. What is left is links."""
    print("\n-- the machine page links to the tools instead of holding them --")
    machine = read(TEMPLATES, "machine.html")
    check("no tab strip is left on the machine page", 'role="tablist"' not in machine)
    for slug, panel_id in TABS:
        check(f"...but it links to the {slug} tab", f"?tab={slug}&amp;machine=" in machine)
    check("the terminal's markup went with it", "terminal-scrollback" not in machine)
    # The weight, not the word: the comment above the script block still explains where
    # xterm went, and should.
    check("...and so did xterm's script and stylesheet",
          "vendor/xterm" not in machine)


# ---------------------------------------------------------------------------- the gates
def test_the_two_halves_of_a_tab_keep_their_own_gates():
    """Firmware is the interesting one. Its per-machine half was NEVER capability-gated --
    what a PC's BIOS is set to is inventory, like its model -- while the image library
    behind it required manage_firmware as its own page. Sharing a tab must not quietly
    promote or demote either one."""
    print("\n-- capability gating survived the merge --")
    permissions.init_permissions_db(app.DB_PATH)
    permissions.invalidate()
    permissions.create_group(
        app.DB_PATH, name="Plain viewers", capabilities=[permissions.VIEW],
        machines=None, members=["viewer@example.com"], actor="root@example.com")

    plain = get(client_for("viewer@example.com"), "/tools", IFRAME)
    full = get(client_for("root@example.com"), "/tools", IFRAME)

    check("a plain viewer still gets the Firmware tab", 'data-tab-slug="firmware"' in plain)
    check("...and this machine's BIOS card", 'id="firmware-card"' in plain)
    check("...but not the fleet image library", 'id="images-body"' not in plain)
    check("...nor the flash dialog that queues an update", 'id="flash-modal"' not in plain)
    check("a manage_firmware holder gets the library", 'id="images-body"' in full)

    check("a plain viewer gets no Backup tab at all", 'data-tab-slug="backup"' not in plain)
    check("...nor the master-key dialog", 'id="key-modal"' not in plain)
    check("a manage_backups holder gets both halves",
          'data-tab-slug="backup"' in full and 'id="key-banner"' in full
          and 'id="backup-machine-pane"' in full)

    # Terminal and Network were ungated on the machine page and stay ungated here.
    check("Terminal is ungated", 'data-tab-slug="terminal"' in plain)
    check("Network is ungated", 'data-tab-slug="network"' in plain)


def test_the_absorbed_pages_still_answer_where_they_were():
    """/firmware and /backups are bookmarks, links in tickets and url_for() calls. They
    redirect rather than 404 -- and the gate still runs BEFORE the redirect, so whether
    the route exists cannot become a way to learn something."""
    print("\n-- the two absorbed pages redirect, and still refuse --")
    root = client_for("root@example.com")
    viewer = client_for("viewer@example.com")

    for path, slug in (("/firmware", "firmware"), ("/backups", "backup")):
        r = root.get(path)
        check(f"{path} redirects", r.status_code in (301, 302))
        check(f"...to the {slug} tab",
              r.headers.get("Location", "").endswith(f"/tools?tab={slug}"))
        check(f"{path} still refuses a caller without the capability",
              viewer.get(path).status_code == 403)

    check("their templates are gone",
          not os.path.exists(os.path.join(TEMPLATES, "firmware.html"))
          and not os.path.exists(os.path.join(TEMPLATES, "backups.html")))

    sidebar = read(TEMPLATES, "partials", "_sidebar.html")
    check("the sidebar no longer offers Firmware separately",
          'data-nav-prefix="/firmware"' not in sidebar)
    check("...nor Backups", 'data-nav-prefix="/backups"' not in sidebar)
    check("...and offers Tools instead", 'data-nav-prefix="/tools"' in sidebar)

    # The exceptions table used to send you to /machine/X#backup, which no longer holds a
    # backup view at all.
    check("the fleet exceptions table links into the Backup tab",
          "/tools?tab=backup&machine=" in read(STATIC, "js", "backups.js"))


def test_the_page_is_scope_filtered():
    """The machine column is fed by /api/machines, which access.filter_rows() has always
    scoped. Pinned here because the column is a NEW way to enumerate the fleet, and a
    picker that listed machines an operator may not touch would be a disclosure even if
    every click behind it 403'd."""
    print("\n-- the machine column cannot widen what a caller can see --")
    js = read(STATIC, "js", "tools.js")
    check("the column is built from the scoped roster", "'/api/machines'" in js)
    check("...and a ?machine= outside that list is dropped rather than used",
          "machine_unavailable" in js)


def test_favorites_do_not_wait_for_a_machine():
    """The favourites dialog must set itself up whether or not a machine is picked yet.

    fleet-favorites.js used to early-return on `!FleetApi.machine` at parse time, which was
    harmless while the terminal only ever lived at /machine/<name> (the machine was in the
    document). On the Tools page nothing is picked when the script runs, so the module
    returned, `window.FleetFavorites` was never defined, and the Favorites button on the
    terminal toolbar did nothing at all. Nothing behind the dialog is per-machine -- the
    four /api/fleet/favorites routes are keyed on the session's email -- so the guard was
    only ever a way to break the button."""
    print("\n-- the favourites dialog does not need a machine to exist --")
    js = read(STATIC, "js", "fleet-favorites.js")
    guard = [line for line in js.splitlines()
             if line.strip().startswith("if (!dialog")]
    check("the module still refuses to run without its <dialog>", len(guard) == 1)
    check("...but no longer gives up when no machine is chosen",
          guard and "FleetApi.machine" not in guard[0])
    check("the button that opens it is bound unconditionally",
          "FleetFavorites.open(" in read(STATIC, "js", "fleet-terminal.js"))


def test_the_files_tab_is_gated_and_wired():
    """The Files tab, and the reason it is the only per-machine panel here that is gated.

    Firmware and Network read as inventory behind `view`; a folder listing is not inventory
    -- those are the names of somebody's documents, and the same door reads the bytes in
    them. Every route behind this panel needs `issue_commands` (see files_web.py), so a tab
    rendered without it would open onto a wall of 403s.

    The second half of this is the joins check, and it earns its keep here more than
    anywhere else on the page: files.js looks up roughly thirty ids across two partials, and
    a rename of any one of them ships green and produces a button that silently does
    nothing. So every id the module asks for is asserted to exist in the rendered page.
    """
    print("\n-- the Files tab: one gate, and every id its module looks up --")
    plain = get(client_for("viewer@example.com"), "/tools", IFRAME)
    full = get(client_for("root@example.com"), "/tools", IFRAME)

    check("a plain viewer gets no Files tab at all", 'data-tab-slug="files"' not in plain)
    check("...nor its panel", 'id="tool-files"' not in plain)
    check("...nor its dialogs, one of which deletes things",
          'id="files-delete-dialog"' not in plain)
    check("...nor the module itself", "js/files.js" not in plain)
    check("an issue_commands holder gets the tab and the panel",
          'data-tab-slug="files"' in full and 'id="tool-files"' in full)

    js = read(STATIC, "js", "files.js")
    check("files.js registers the panel", "PANEL_ID = 'tool-files'" in js)
    # The panel is wholly machine-only: there is no fleet-wide half of "what is in this
    # folder", unlike the Firmware image library or the Backup destination list.
    panel = full.split('id="tool-files"', 1)[1].split('id="tool-', 1)[0]
    check("...and the whole of it waits for a machine to be picked",
          "tool-machine-only" in panel)

    wanted = sorted(set(re.findall(r"getElementById\('([a-z0-9-]+)'\)", js)))
    missing = [name for name in wanted if f'id="{name}"' not in full]
    check(f"every id files.js looks up is in the page ({len(wanted)} checked, "
          f"missing {missing[:5]})", not missing)

    # The version gate. An agent without these executors answers "unknown command type", so
    # without a named floor the panel would report a hard failure on every click -- which,
    # during a rollout, is every PC that has not self-updated yet.
    check("files.js names the agent version it needs, like processes.js does",
          "MIN_FILES_AGENT = '" in js)
    # Opening arrived a release later and is gated on its own number, so an agent that can
    # browse but not open loses one button rather than the whole panel.
    check("...and a second, later floor for opening things",
          "MIN_OPEN_AGENT = '" in js)

    # Opening is the one verb here that starts a process on somebody's PC, so the same gate
    # has to cover it and its dialogs -- including the radio group that chooses the account.
    check("a plain viewer gets no Open button", 'id="files-open"' not in plain)
    check("...nor the dialog that chooses the account", 'id="files-open-dialog"' not in plain)
    check("an issue_commands holder gets both",
          'id="files-open"' in full and 'id="files-open-dialog"' in full)
    check("...and the preview dialog the console renders files in",
          'id="files-preview-dialog"' in full)
    # Not found by the getElementById sweep above: these are read by name, as a radio group.
    check("both halves of the where/as-whom choice are on the page",
          'name="files-open-where"' in full and 'name="files-open-runas"' in full)
    # A blob URL carries this hub's origin, so a preview frame without sandbox would run an
    # untrusted file's script as the signed-in operator.
    check("the preview frame is sandboxed", "setAttribute('sandbox', '')" in js)


def main():
    test_the_page_answers_in_every_shell_mode()
    test_a_deep_link_survives_into_the_frame()
    test_the_panel_ids_match_what_the_modules_register()
    test_every_folded_section_can_remember_itself()
    test_the_machine_page_hands_off_rather_than_duplicating()
    test_the_two_halves_of_a_tab_keep_their_own_gates()
    test_the_absorbed_pages_still_answer_where_they_were()
    test_the_page_is_scope_filtered()
    test_favorites_do_not_wait_for_a_machine()
    test_the_files_tab_is_gated_and_wired()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
