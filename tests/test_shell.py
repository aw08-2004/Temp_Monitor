"""The app shell: one url, three renderings, and the joins the frame navigation rests on.

WHAT THE SHELL IS FOR. A WebRTC session belongs to the document that negotiated it, so in a
plain multi-page app clicking any nav link kills every open remote screen. The shell keeps the
sidebar/topbar document alive and loads pages into a frame beneath it, so a screen survives a
trip to Packages. app.py's _shell_mode() picks the rendering from Sec-Fetch-Dest, which is
what lets the shell and the page it frames answer on the SAME url -- no ?frame=1, no hash
routing, every existing bookmark and url_for() untouched.

That last property is the one worth pinning hardest, because getting it wrong is not a visual
bug: serve the shell to the frame and the browser nests chrome inside chrome forever; serve
the bare page to a top-level request and the operator loses the sidebar entirely. Both are
decided by one header, so the three modes are asserted here against the real app.

The rest is the same kind of joins test as test_mobile_nav.py -- shell.js reaches into the
markup by a handful of ids, classes and data attributes, and there is no browser harness in
this repo to catch a rename. In particular data-nav-prefix is a hardcoded path sitting next to
a url_for(), which is exactly the pair that drifts, so every sidebar link is checked against
its own href.

Run from the repo root so `import app` resolves.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-shell-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "hub", "static")
TEMPLATES = os.path.join(ROOT, "hub", "templates")

DOCUMENT = {"Sec-Fetch-Dest": "document"}
IFRAME = {"Sec-Fetch-Dest": "iframe"}

client = app.app.test_client()
with client.session_transaction() as sess:
    sess["user"] = {"email": "tester@example.com"}


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


def get(path, headers=None):
    return client.get(path, headers=headers or {}).get_data(as_text=True)


# --------------------------------------------------------------------------- the three modes
def test_top_level_request_gets_the_shell():
    print("\n-- a document request is answered with the chrome and a frame --")
    body = get("/", DOCUMENT)

    check("the frame host is rendered", 'id="app-frames"' in body)
    check("...holding a frame", 'class="app-frames__frame"' in body)
    check("the sidebar is in the shell", 'id="app-sidebar"' in body)
    check("the topbar is in the shell", 'class="topbar"' in body)
    check("shell.js is loaded", "js/shell.js" in body)
    # The page's own content and scripts belong to the frame, not to the document around it.
    # Loading them here would run every page script twice, against markup that is not there.
    check("the page's content block is NOT rendered", "app-shell__content" not in body)
    check("the dashboard's own script is not loaded", "js/dashboard.js" not in body)
    check("autocomplete is not loaded around an empty frame", "js/autocomplete.js" not in body)


def test_frame_request_gets_the_bare_page():
    print("\n-- an iframe request is answered with the page and no chrome --")
    body = get("/", IFRAME)

    check("the content column is rendered", "app-shell__content" in body)
    check("the page's own script is loaded", "js/dashboard.js" in body)
    check("autocomplete is loaded for the page", "js/autocomplete.js" in body)
    check("the sidebar is NOT repeated inside the frame", 'id="app-sidebar"' not in body)
    check("the topbar is NOT repeated inside the frame", 'class="topbar"' not in body)
    check("the grid drops the column the sidebar would have used",
          "app-shell--bare" in body)
    check("shell.js is not loaded inside the frame", "js/shell.js" not in body)
    check("no frame is nested inside the frame", 'id="app-frames"' not in body)


def test_a_client_without_the_header_gets_the_whole_page():
    print("\n-- no Sec-Fetch-Dest: the page exactly as it rendered before the shell --")
    body = get("/")

    check("the sidebar is there", 'id="app-sidebar"' in body)
    check("the topbar is there", 'class="topbar"' in body)
    check("the content is there", "app-shell__content" in body)
    check("the page's own script is loaded", "js/dashboard.js" in body)
    check("no frame host at all", 'id="app-frames"' not in body)
    check("shell.js is not loaded", "js/shell.js" not in body)


def test_the_frame_opens_on_the_requested_url():
    """The whole no-redirect, no-hash-routing design rests on this: the shell for /packages
    frames /packages. A deep link with a query has to survive too -- /remote?machine=PC-2 is
    the documented way to open the Remote page with screens already on it."""
    print("\n-- the shell frames the very url that was asked for --")

    body = get("/inventory", DOCUMENT)
    check("a plain path is framed as-is", 'src="/inventory"' in body)
    # full_path would make this "/inventory?" and leave the shell and the frame disagreeing
    # about their own url on every page in the app.
    check("...with no stray question mark", 'src="/inventory?"' not in body)

    body = get("/remote?machine=PC-2", DOCUMENT)
    check("the query string survives into the frame",
          'src="/remote?machine=PC-2"' in body or 'src="/remote?machine=PC-2"' in body)


def test_pages_behind_a_capability_still_gate_in_every_mode():
    """The shell renders the chrome for whatever url was requested, and it must not become a
    way to see that a page exists. A caller who may not open /settings gets the same answer
    framed, unframed and as a document."""
    print("\n-- the modes do not widen what a caller can reach --")
    with client.session_transaction() as sess:
        sess["user"] = {"email": "nobody@example.com"}
    try:
        for label, headers in (("document", DOCUMENT), ("iframe", IFRAME), ("plain", {})):
            r = client.get("/settings", headers=headers)
            check(f"/settings as {label} is refused (got {r.status_code})",
                  r.status_code in (302, 403))
    finally:
        with client.session_transaction() as sess:
            sess["user"] = {"email": "tester@example.com"}


def test_framing_policy_headers():
    print("\n-- the shell may frame us; nobody else may --")
    r = client.get("/", headers=DOCUMENT)
    check("X-Frame-Options is SAMEORIGIN",
          r.headers.get("X-Frame-Options") == "SAMEORIGIN")
    check("CSP pins frame-ancestors to self",
          "frame-ancestors 'self'" in (r.headers.get("Content-Security-Policy") or ""))


# --------------------------------------------------------------------------- the joins
def test_sidebar_prefixes_match_their_own_links():
    """data-nav-prefix is what shell.js lights the active link from, because the server can
    only decide that once and the sidebar now outlives every page it navigates to. It is a
    hardcoded path next to a url_for(), so it is checked against the href it ships with."""
    print("\n-- every nav link's prefix actually matches its href --")
    body = get("/", DOCUMENT)
    links = re.findall(r'<a class="sidebar__link[^"]*"\s+data-nav-prefix="([^"]+)"\s+href="([^"]+)"',
                       body)
    check(f"every sidebar link carries a prefix (found {len(links)})", len(links) >= 10)
    for prefixes, href in links:
        parts = prefixes.split()
        matched = any(href == p or href.startswith(p.rstrip("/") + "/") for p in parts)
        check(f'{href} is covered by "{prefixes}"', matched)


def test_socket_pill_is_shared_by_the_shell():
    print("\n-- the live-data pill is rendered once in the chrome, claimed by the page --")
    shell = get("/", DOCUMENT)
    check("the shell renders the pill", 'id="socket-status"' in shell)
    check("...starting hidden, since no page has claimed it yet",
          re.search(r'id="socket-status"[^>]*hidden', shell) is not None)

    framed = get("/", IFRAME)
    check("the framed page does not render a second one", 'id="socket-status"' not in framed)

    plain = get("/", {})
    check("outside the shell the dashboard still renders its own",
          'id="socket-status"' in plain and "hidden" not in
          plain.split('id="socket-status"', 1)[1].split(">", 1)[0])
    check("...and a page with no socket still renders none",
          'id="socket-status"' not in get("/inventory", {}))

    js = read(STATIC, "js", "common.js")
    check("the page reaches the chrome through the frame boundary", "shellElement" in js)
    check("...and marks its own frame as the claimant",
          "window.frameElement" in js and "dataset.socket" in js)


def test_shell_js_and_markup_agree():
    print("\n-- shell.js still owns the hooks the markup hands it --")
    js = read(STATIC, "js", "shell.js")
    css = read(STATIC, "css", "base.css")

    check("the frame host id matches the template", "'app-frames'" in js)
    check("the frame class matches the template", "app-frames__frame" in js)
    check("the active-link class matches the sidebar", "sidebar__link--active" in js)
    check("the prefix attribute matches the sidebar", "navPrefix" in js)
    check("Remote is the page kept alive", "'/remote'" in js)
    # location.replace() is what keeps Back walking only the shell's own pushState entries.
    check("frames are moved with replace(), not src", "location.replace" in js)
    check("fullscreen is delegated to the frame", "allow" in js and "fullscreen" in js)

    check("the frame host is styled", ".app-frames" in css)
    # display:block on the frame would otherwise beat [hidden]'s UA rule and put every
    # background frame on screen at once.
    check("hidden frames are explicitly display:none",
          ".app-frames__frame[hidden]" in css)
    check("the framed page's grid loses the sidebar column", ".app-shell--bare" in css)

    common = read(STATIC, "js", "common.js")
    check("the theme toggle announces changes for the frames",
          "theme:change" in common and "theme:change" in js)


def test_login_breaks_out_of_the_frame():
    """A session that expires mid-session leaves the frame pointed at a redirect to /login.
    Google will not be framed, so the login page has to take the whole window."""
    print("\n-- the login page refuses to be framed --")
    login = read(TEMPLATES, "login.html")
    check("it breaks out to the top window",
          "window.top" in login and "window.top.location.replace" in login)


if __name__ == "__main__":
    test_top_level_request_gets_the_shell()
    test_frame_request_gets_the_bare_page()
    test_a_client_without_the_header_gets_the_whole_page()
    test_the_frame_opens_on_the_requested_url()
    test_pages_behind_a_capability_still_gate_in_every_mode()
    test_framing_policy_headers()
    test_sidebar_prefixes_match_their_own_links()
    test_socket_pill_is_shared_by_the_shell()
    test_shell_js_and_markup_agree()
    test_login_breaks_out_of_the_frame()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
