"""The device workspace and Fleet tasks: the joins the tool panels hang off, the redirects that
keep old links alive, and the gates on both pages.

WHY THIS SHAPE. Until hub 1.114.0 Terminal, Backup, Firmware, Network and Files were tabs on a
Tools page with the machine picked from a column beside them (hub 1.87.0). They are tabs on
/machine/<name> again, beside Overview, and the Tools url became Fleet tasks: only the fleet
halves of Backup and Firmware, which were never about one PC.

WHAT IS ACTUALLY AT RISK. Almost none of that is Python. The panels reach each other through
element ids, `data-fold-key` attributes and a `?tab=` slug vocabulary spread across a dozen JS
files, several partials and two templates, and there is no browser harness in this repo -- so
a rename ships green and breaks a console silently. The silent failures this module exists to
catch are:

  * a panel id a module registers that the page no longer renders (the tab stays empty),
  * an old /tools?tab=X&machine=Y link from a ticket that now lands on the wrong page,
  * a fleet half and a machine half that lost their separate capability gates in the move.

The behaviour itself -- switching machine, a console re-attaching -- still needs eyes.

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

MACHINE = "PC-1"

# The per-machine tabs, by the slug a link carries and the panel id it selects. This table IS
# the contract: old /tools links redirect by slug, tabs.js resolves slug -> panel, and each JS
# module registers itself by panel id.
TABS = (("terminal", "tool-terminal"),
        ("backup", "tool-backup"),
        ("firmware", "tool-firmware"),
        ("network", "tool-network"),
        ("files", "tool-files"))


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


def markup_only(text):
    """Jinja comments talk ABOUT the markup at length; strip them before counting it."""
    return re.sub(r"\{#.*?#\}", "", text, flags=re.S)


def client_for(email):
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = {"email": email}
    return c


def get(c, path, headers=None):
    return c.get(path, headers=headers or {}).get_data(as_text=True)


def machine_page(email):
    r = client_for(email).get(f"/machine/{MACHINE}", headers=IFRAME)
    return r.status_code, r.get_data(as_text=True)


def setup_viewer():
    permissions.init_permissions_db(app.DB_PATH)
    permissions.invalidate()
    permissions.create_group(
        app.DB_PATH, name="Plain viewers", capabilities=[permissions.VIEW],
        machines=[MACHINE], members=["viewer@example.com"], actor="root@example.com")


# --------------------------------------------------------------------- the redirects
def test_old_tool_links_land_on_the_machine_page():
    """/tools?tab=firmware&machine=PC-1 is in tickets, bookmarks and the old Backups
    exceptions table. It must land on that PC's page with the same tab -- not on Fleet tasks,
    which no longer holds anything per-PC."""
    print("\n-- per-PC /tools links redirect to the machine page --")
    c = client_for("root@example.com")
    for slug, _panel in TABS:
        r = c.get(f"/tools?tab={slug}&machine={MACHINE}")
        check(f"?tab={slug}&machine= redirects", r.status_code in (301, 302))
        check(f"...to /machine/{MACHINE}?tab={slug}",
              r.headers.get("Location", "").endswith(f"/machine/{MACHINE}?tab={slug}"))

    r = c.get("/tools?machine=PC%202&tab=nonsense")
    location = r.headers.get("Location", "")
    check("an unknown slug is dropped rather than passed on", "tab=" not in location)
    check("...and a machine name is re-quoted, not echoed raw", "/machine/PC%202" in location)

    for slug in ("terminal", "network", "files"):
        r = c.get(f"/tools?tab={slug}")
        check(f"a machine-less ?tab={slug} goes to Inventory",
              r.status_code in (301, 302)
              and r.headers.get("Location", "").endswith("/inventory"))


def test_the_absorbed_pages_still_answer_where_they_were():
    """/firmware and /backups are bookmarks, links in tickets and url_for() calls. They
    redirect rather than 404 -- and the gate still runs BEFORE the redirect, so whether the
    route exists cannot become a way to learn something."""
    print("\n-- the two absorbed pages redirect to Fleet tasks, and still refuse --")
    root = client_for("root@example.com")
    viewer = client_for("viewer@example.com")

    for path, slug in (("/firmware", "firmware"), ("/backups", "backup")):
        r = root.get(path)
        check(f"{path} redirects", r.status_code in (301, 302))
        check(f"...to the {slug} tab of Fleet tasks",
              r.headers.get("Location", "").endswith(f"/tools?tab={slug}"))
        check(f"{path} still refuses a caller without the capability",
              viewer.get(path).status_code == 403)
        # And the redirect target must not bounce again: a machine-less fleet slug renders.
        check(f"/tools?tab={slug} renders rather than redirecting on",
              root.get(f"/tools?tab={slug}").status_code == 200)

    check("their templates are gone",
          not os.path.exists(os.path.join(TEMPLATES, "firmware.html"))
          and not os.path.exists(os.path.join(TEMPLATES, "backups.html")))


# --------------------------------------------------------------------- fleet tasks
def test_fleet_tasks_answers_in_every_shell_mode():
    """Same rule as every other page (see test_shell.py): the shell and the page it frames
    answer on one url, chosen from Sec-Fetch-Dest."""
    print("\n-- /tools (Fleet tasks) in all three renderings --")
    c = client_for("root@example.com")

    body = get(c, "/tools", DOCUMENT)
    check("a document request gets the chrome", 'id="app-frames"' in body)
    check("...framing /tools itself", 'src="/tools"' in body)

    body = get(c, "/tools", IFRAME)
    check("an iframe request gets the page", 'id="fleet-tasks-root"' in body)
    check("...and no nested frame", 'id="app-frames"' not in body)

    body = get(c, "/tools")
    check("no Sec-Fetch-Dest gets the whole page", 'id="fleet-tasks-root"' in body
          and 'id="app-sidebar"' in body)


def test_fleet_tasks_holds_only_the_fleet_halves():
    """Each per-machine module on this page would boot against no machine and draw an
    error, and the old machine column is gone."""
    print("\n-- Fleet tasks carries the fleet halves and nothing per-PC --")
    full = get(client_for("root@example.com"), "/tools", IFRAME)
    check("the backup fleet half renders", 'id="key-banner"' in full and 'id="hub-pane"' in full)
    check("the firmware image library renders", 'id="images-body"' in full)
    for module in ("fleet-terminal.js", "backup-tab.js", "js/firmware.js", "wake.js",
                   "files.js", "tools.js"):
        check(f"...and {module} is not loaded", module not in full)
    check("no machine column is left", 'id="tools-machine-list"' not in full)
    check("the tablist keeps the query-parameter mode", 'data-tabs-param="tab"' in full)


def test_fleet_tasks_gates():
    print("\n-- Fleet tasks keeps each half behind its own capability --")
    plain = get(client_for("viewer@example.com"), "/tools", IFRAME)
    check("a plain viewer is told why the page is empty",
          'id="fleet-tasks-root"' in plain and 'id="tools-tabs"' not in plain)
    check("...and gets no image library", 'id="images-body"' not in plain)
    check("...nor the flash dialog that queues an update", 'id="flash-modal"' not in plain)
    check("...nor the master-key dialog", 'id="key-modal"' not in plain)

    sidebar = get(client_for("viewer@example.com"), "/", DOCUMENT)
    check("a plain viewer's sidebar has no Fleet tasks link", 'data-nav-prefix="/tools"' not in sidebar)
    sidebar = get(client_for("root@example.com"), "/", DOCUMENT)
    check("a superuser's does", 'data-nav-prefix="/tools"' in sidebar)
    raw = read(TEMPLATES, "partials", "_sidebar.html")
    check("the sidebar still offers no separate Firmware entry", 'data-nav-prefix="/firmware"' not in raw)
    check("...nor Backups", 'data-nav-prefix="/backups"' not in raw)


# ------------------------------------------------------------------ the machine page
def test_the_machine_page_holds_the_tools():
    """The tool panels live on /machine/<name> again. Each module finds its panel by a
    hardcoded id; the page must supply every one."""
    print("\n-- the machine page renders every tool panel its modules register --")
    status, full = machine_page("root@example.com")
    check("the machine page renders for a superuser", status == 200)

    raw = read(TEMPLATES, "machine.html")
    check("the tablist asks tabs.js for the query-parameter mode",
          'data-tabs-param="tab"' in raw)
    check("tabs.js still implements that mode", "dataset.tabsParam" in read(STATIC, "js", "tabs.js"))
    check("Overview is a tab of its own", 'data-tab-slug="overview"' in full
          and 'id="machine-overview"' in full)
    for slug, panel_id in TABS:
        check(f"the {slug} panel renders", f'id="{panel_id}"' in full)
        check(f"...with a tab whose slug is {slug}", f'data-tab-slug="{slug}"' in full)

    for module, panel_id in (("fleet-terminal.js", "tool-terminal"),
                             ("backup-tab.js", "tool-backup"),
                             ("firmware.js", "tool-firmware"),
                             ("wake.js", "tool-network"),
                             ("files.js", "tool-files")):
        check(f"{module} registers {panel_id}",
              f"PANEL_ID = '{panel_id}'" in read(STATIC, "js", module))
        check(f"...and the machine page loads it", f"js/{module}" in full)
    check("fleet-pty.js looks up the same terminal panel",
          "getElementById('tool-terminal')" in read(STATIC, "js", "fleet-pty.js"))
    check("backup-tab.js has a container of its own",
          "getElementById('backup-machine-pane')" in read(STATIC, "js", "backup-tab.js"))
    check("...and the machine page renders it", 'id="backup-machine-pane"' in full)

    # The fleet halves did NOT come back: backups.js runs at top level with no guard and
    # would throw on a page without its panes.
    check("the fleet backup script is not loaded here", "js/backups.js" not in full)
    check("...nor the image library", "firmware-images.js" not in full
          and 'id="images-body"' not in full)
    check("the old Tools deep links are gone", "/tools?tab=" not in markup_only(raw))

    # tool-panels and machine-context must precede every module that registers or asks.
    order = [full.find(f"js/{name}") for name in
             ("machine-context.js", "tool-panels.js", "fleet-terminal.js", "tabs.js",
              "machine-tools-gate.js", "machine-switcher.js", "collapse.js")]
    check("scripts load in dependency order", all(i >= 0 for i in order) and order == sorted(order))


def test_the_tabs_are_capability_gated_by_the_device():
    """machine-tools-gate.js hides anything with data-needs-command a device reported it
    cannot answer, and counts the hidden entries inside #machine-tools. The tabs moved inside
    that element so the explanatory sentence still fires."""
    print("\n-- device capability gating reaches the tabs --")
    raw = read(TEMPLATES, "machine.html")
    strip = raw.split('id="machine-tools"', 1)[1].split('id="machine-overview"', 1)[0]
    for command in ("run_script", "backup_files", "refresh_bios_inventory", "prepare_wake",
                    "list_directory"):
        check(f"a tab carries data-needs-command={command}",
              f'data-needs-command="{command}"' in strip)
    check("...inside the element the gate counts", 'id="machine-tools-note"' in strip)
    check("the gate still counts that element", "getElementById('machine-tools')"
          in read(STATIC, "js", "machine-tools-gate.js"))
    js = read(STATIC, "js", "machine-switcher.js")
    check("a linked tab the device cannot answer falls back to Overview",
          "tab-btn-overview" in js and "active.hidden" in js)


def test_the_two_halves_keep_their_own_gates():
    """Firmware's per-machine half was NEVER capability-gated -- what a PC's BIOS is set to
    is inventory, like its model -- while the image library required manage_firmware. Moving
    pages must not quietly promote or demote either one."""
    print("\n-- per-user capability gating on the machine page --")
    _status, plain = machine_page("viewer@example.com")
    _status, full = machine_page("root@example.com")

    check("a plain viewer still gets the Firmware tab", 'data-tab-slug="firmware"' in plain)
    check("...and this machine's BIOS card", 'id="firmware-card"' in plain)
    check("...and the BIOS password dialog it opens", 'id="firmware-password-dialog"' in plain)
    check("a plain viewer gets no Backup tab at all", 'data-tab-slug="backup"' not in plain)
    check("...nor its module", "backup-tab.js" not in plain)
    check("a manage_backups holder gets it", 'data-tab-slug="backup"' in full)
    check("Terminal is ungated", 'data-tab-slug="terminal"' in plain)
    check("Network is ungated", 'data-tab-slug="network"' in plain)
    check("a plain viewer gets no Remote link", 'id="machine-open-remote"' not in plain)
    check("a remote_control holder does", 'id="machine-open-remote"' in full)


def test_every_folded_section_can_remember_itself():
    """collapse.js keys storage off data-fold-key. A <details> without one still folds --
    it just forgets, silently, which is the kind of regression nobody files a bug for."""
    print("\n-- folds carry the key collapse.js stores them under --")
    js = read(STATIC, "js", "collapse.js")
    check("collapse.js namespaces its keys under the house prefix",
          "'tempmonitor:fold:'" in js)
    check("...and finds folds by the attribute", "details[data-fold-key]" in js)

    sources = {name: read(TEMPLATES, *name.split("/")) for name in (
        "partials/_tool_firmware.html", "partials/_tool_firmware_fleet.html",
        "partials/_tool_network.html", "partials/_tool_backup_fleet.html")}
    machine = markup_only(read(TEMPLATES, "machine.html"))
    # The switcher is a <details> for its disclosure semantics, not a fold: remembering it
    # open would pop a machine list over every machine page afterwards.
    machine_folds = machine.replace('<details class="machine-switcher"', "")
    sources_text = {name: markup_only(raw) for name, raw in sources.items()}
    sources_text["machine.html"] = machine_folds
    for name, text in sources_text.items():
        opens = len(re.findall(r"<details\b", text))
        keyed = len(re.findall(r'data-fold-key="', text))
        check(f"every fold in {name} carries a key ({keyed}/{opens})",
              opens == keyed and opens > 0)
    check("the switcher does NOT carry a fold key",
          'id="machine-switcher"' in machine
          and "data-fold-key" not in machine.split('<details class="machine-switcher"', 1)[1].split(">", 1)[0])

    # Closed by default: the machine does no process sampling until one of them is opened.
    for section in ("process-browser", "sensor-browser"):
        block = machine.split(f'id="{section}"', 1)[1].split(">", 1)[0]
        check(f"{section} still opens closed", "open" not in block)

    check("backup-tab.js attaches its runtime folds",
          "Collapse.attach(card)" in read(STATIC, "js", "backup-tab.js"))

    css = rules_only(read(STATIC, "css", "components.css"))
    check("fold spacing is a property of the section, not of what precedes it",
          ".fold + .fold" not in css and ".card + .fold" not in css)
    check("...and every fold gets it",
          re.search(r"^\.fold \{[^}]*margin-top:", css, re.M | re.S) is not None)
    check("machine.css does not set that margin a second time",
          not re.search(r"^\.sensor-browser \{[^}]*margin-top:",
                        rules_only(read(STATIC, "css", "machine.css")), re.M | re.S))


def test_the_history_charts_survive_a_hidden_overview():
    """Chart.js measures a canvas when it is built. A machine page opened as ?tab=terminal
    builds the charts inside a hidden Overview, and they stay zero pixels tall unless
    something re-measures them when Overview is first shown."""
    print("\n-- charts built behind another tab get re-measured --")
    js = read(STATIC, "js", "machine-switcher.js")
    check("the switcher re-sends the history card's toggle on Overview's tab:shown",
          "getElementById('history-card')" in js and "new Event('toggle')" in js)
    check("...which is the event machine.js resizes on",
          "historyCardEl.addEventListener('toggle'" in read(STATIC, "js", "machine.js"))


def test_the_switcher_is_scope_filtered_and_keeps_the_tab():
    """The switcher is a way to enumerate the fleet, so it must come from the scoped roster;
    and it exists to keep the twenty-PCs-in-a-row case cheap, which means keeping ?tab=."""
    print("\n-- the switcher: scoped roster, tab preserved, plain links --")
    js = read(STATIC, "js", "machine-switcher.js")
    check("built from the scoped roster", "'/api/machines'" in js)
    check("...only when first opened", "if (!roster) load()" in js)
    check("rows carry ?tab= forward",
          "URLSearchParams(location.search).get('tab')" in js and "?tab=" in js)
    check("...and are links shell.js can route", "createElement('a')" in js)
    # // comments stripped: the header explains the rule by naming the very property.
    check("names are set as text, never HTML",
          "innerHTML" not in re.sub(r"^\s*//.*$", "", js, flags=re.M))
    check("the backups exceptions table links into the machine's Backup tab",
          "/machine/${encodeURIComponent(m.machine)}?tab=backup" in read(STATIC, "js", "backups.js"))


def test_favorites_do_not_wait_for_a_machine():
    """The favourites dialog must set itself up whether or not a machine is known yet.
    Nothing behind the dialog is per-machine -- the /api/fleet/favorites routes are keyed on
    the session's email -- so a guard on the machine was only ever a way to break the button."""
    print("\n-- the favourites dialog does not need a machine to exist --")
    js = read(STATIC, "js", "fleet-favorites.js")
    guard = [line for line in js.splitlines()
             if line.strip().startswith("if (!dialog")]
    check("the module still refuses to run without its <dialog>", len(guard) == 1)
    check("...but does not give up when no machine is chosen",
          guard and "FleetApi.machine" not in guard[0])
    check("the button that opens it is bound unconditionally",
          "FleetFavorites.open(" in read(STATIC, "js", "fleet-terminal.js"))
    _status, full = machine_page("root@example.com")
    check("the machine page renders the favourites dialog", 'id="favorites-dialog"' in full)


