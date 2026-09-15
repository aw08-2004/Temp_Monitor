"""The Devices page (hub 1.114.0): row actions, bulk selection, the deploy hand-off, export.

The silent failures this file exists to catch:

  * **A bulk bar that acts on the wrong machines.** Selection is held by name across refreshes;
    if the pruning of names that left the roster goes, "Wake selected" quietly includes a PC
    the list stopped showing. And bulk wake must go through the scope-applying fleet endpoint
    with an explicit list -- without the list it wakes the whole fleet.
  * **A deploy hand-off that never arrives.** inventory.js writes a sessionStorage key and
    packages.js reads it. Two string literals in two files, no import between them.
  * **A CSV that runs code.** Every exported field comes from the unauthenticated /api/report;
    a cell starting with "=" is a formula the helpdesk's spreadsheet runs on open.
  * **Controls drawn for operators who cannot use them.** The server re-checks, but a Wake or
    Deploy button that only ever 403s is a broken page.
  * **Tailwind utilities with no CSS, and `hidden` fighting `flex`.** The bulk bar, row menu,
    preset banner and toasts are utility-built; a stale app.css leaves them unstyled, and
    toggling the hidden ATTRIBUTE on a `flex` element does nothing at all.

Run from the repo root so `import app` resolves.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-devices-test-")
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
IFRAME = {"Sec-Fetch-Dest": "iframe"}


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
    """// comments removed -- the headers explain each rule by naming what it forbids."""
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


def client_for(email):
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = {"email": email}
    return c


def page(email, path="/inventory"):
    return client_for(email).get(path, headers=IFRAME).get_data(as_text=True)


def setup():
    permissions.init_permissions_db(app.DB_PATH)
    permissions.invalidate()
    permissions.create_group(
        app.DB_PATH, name="Plain viewers", capabilities=[permissions.VIEW],
        machines=None, members=["viewer@example.com"], actor="root@example.com")


def test_the_page_renders_what_its_script_needs():
    print("\n-- every id inventory.js looks up is in the page --")
    full = page("root@example.com")
    js = read(STATIC, "js", "inventory.js")
    wanted = sorted(set(re.findall(r"getElementById\('([a-z0-9-]+)'\)", js)))
    missing = [w for w in wanted if f'id="{w}"' not in full]
    check(f"all {len(wanted)} ids are rendered for a superuser (missing {missing})", not missing)
    check("the header checkbox is not a sort column",
          'id="inventory-select-all"' in full
          and not re.search(r'<th[^>]*data-sort[^>]*>\s*<input', full))
    check("the loading row spans every column (12)", 'colspan="12"' in full)
    check("the sidebar calls the page Devices", ">Devices<" in get_shell_sidebar())
    check("the url is still /inventory", 'data-nav-prefix="/inventory"' in get_shell_sidebar())


def get_shell_sidebar():
    return client_for("root@example.com").get("/", headers={"Sec-Fetch-Dest": "document"}).get_data(as_text=True)


def test_controls_follow_capabilities():
    print("\n-- wake and deploy controls are drawn only for operators who can use them --")
    plain = page("viewer@example.com")
    full = page("root@example.com")
    check("a plain viewer gets no bulk wake", 'id="inventory-bulk-wake"' not in plain)
    check("...no bulk deploy, and no hand-off link", 'id="inventory-bulk-deploy"' not in plain
          and 'id="inventory-deploy-link"' not in plain)
    check("...but can still export and clear", 'id="inventory-bulk-export"' in plain
          and 'id="inventory-bulk-clear"' in plain and 'id="inventory-export"' in plain)
    check("...and the page tells its script so", 'data-can-issue-commands="0"' in plain)
    check("a superuser gets both", 'id="inventory-bulk-wake"' in full and 'id="inventory-bulk-deploy"' in full)

    js = code_only(read(STATIC, "js", "inventory.js"))
    check("the scripts' null-guards cover the gated buttons",
          "if (bulkWakeBtn)" in js and "if (bulkDeployBtn && deployLink)" in js)
    check("the row menu offers Wake only to issue_commands, and only for an offline PC",
          "CAN_ISSUE && row.status !== 'online'" in js)
    check("...and Files only to issue_commands", "if (CAN_ISSUE) items.push(menuItem(t('tools.tab.files')" in js)


