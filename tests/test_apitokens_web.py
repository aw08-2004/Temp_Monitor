"""HTTP-layer tests for device pairing, token authentication and the Download Client
page (roadmap #11).

Unlike the other *_web tests, this one imports the REAL app.py rather than wiring a
blueprint onto a minimal Flask app. That is the point: the thing under test is `app.py`'s
`login_required` -- the single gate every console route passes through, now with two ways
in -- and a stub gate would assert nothing about it.

What is worth stating about the assertions:

  * **A device token is a CEILING, not a grant.** The effective capability set is the
    intersection with the owner's live permissions, so demoting an operator has to disable
    their paired devices immediately, and a token can never be used to do something its
    owner cannot.
  * **The CSRF content-type rule does not apply to bearer callers**, because CSRF rides an
    ambient credential and a bearer header is not one. The cookie path must still enforce
    it, so both halves are asserted together -- a change that relaxed the wrong one would
    otherwise pass.
  * **An agent token must not authenticate a console endpoint, and a user token must not
    authenticate an agent endpoint.** Two credential formats in one header is exactly the
    kind of thing that works by accident.
  * **The Download page renders only what a VERIFIED manifest declares.** An unsigned or
    wrongly-signed manifest must produce an error, never an empty list -- "no client has
    been published" and "this manifest is not trustworthy" are different situations.
"""
import functools
import json
import os
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="hub-apitokens-test-")
# app.py resolves LOG_DIR/DB_PATH from the cwd at import time -- declare a throwaway one
# before importing it, exactly as test_versions.py and test_alerts.py do.
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import apitokens          # noqa: E402
import clientrelease      # noqa: E402
import fleet              # noqa: E402
import permissions        # noqa: E402
import settings           # noqa: E402
import app as hub         # noqa: E402
from flask import Flask   # noqa: E402
from permissions_web import create_access  # noqa: E402
from apitokens_web import create_apitokens_blueprint  # noqa: E402

PASS = 0
FAIL = 0
CURRENT_USER = None


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def sign_in(client, email):
    """Put a session cookie on the client, the way _complete_login would."""
    with client.session_transaction() as sess:
        sess["user"] = {"email": email, "name": email, "directory_groups": []}


def sign_out(client):
    with client.session_transaction() as sess:
        sess.clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def pair(client, email, capabilities, redirect=None, device="Test device"):
    """Drive a whole pairing through the real endpoints and return the token."""
    sign_in(client, email)
    body = {"capabilities": capabilities, "device_name": device, "platform": "windows"}
    if redirect:
        body["redirect"] = redirect
    resp = client.post("/app/pair/confirm", json=body)
    if resp.status_code != 200:
        return resp, None
    code = resp.get_json()["code"]
    sign_out(client)
    exchanged = client.post("/api/tokens/exchange", json={"code": code})
    return exchanged, exchanged.get_json().get("token")