def test_the_files_tab_is_gated_and_wired():
    """Files is the only per-machine tab gated on a user capability. Firmware and Network read
    as inventory behind `view`; a folder listing is not inventory -- those are the names of
    somebody's documents -- and every route behind the panel needs `issue_commands`.

    files.js looks up roughly thirty ids across two partials, and a rename of any one of them
    ships green and produces a button that silently does nothing, so every id it asks for is
    asserted to exist in the rendered page."""
    print("\n-- the Files tab: one gate, and every id its module looks up --")
    _status, plain = machine_page("viewer@example.com")
    _status, full = machine_page("root@example.com")

    check("a plain viewer gets no Files tab at all", 'data-tab-slug="files"' not in plain)
    check("...nor its panel", 'id="tool-files"' not in plain)
    check("...nor its dialogs, one of which deletes things",
          'id="files-delete-dialog"' not in plain)
    check("...nor the menu that every one of its verbs lives on",
          'id="files-menu"' not in plain)
    check("...nor the module itself", "js/files.js" not in plain)
    check("an issue_commands holder gets the tab and the panel",
          'data-tab-slug="files"' in full and 'id="tool-files"' in full)

    js = read(STATIC, "js", "files.js")
    check("files.js registers the panel", "PANEL_ID = 'tool-files'" in js)

    wanted = sorted(set(re.findall(r"getElementById\('([a-z0-9-]+)'\)", js)))
    missing = [name for name in wanted if f'id="{name}"' not in full]
    check(f"every id files.js looks up is in the page ({len(wanted)} checked, "
          f"missing {missing[:5]})", not missing)

    check("files.js names the agent version it needs", "MIN_FILES_AGENT = '" in js)
    check("...and a second, later floor for launching things on the machine",
          "MIN_OPEN_AGENT = '" in js)
    check("an issue_commands holder gets the menu and the account dialog",
          'id="files-menu"' in full and 'id="files-open-dialog"' in full)
    check("...including the panel's one irreversible verb", 'id="files-menu-delete"' in full)
    check("...and the preview dialog", 'id="files-preview-dialog"' in full)
    check("the account choice survives, and the where-choice does not",
          'name="files-open-runas"' in full and 'name="files-open-where"' not in full)
    check("the preview frame is sandboxed", "setAttribute('sandbox', '')" in js)


def main():
    setup_viewer()
    test_old_tool_links_land_on_the_machine_page()
    test_the_absorbed_pages_still_answer_where_they_were()
    test_fleet_tasks_answers_in_every_shell_mode()
    test_fleet_tasks_holds_only_the_fleet_halves()
    test_fleet_tasks_gates()
    test_the_machine_page_holds_the_tools()
    test_the_tabs_are_capability_gated_by_the_device()
    test_the_two_halves_keep_their_own_gates()
    test_every_folded_section_can_remember_itself()
    test_the_history_charts_survive_a_hidden_overview()
    test_the_switcher_is_scope_filtered_and_keeps_the_tab()
    test_favorites_do_not_wait_for_a_machine()
    test_the_files_tab_is_gated_and_wired()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
