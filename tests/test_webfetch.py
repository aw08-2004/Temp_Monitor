"""webfetch.py and webfetch_web.py -- the hub reaching the internet so the assistant can build
packages (roadmap #26).

**The silent failures this file exists to catch:**

  * **The hub fetching an address it should never reach.** Every URL here comes from a model
    that just read a stranger's web page. A loopback, LAN, link-local (cloud metadata) or
    CGNAT address must be refused -- including one reached through a REDIRECT, one hidden
    behind an IPv4-mapped IPv6 address, and a name that resolves to a public and a private
    address at once.
  * **A connection to an address other than the one that was checked.** The point of pinning
    is that the IP judged public is the IP connected to; a refactor back to "hand the URL to
    requests" would reopen DNS rebinding and every check above would still pass.
  * **A staged file whose hash is not the bytes the store holds**, or a download over the
    size cap that leaves a partial file behind.
  * **A route reachable with the switch off, or without deploy_packages.**

No network: DNS and HTTP are replaced at webfetch's two seams, `_resolve` and `_request`.
"""
import functools
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "hub"))
import fleet
import packages
import permissions
import settings
import webfetch
from flask import Flask
from packages_web import create_packages_blueprint
from permissions_web import create_access
from webfetch_web import create_webfetch_blueprint

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


# ---------------------------------------------------------------- the fake internet
DNS = {
    "vendor.example": ["93.184.216.34"],
    "cdn.example": ["93.184.216.35"],
    "inside.example": ["10.1.2.3"],
    "mixed.example": ["93.184.216.36", "192.168.1.10"],
    "mapped.example": ["::ffff:10.0.0.1"],
    "cgnat.example": ["100.64.0.1"],
    "metadata.example": ["169.254.169.254"],
    "api.github.com": ["140.82.112.6"],
    "raw.githubusercontent.com": ["185.199.108.133"],
    # AAAA listed first, as getaddrinfo commonly does; its IPv4 address is unreachable here.
    "dual.example": ["2606:2800:220:1::1", "93.184.216.40"],
}
DEAD = {"93.184.216.40"}     # addresses whose connection fails
PAGES = {}           # (host, path) -> (status, headers, body)
REQUESTS = []        # (hostname, address, path) for every connection made


class FakeResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body
        self.released = False

    def stream(self, size):
        for i in range(0, len(self.body), max(1, size)):
            yield self.body[i:i + size]

    def release_conn(self):
        self.released = True


def fake_resolve(host, port):
    if host not in DNS:
        import socket
        raise socket.gaierror("no such host")
    return list(DNS[host])


def fake_request(parsed, address):
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    REQUESTS.append((parsed.hostname, address, path))
    if address in DEAD:
        raise webfetch.FetchError(f"could not reach {parsed.hostname}: ConnectTimeoutError")
    status, headers, body = PAGES.get((parsed.hostname, path), (404, {}, b""))
    return FakeResponse(status, headers, body)


webfetch._resolve = fake_resolve
webfetch._request = fake_request


def refused(url):
    try:
        webfetch.check_url(url)
    except webfetch.FetchError:
        return True
    return False


# ---------------------------------------------------------------- the address check
def test_addresses():
    print("\n-- only public https addresses are fetched --")
    check("a public https address is allowed", not refused("https://vendor.example/x"))
    check("plain http is refused", refused("http://vendor.example/x"))
    check("a user:password in the URL is refused", refused("https://u:p@vendor.example/"))
    check("a non-standard port is refused", refused("https://vendor.example:8443/"))
    check("a LAN name is refused", refused("https://inside.example/"))
    check("loopback is refused", refused("https://127.0.0.1/"))
    check("the cloud metadata address is refused", refused("https://169.254.169.254/latest"))
    check("...and a name that resolves to it", refused("https://metadata.example/"))
    check("CGNAT space is refused (not merely 'not private')", refused("https://cgnat.example/"))
    check("an IPv4-mapped LAN address is refused", refused("https://mapped.example/"))
    check("a name resolving to public AND private is refused", refused("https://mixed.example/"))
    check("an unresolvable name is refused", refused("https://nowhere.example/"))
    check("IPv6 loopback is refused", refused("https://[::1]/"))
    check("a file: URL is refused", refused("file:///C:/Windows/win.ini"))