# ================================
# PAIRING + TOKEN AUTH
# ================================
def test_pairing_and_auth(client, db_path):
    print("\n== Pairing hands over a token, once ==")
    sign_in(client, "tech@x.com")
    page = client.get("/app/pair?name=Ward%20laptop&platform=windows"
                      "&redirect=http://127.0.0.1:53219/cb")
    check("the consent page renders", page.status_code == 200)
    body = page.get_data(as_text=True)
    check("...naming the device", "Ward laptop" in body)
    check("...and offering the capabilities the operator holds",
          'value="issue_commands"' in body)
    check("...but never an administrative one",
          'value="manage_settings"' not in body and 'value="manage_users"' not in body)

    resp = client.post("/app/pair/confirm", json={
        "capabilities": [permissions.VIEW, permissions.ISSUE_COMMANDS],
        "device_name": "Ward laptop", "platform": "windows",
        "redirect": "http://127.0.0.1:53219/cb", "state": "abc123",
    })
    check("confirming returns a code", resp.status_code == 200 and resp.get_json()["code"])
    redirect_url = resp.get_json()["redirect"]
    check("...and a loopback redirect carrying it",
          redirect_url.startswith("http://127.0.0.1:53219/cb?"))
    check("...with the app's own state echoed back", "state=abc123" in redirect_url)

    code = resp.get_json()["code"]
    sign_out(client)
    exchanged = client.post("/api/tokens/exchange", json={"code": code})
    check("the exchange needs no session at all", exchanged.status_code == 200)
    token = exchanged.get_json()["token"]
    check("...and returns the token exactly once", bool(token))

    again = client.post("/api/tokens/exchange", json={"code": code})
    check("a second exchange of the same code is refused", again.status_code == 400)

    print("\n== The token authenticates console endpoints ==")
    r = client.get("/api/machines", headers=auth(token))
    check("GET /api/machines with a token -> 200", r.status_code == 200)
    r = client.get("/api/machines")
    check("...and without one -> 401", r.status_code == 401)

    print("\n== A device is a ceiling, intersected with its owner's live permissions ==")
    # The owner holds ISSUE_COMMANDS; this device deliberately does not.
    view_resp, view_token = pair(client, "tech@x.com", [permissions.VIEW],
                                 device="Read-only phone")
    check("pairing a narrower device works", view_resp.status_code == 200)
    r = client.get("/api/permissions/me", headers=auth(view_token))
    caps = r.get_json()["capabilities"]
    check("the narrow device holds only view", caps == [permissions.VIEW])
    r = client.get("/api/permissions/me", headers=auth(token))
    check("...while the fuller device holds both",
          set(r.get_json()["capabilities"])
          == {permissions.VIEW, permissions.ISSUE_COMMANDS})

    print("\n== An operator cannot grant a device more than they hold ==")
    sign_in(client, "viewer@x.com")
    over = client.post("/app/pair/confirm", json={
        "capabilities": [permissions.VIEW, permissions.ISSUE_COMMANDS],
        "device_name": "Ambitious phone", "platform": "android"})
    check("over-granting is refused at pairing", over.status_code == 403)
    check("...naming what was refused", "issue_commands" in over.get_json()["error"])
    sign_out(client)

    print("\n== ...and cannot grant an administrative capability at all ==")
    sign_in(client, "super@x.com")           # a break-glass superuser: holds everything
    admin = client.post("/app/pair/confirm", json={
        "capabilities": [permissions.VIEW, permissions.MANAGE_PERMISSION_GROUPS],
        "device_name": "Admin laptop", "platform": "windows"})
    check("even a superuser cannot put manage_permission_groups on a device",
          admin.status_code == 400)
    sign_out(client)

    print("\n== Demoting the owner disables the device immediately ==")
    demoted_resp, demoted_token = pair(client, "temp@x.com", [permissions.VIEW],
                                       device="Contractor laptop")
    check("the contractor's device works while they are in a group",
          client.get("/api/machines", headers=auth(demoted_token)).status_code == 200)
    permissions.update_group(db_path, TEMP_GROUP_ID, members=[])
    settings.invalidate()
    permissions.invalidate()
    check("...and stops the moment they are removed from every group",
          client.get("/api/machines", headers=auth(demoted_token)).status_code == 401)

    print("\n== Revocation ==")
    device_id = json.loads(
        client.get("/api/tokens", headers=auth(view_token)).get_data())["devices"][0]["token_id"]
    gone = client.delete(f"/api/tokens/{device_id}", headers=auth(view_token))
    check("a device can revoke itself", gone.status_code == 200)
    check("...and stops working at once",
          client.get("/api/machines", headers=auth(view_token)).status_code == 401)

    print("\n== One operator cannot revoke another's device ==")
    sign_in(client, "viewer@x.com")
    mine = client.get("/api/tokens").get_json()["devices"]
    check("a viewer sees none of the tech's devices", mine == [])
    stranger = client.delete(f"/api/tokens/{_first_device_id(client, db_path)}")
    check("...and revoking one by id is a 404, not a 403 that confirms it exists",
          stranger.status_code == 404)
    sign_out(client)

    return token


def _first_device_id(client, db_path):
    return apitokens.list_tokens(db_path, email="tech@x.com")[0]["token_id"]


# ================================
# CSRF + CREDENTIAL SEPARATION
# ================================
def test_csrf_and_credential_separation(client, db_path, token):
    print("\n== The content-type rule applies to the ambient credential only ==")
    sign_in(client, "tech@x.com")
    formish = client.post("/api/fleet/commands", data="machine=PC-1",
                          content_type="application/x-www-form-urlencoded")
    check("a cookie POST without JSON is refused (415)", formish.status_code == 415)
    sign_out(client)

    # Same request, bearer credential. Not CSRF-able -- no browser attaches an
    # Authorization header on its own -- so it must reach the view and be judged on its
    # merits (here: a 400 for a nonsense body, which is the view answering).
    bearer = client.post("/api/fleet/commands", data="machine=PC-1",
                         content_type="application/x-www-form-urlencoded",
                         headers=auth(token))
    check("a bearer POST without JSON reaches the view", bearer.status_code != 415)

    print("\n== Agent and user credentials do not cross ==")
    agent_id, agent_token = fleet.enroll_agent(db_path, "PC-AGENT", SECRET, SECRET)
    r = client.get("/api/machines", headers={"Authorization": f"Bearer {agent_id}:{agent_token}"})
    check("an agent token is refused at a console endpoint", r.status_code == 401)

    r = client.post("/api/agent/heartbeat", json={"config_version": 0}, headers=auth(token))
    check("a user token is refused at an agent endpoint", r.status_code in (401, 403))

    print("\n== A token is never laundered into a session cookie ==")
    resp = client.get("/api/machines", headers=auth(token))
    check("a token request sets no session cookie",
          not any("session=" in v for _k, v in resp.headers if _k == "Set-Cookie"))


# ================================
# DOWNLOAD CLIENT
# ================================
def _ephemeral_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return priv, pub.hex()


def _write_manifest(code_dir, doc, priv=None):
    data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_path, sig_path = clientrelease.manifest_paths(code_dir)
    with open(manifest_path, "wb") as f:
        f.write(data)
    with open(sig_path, "w", encoding="utf-8") as f:
        f.write(priv.sign(data).hex() if priv else "00" * 64)
    return data