def test_bulk_actions_are_scoped_and_explicit():
    print("\n-- bulk wake names its machines and goes through the scoped endpoint --")
    js = code_only(read(STATIC, "js", "inventory.js"))
    bulk = js.split("const bulkWakeBtn", 1)[1].split("const bulkDeployBtn", 1)[0]
    check("bulk wake posts to /api/wake/fleet", "'/api/wake/fleet'" in bulk)
    check("...with the selection as its machines list", "machines: Array.from(selected)" in bulk)
    wake_web = read(ROOT, "hub", "wake_web.py")
    check("the endpoint narrows by that list AFTER applying scope",
          wake_web.find("access.in_scope(e[\"machine\"])") < wake_web.find("if isinstance(wanted, list)"))
    check("names that left the roster are dropped from the selection on refresh",
          "if (!present.has(machine)) selected.delete(machine)" in js)
    check("posts carry the JSON content type the hub requires",
          "'Content-Type': 'application/json'" in js)


def test_the_deploy_hand_off_joins_up():
    print("\n-- Devices and Packages agree on the hand-off --")
    inv = read(STATIC, "js", "inventory.js")
    pkg = read(STATIC, "js", "packages.js")
    key_inv = re.search(r"DEPLOY_PRESET_KEY = '([^']+)'", inv)
    key_pkg = re.search(r"DEPLOY_PRESET_KEY = '([^']+)'", pkg)
    check("both files name the same key",
          key_inv and key_pkg and key_inv.group(1) == key_pkg.group(1))
    check("it travels in sessionStorage, not the url",
          "sessionStorage.setItem(DEPLOY_PRESET_KEY" in inv and "sessionStorage.getItem(DEPLOY_PRESET_KEY" in pkg)
    check("the deploy dialog starts from it", "draftMachines = readDeployPreset();" in pkg)
    save = pkg.split("getElementById('deploy-save')", 1)[1].split("\n});", 1)[0]
    cancel = pkg.split("getElementById('deploy-cancel')", 1)[1].split("\n", 1)[0]
    check("the preset is cleared once a deployment is actually created",
          save.find("clearDeployPreset();") > save.find("await api('/api/deployments'"))
    check("...but not by cancelling the dialog", "clearDeployPreset" not in cancel)
    pkg_page = page("root@example.com", "/packages")
    for ident in ("deploy-preset", "deploy-preset-text", "deploy-preset-clear"):
        check(f"packages.html renders #{ident}", f'id="{ident}"' in pkg_page)
    check("the banner starts hidden by class, not attribute",
          re.search(r'id="deploy-preset"[^>]*class="[^"]*\bhidden\b', pkg_page) is not None)


def test_csv_cannot_carry_a_formula():
    print("\n-- the export neutralises formula-looking cells --")
    js = read(STATIC, "js", "inventory.js")
    cell = js.split("function csvCell", 1)[1].split("\n}", 1)[0]
    check("a leading = + - @ tab or CR gets an apostrophe", "/^[=+\\-@\\t\\r]/" in cell and "`'${text}`" in cell)
    check("every cell is quoted and quotes doubled", '.replace(/"/g, \'""\')' in cell)
    check("the file is written with a BOM for Excel", "'\\ufeff'" in js)
    check("the download link is left alone by the shell",
          "hasAttribute('download')" in read(STATIC, "js", "shell.js") and "a.download = " in js)


def test_toasts():
    print("\n-- the shared toast --")
    common = read(STATIC, "js", "common.js")
    fn = common.split("function toast(", 1)[1].split("\n}", 1)[0]
    check("toast() is a common.js global", "function toast(" in common)
    check("it announces politely to screen readers", "aria-live', 'polite'" in fn)
    check("errors are not auto-dismissed", "kind !== 'error' && timeout" in fn)
    check("messages are set as text", "textContent = message" in fn and "innerHTML" not in fn)


def test_utilities_and_visibility():
    print("\n-- utility-built parts have CSS, and toggle display by class --")
    built = read(STATIC, "css", "app.css")
    for utility in (r"bottom-4", r"z-\[70\]", r"min-w-44", r"bg-accent-soft", r"border-accent",
                    r"hover\:bg-control", r"pointer-events-none", r"sticky"):
        check(f"app.css defines {utility}", utility in built)
    js = code_only(read(STATIC, "js", "inventory.js"))
    check("the bulk bar is shown by swapping hidden/flex classes",
          "bulkBar.classList.toggle('hidden'" in js and "bulkBar.classList.toggle('flex'" in js)
    check("...never via the hidden attribute", "bulkBar.hidden" not in js and "rowMenu.hidden" not in js)
    check("no row cell is built from HTML", "innerHTML" not in js)


def main():
    setup()
    test_the_page_renders_what_its_script_needs()
    test_controls_follow_capabilities()
    test_bulk_actions_are_scoped_and_explicit()
    test_the_deploy_hand_off_joins_up()
    test_csv_cannot_carry_a_formula()
    test_toasts()
    test_utilities_and_visibility()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