def test_pinning_and_redirects():
    print("\n-- the checked address is the one connected to; every redirect is re-checked --")
    PAGES[("vendor.example", "/page")] = (200, {"Content-Type": "text/plain"}, b"hello")
    REQUESTS.clear()
    page = webfetch.fetch_text("https://vendor.example/page")
    check("a plain-text page is read", page["text"] == "hello")
    check("the connection went to the address that was checked",
          REQUESTS == [("vendor.example", "93.184.216.34", "/page")])

    PAGES[("dual.example", "/page")] = (200, {"Content-Type": "text/plain"}, b"dual")
    REQUESTS.clear()
    check("an unreachable address falls back to the next checked one",
          webfetch.fetch_text("https://dual.example/page")["text"] == "dual"
          and [a for _h, a, _p in REQUESTS] == ["93.184.216.40", "2606:2800:220:1::1"])

    PAGES[("vendor.example", "/go-inside")] = (302, {"Location": "https://inside.example/admin"},
                                               b"")
    REQUESTS.clear()
    try:
        webfetch.fetch_text("https://vendor.example/go-inside")
        blocked = False
    except webfetch.FetchError:
        blocked = True
    check("a redirect to a LAN address is refused", blocked)
    check("...without ever connecting to it",
          all(host != "inside.example" for host, _a, _p in REQUESTS))

    PAGES[("vendor.example", "/plain")] = (301, {"Location": "http://vendor.example/page"}, b"")
    check("a redirect down to plain http is refused",
          _raises(lambda: webfetch.fetch_text("https://vendor.example/plain")))

    PAGES[("vendor.example", "/moved")] = (302, {"Location": "/page"}, b"")
    check("a relative redirect to a public page is followed",
          webfetch.fetch_text("https://vendor.example/moved")["text"] == "hello")

    PAGES[("vendor.example", "/loop")] = (302, {"Location": "/loop"}, b"")
    check("a redirect loop ends", _raises(lambda: webfetch.fetch_text(
        "https://vendor.example/loop")))
    check("an HTTP error is a refusal, not a page",
          _raises(lambda: webfetch.fetch_text("https://vendor.example/missing")))