def test_download(db_path):
    print("\n== The Download page renders only a VERIFIED manifest ==")
    code_dir = tempfile.mkdtemp(prefix="hub-clientmanifest-")
    mini = Flask(__name__,
                 template_folder=os.path.join(
                     os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "hub", "templates"))
    mini.secret_key = "test"

    def fake_login_required(view):
        @functools.wraps(view)
        def wrapped(*a, **k):
            return view(*a, **k)
        return wrapped

    access = create_access(db_path, {"super@x.com"})
    mini.register_blueprint(create_apitokens_blueprint(
        db_path, fake_login_required, access, code_dir=code_dir))

    from flask import session as flask_session

    @mini.before_request
    def _seed():
        flask_session["user"] = {"email": "super@x.com", "directory_groups": []}

    c = mini.test_client()

    r = c.get("/api/app/manifest")
    check("no manifest -> 503, not an empty list", r.status_code == 503)
    check("...saying no release has been published",
          "published" in r.get_json()["error"].lower())
    check("the raw manifest 404s too", c.get("/download/manifest.json").status_code == 404)

    doc = {"version": "1.0.0", "builds": [
        {"platform": "windows", "arch": "x64", "kind": "file",
         "filename": "FleetHubSetup.zip", "size": 12345, "sha256": "ab" * 32,
         "url": "https://example.test/FleetHubSetup.zip"},
        {"platform": "ios", "kind": "link", "url": "https://testflight.example.test/x"},
    ]}

    _write_manifest(code_dir, doc)                      # signature is junk
    r = c.get("/api/app/manifest")
    check("an unsigned manifest -> 503", r.status_code == 503)
    check("...saying the signature is the problem, not the absence",
          "signature" in r.get_json()["error"].lower())

    priv, pub_hex = _ephemeral_key()
    real_key = clientrelease.RELEASE_PUBLIC_KEY_HEX
    try:
        clientrelease.RELEASE_PUBLIC_KEY_HEX = pub_hex
        data = _write_manifest(code_dir, doc, priv)
        r = c.get("/api/app/manifest")
        check("a correctly signed manifest -> 200", r.status_code == 200)
        body = r.get_json()
        check("...listing every build it declares", len(body["builds"]) == 2)
        check("...including the link-kind one",
              [b for b in body["builds"] if b["kind"] == "link"][0]["platform"] == "ios")
        check("...and the digest an operator can check",
              body["builds"][0]["sha256"] == "ab" * 32)

        check("the raw manifest serves the EXACT signed bytes",
              c.get("/download/manifest.json").get_data() == data)
        check("...and needs no session, so an unpaired client can check for updates",
              c.get("/download/manifest.json.sig").status_code == 200)

        # One byte changed after signing is the whole point of signing it.
        manifest_path, _ = clientrelease.manifest_paths(code_dir)
        with open(manifest_path, "wb") as f:
            f.write(data.replace(b"1.0.0", b"9.9.9"))
        r = c.get("/api/app/manifest")
        check("editing the manifest after signing invalidates it", r.status_code == 503)

        print("\n== A malformed manifest is refused, not half-rendered ==")
        for bad, why in (
            ({"version": "1.0.0", "builds": []}, "no builds"),
            ({"builds": [{"platform": "windows", "kind": "link",
                          "url": "https://x.test/a"}]}, "no version"),
            ({"version": "1.0.0", "builds": [
                {"platform": "windows", "kind": "file", "sha256": "nope",
                 "url": "https://x.test/a"}]}, "a file build with no valid digest"),
            ({"version": "1.0.0", "builds": [
                {"platform": "windows", "kind": "file", "sha256": "ab" * 32,
                 "url": "javascript:alert(1)"}]}, "a url that is not http(s)"),
        ):
            _write_manifest(code_dir, bad, priv)
            check(f"refused: {why}", c.get("/api/app/manifest").status_code == 503)
    finally:
        clientrelease.RELEASE_PUBLIC_KEY_HEX = real_key


SECRET = "hub-enroll-secret"
TEMP_GROUP_ID = None


def main():
    global TEMP_GROUP_ID
    db_path = hub.DB_PATH
    apitokens.init_apitokens_db(db_path)

    permissions.create_group(
        db_path, "Techs",
        capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
        scope_mode=permissions.SCOPE_ALL, members=["tech@x.com"])
    permissions.create_group(
        db_path, "Viewers", capabilities=[permissions.VIEW],
        scope_mode=permissions.SCOPE_ALL, members=["viewer@x.com"])
    TEMP_GROUP_ID = permissions.create_group(
        db_path, "Contractors", capabilities=[permissions.VIEW],
        scope_mode=permissions.SCOPE_ALL, members=["temp@x.com"])
    permissions.invalidate()

    # The break-glass list app.py built at import is captured by reference, so adding to it
    # here is what makes super@x.com a superuser for this run.
    hub.ALLOWED_EMAILS.add("super@x.com")

    hub.AGENT_ENROLLMENT_SECRET = SECRET
    client = hub.app.test_client()

    token = test_pairing_and_auth(client, db_path)
    test_csrf_and_credential_separation(client, db_path, token)
    test_download(db_path)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