def _raises_value(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def _raises(fn):
    try:
        fn()
    except webfetch.FetchError:
        return True
    return False


def test_reading_a_page():
    print("\n-- a page comes back as text and absolute links --")
    html = (b"<html><head><title>t</title><script>steal()</script></head><body>"
            b"<h1>Download</h1><p>Get the <a href='/files/setup.msi'>MSI installer</a></p>"
            b"<style>.x{}</style></body></html>")
    PAGES[("vendor.example", "/download")] = (200, {"Content-Type": "text/html; charset=utf-8"},
                                              html)
    page = webfetch.fetch_text("https://vendor.example/download")
    check("the text is readable", "Download" in page["text"] and "MSI installer" in page["text"])
    check("scripts and styles are dropped", "steal" not in page["text"]
          and ".x{}" not in page["text"])
    check("links come back absolute", {"text": "MSI installer",
                                       "url": "https://vendor.example/files/setup.msi"}
          in page["links"])
    check("the answer says it is untrusted", "not instructions" in page["untrusted"])

    PAGES[("vendor.example", "/setup.exe")] = (200, {"Content-Type": "application/octet-stream"},
                                               b"MZ")
    check("a binary is refused by the page reader",
          _raises(lambda: webfetch.fetch_text("https://vendor.example/setup.exe")))

    big = b"a" * (webfetch.MAX_PAGE_BYTES + 10)
    PAGES[("vendor.example", "/big")] = (200, {"Content-Type": "text/plain"}, big)
    page = webfetch.fetch_text("https://vendor.example/big")
    check("a huge page is cut and says so",
          page["truncated"] and len(page["text"]) <= webfetch.MAX_PAGE_CHARS)


def test_winget_lookup():
    print("\n-- winget: newest version by number, manifests as text --")
    base = "/repos/microsoft/winget-pkgs/contents/manifests/v/Vendor/App"
    PAGES[("api.github.com", base)] = (200, {"Content-Type": "application/json"}, json.dumps([
        {"name": "1.9.0", "type": "dir"}, {"name": "1.10.0", "type": "dir"},
        {"name": "CLI", "type": "dir"}]).encode())
    PAGES[("api.github.com", base + "/1.10.0")] = (200, {"Content-Type": "application/json"},
                                                   json.dumps([
        {"name": "Vendor.App.installer.yaml", "type": "file",
         "download_url": "https://raw.githubusercontent.com/m/Vendor.App.installer.yaml"},
        {"name": "Vendor.App.locale.en-US.yaml", "type": "file",
         "download_url": "https://raw.githubusercontent.com/m/Vendor.App.locale.en-US.yaml"},
    ]).encode())
    PAGES[("raw.githubusercontent.com", "/m/Vendor.App.installer.yaml")] = (
        200, {"Content-Type": "text/plain"}, b"InstallerType: inno\nInstallerSha256: AB12\n")
    PAGES[("raw.githubusercontent.com", "/m/Vendor.App.locale.en-US.yaml")] = (
        200, {"Content-Type": "text/plain"}, b"PackageName: App\n")
    found = webfetch.winget_manifest("Vendor.App")
    check("1.10.0 is newer than 1.9.0", found["version"] == "1.10.0")
    check("the installer manifest comes back as text",
          "InstallerType: inno" in (found["installer_manifest"] or ""))
    check("a sub-package folder is listed as an id", "Vendor.App.CLI" in found["sub_package_ids"])
    check("an unknown version is refused",
          _raises(lambda: webfetch.winget_manifest("Vendor.App", "9.9")))
    check("an id with a path in it is refused",
          _raises(lambda: webfetch.winget_manifest("../../etc")))


# ---------------------------------------------------------------- staging
def test_staging(root, blob_dir):
    print("\n-- a download is staged, hashed, capped, promoted and pruned --")
    payload = b"MZ" + b"\x00" * 5000
    PAGES[("cdn.example", "/app-1.0.exe")] = (200, {"Content-Type": "application/octet-stream"},
                                              payload)
    PAGES[("vendor.example", "/latest")] = (302, {"Location": "https://cdn.example/app-1.0.exe"},
                                            b"")
    check("a LAN download is refused before any record exists",
          _raises(lambda: webfetch.begin_download(root, "https://inside.example/x", "a@x"))
          and webfetch.list_staged(root) == [])

    meta = webfetch.begin_download(root, "https://vendor.example/latest", "a@x")
    check("a new download starts queued", meta["status"] == "queued")
    check("...and a queued one is never judged stalled",
          webfetch._read_meta(root, meta["id"],
                              now=time.time() + webfetch.STALL_SECONDS + 5)["status"] == "queued")
    done = webfetch.run_download(root, meta["id"], 10 * 1024 * 1024)
    check("it finishes", done["status"] == "done")
    check("the sha256 is of the bytes received",
          done["sha256"] == hashlib.sha256(payload).hexdigest() and done["size"] == len(payload))
    check("the file is named from where it really came from",
          done["file_name"] == "app-1.0.exe" and done["final_url"].startswith("https://cdn."))
    check("the record reads back", webfetch.get_staged(root, meta["id"])["status"] == "done")

    capped = webfetch.begin_download(root, "https://cdn.example/app-1.0.exe", "a@x")
    result = webfetch.run_download(root, capped["id"], 1000)
    folder = os.path.join(root, capped["id"])
    check("a file over the cap fails", result["status"] == "failed" and "limit" in result["error"])
    check("...and leaves no partial file", sorted(os.listdir(folder)) == ["meta.json"])

    PAGES[("cdn.example", "/empty")] = (200, {}, b"")
    empty = webfetch.begin_download(root, "https://cdn.example/empty", "a@x")
    check("an empty file fails",
          webfetch.run_download(root, empty["id"], 1000)["status"] == "failed")

    stalled = webfetch.begin_download(root, "https://cdn.example/app-1.0.exe", "a@x")
    record = webfetch._read_meta(root, stalled["id"])
    webfetch._write_meta(root, stalled["id"], dict(record, status="downloading"))
    later = time.time() + webfetch.STALL_SECONDS + 5
    check("a download nobody is running any more reads as failed",
          webfetch._read_meta(root, stalled["id"], now=later)["status"] == "failed")

    orphan = webfetch.begin_download(root, "https://cdn.example/app-1.0.exe", "a@x")
    record = webfetch._read_meta(root, orphan["id"])
    webfetch._write_meta(root, orphan["id"], dict(record, process="a-previous-hub"))
    after = webfetch.get_staged(root, orphan["id"])
    check("a queued record left by a previous hub process reads as failed, not queued",
          after["status"] == "failed" and "restarted" in after["error"])
    check("...and no worker picks it up", webfetch.run_download(
        root, orphan["id"], 10 ** 9)["status"] == "failed"
          and not os.path.exists(os.path.join(root, orphan["id"], "file")))

    gone = webfetch.begin_download(root, "https://cdn.example/app-1.0.exe", "a@x")
    webfetch.discard(root, gone["id"])
    check("a download discarded while queued is skipped by its worker",
          webfetch.run_download(root, gone["id"], 10 ** 9) is None)

    running = webfetch.begin_download(root, "https://cdn.example/app-1.0.exe", "a@x")

    class DiscardMidway(FakeResponse):
        """Discards its own download after the first chunk, as an operator would mid-run."""
        def stream(self, size):
            yield b"MZ" * 100
            check("discarding a running download asks its worker to stop",
                  webfetch.discard(root, running["id"]) == "cancelling")
            check("...and it is gone from the API at once",
                  webfetch.get_staged(root, running["id"]) is None)
            yield b"Z" * 100

    real_request = webfetch._request
    webfetch._request = lambda parsed, address: DiscardMidway(200, {}, b"")
    try:
        webfetch.run_download(root, running["id"], 10 ** 9)
    finally:
        webfetch._request = real_request
    check("...and the worker removes the folder once its file is closed, so it cannot reappear",
          not os.path.exists(os.path.join(root, running["id"])))
    finished = webfetch.begin_download(root, "https://cdn.example/app-1.0.exe", "a@x")
    webfetch.run_download(root, finished["id"], 10 ** 9)
    check("discarding a finished download deletes it outright",
          webfetch.discard(root, finished["id"]) == "deleted"
          and not os.path.exists(os.path.join(root, finished["id"])))

    check("an unfinished download cannot be promoted",
          _raises(lambda: webfetch.promote(root, capped["id"], blob_dir, 10 ** 9)))
    os.replace(os.path.join(root, meta["id"], "file"),
               os.path.join(root, meta["id"], "file.promoting"))
    check("a promote that loses the race is refused with a sentence, not an OSError",
          _raises(lambda: webfetch.promote(root, meta["id"], blob_dir, 10 ** 9)))
    os.replace(os.path.join(root, meta["id"], "file.promoting"),
               os.path.join(root, meta["id"], "file"))
    check("a promote the store refuses puts the file back",
          _raises_value(lambda: webfetch.promote(root, meta["id"], blob_dir, 10))
          and os.path.exists(os.path.join(root, meta["id"], "file")))
    source = webfetch.promote(root, meta["id"], blob_dir, 10 ** 9)
    check("promoting puts it in the package store under its hash",
          os.path.exists(packages.blob_path(blob_dir, source["sha256"]))
          and source["sha256"] == done["sha256"] and source["kind"] == "upload")
    check("...removes the staged copy, and marks the record",
          not os.path.exists(os.path.join(root, meta["id"], "file"))
          and webfetch.get_staged(root, meta["id"])["status"] == "promoted")
    check("a staging id that is not one is unknown, not a path",
          webfetch.get_staged(root, "..\\..\\hub.db") is None)

    real_read = webfetch._read_meta
    webfetch._read_meta = lambda *a, **k: None          # every read loses the sharing race
    try:
        check("pruning never deletes a record it could not read right now",
              webfetch.prune_staging(root, 24, now=time.time() + 25 * 3600) == 0)
    finally:
        webfetch._read_meta = real_read
    before = len(webfetch.list_staged(root))
    check("pruning keeps recent records", webfetch.prune_staging(root, 24) == 0)
    removed = webfetch.prune_staging(root, 24, now=time.time() + 25 * 3600)
    check("pruning removes old ones", removed == before and webfetch.list_staged(root) == [])
    check("a file name cannot climb out of its folder",
          webfetch.safe_file_name("..\\..\\evil.exe") == "evil.exe"
          and webfetch.safe_file_name("") == "download.bin")


# ---------------------------------------------------------------- the routes
CURRENT_USER = "root@x.com"


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def test_routes(workdir):
    global CURRENT_USER
    print("\n-- the routes: the switch, the capability, the audit trail --")
    log_dir = os.path.join(workdir, "web-logs")
    os.makedirs(log_dir)
    db_path = os.path.join(log_dir, "hub.db")
    fleet.init_fleet_db(db_path)
    settings.init_settings_db(db_path)
    settings.invalidate()
    packages.init_packages_db(db_path)
    permissions.init_permissions_db(db_path)
    permissions.invalidate()
    permissions.create_group(db_path, name="Viewers", capabilities=[permissions.VIEW],
                             machines=[], members=["viewer@x.com"], actor="root@x.com")

    app = Flask(__name__)
    app.secret_key = "test"
    access = create_access(db_path, {"root@x.com"})
    app.register_blueprint(create_packages_blueprint(db_path, log_dir, fake_login_required,
                                                     access, hub_url="https://hub.example"))
    bp = create_webfetch_blueprint(db_path, log_dir, fake_login_required, access)
    app.register_blueprint(bp)

    @app.before_request
    def _seed_session():
        from flask import session
        session["user"] = {"email": CURRENT_USER}

    c = app.test_client()
    PAGES[("cdn.example", "/tool.msi")] = (200, {"Content-Type": "application/x-msi"},
                                           b"MSI" * 100)

    r = c.post("/api/webfetch/read", json={"url": "https://vendor.example/page"})
    check("off by default: 403", r.status_code == 403 and "Settings" in r.get_json()["error"])
    check("...and nothing was fetched while off",
          webfetch.list_staged(bp.staging_dir) == [])

    settings.set_many(db_path, {"ai.assistant_web_enabled": True}, "test")
    settings.invalidate()
    CURRENT_USER = "viewer@x.com"
    check("without deploy_packages: 403",
          c.post("/api/webfetch/downloads",
                 json={"url": "https://cdn.example/tool.msi"}).status_code == 403)
    CURRENT_USER = "root@x.com"

    r = c.post("/api/webfetch/read", json={"url": "https://vendor.example/page"})
    check("on: a page is read", r.status_code == 200 and r.get_json()["text"] == "hello")
    r = c.post("/api/webfetch/read", json={"url": "https://inside.example/"})
    check("a LAN address is a 400 with the reason",
          r.status_code == 400 and "public" in r.get_json()["error"])

    r = c.post("/api/webfetch/downloads", json={"url": "https://cdn.example/tool.msi"})
    check("starting a download answers 202 at once", r.status_code == 202)
    staging_id = r.get_json()["id"]
    r = c.get(f"/api/webfetch/downloads/{staging_id}?wait_seconds=10")
    check("waiting returns it done", r.status_code == 200 and r.get_json()["status"] == "done")
    check("it is listed", any(d["id"] == staging_id
                              for d in c.get("/api/webfetch/downloads").get_json()["downloads"]))

    r = c.post(f"/api/webfetch/downloads/{staging_id}/promote", json={})
    check("promote answers a package source", r.status_code == 200
          and r.get_json()["source"]["kind"] == "upload")
    source = r.get_json()["source"]
    r = c.post("/api/packages", json={
        "name": "Tool", "version": "1.0", "sources": [source],
        "install_command": "msiexec.exe", "install_args": "/i {file} /qn /norestart",
        "detection": {"kind": "installed_version", "name": "Tool"}})
    check("...which create_package accepts as-is", r.status_code == 201)
    package = r.get_json()
    check("...and the agent can download it from the store",
          os.path.exists(packages.blob_path(packages.blob_root(log_dir),
                                            package["sources"][0]["sha256"])))

    check("an unknown download is a 404",
          c.get("/api/webfetch/downloads/" + "0" * 32).status_code == 404)
    check("discarding works", c.delete(f"/api/webfetch/downloads/{staging_id}").status_code == 200
          and c.get(f"/api/webfetch/downloads/{staging_id}").status_code == 404)

    actions = {row["action"] for row in fleet.query_audit(db_path, limit=200)["entries"]} \
        if hasattr(fleet, "query_audit") else _audit_actions(db_path)
    check("reads, downloads and promotes are audited",
          {"webfetch.read", "webfetch.download", "webfetch.promote"} <= actions)
    bp.pool.shutdown(wait=True)


def _audit_actions(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT action FROM audit_log")}
    finally:
        conn.close()


def main():
    workdir = tempfile.mkdtemp(prefix="webfetch-tests-")
    try:
        test_addresses()
        test_pinning_and_redirects()
        test_reading_a_page()
        test_winget_lookup()
        test_staging(os.path.join(workdir, "staging"), os.path.join(workdir, "packages"))
        test_routes(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
