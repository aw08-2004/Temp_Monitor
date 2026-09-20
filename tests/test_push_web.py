"""HTTP-layer tests for push registration (roadmap #11 phase 2).

**The silent failure this file exists to catch is a push address registered against the
wrong device.** push_web.py's whole claim is that there is no request shape by which one
operator can do that -- the device is never named in a body, only read off the bearer token
-- and that claim is invisible from the outside. A regression that started trusting a
`token_id` in the body would keep every existing test green, keep the app working, and hand
whoever asked the ability to redirect somebody else's fleet notifications to their own
phone.

Like test_apitokens_web.py, this imports the REAL app.py rather than wiring the blueprint
onto a minimal Flask app, because the thing under test is the interaction between the
blueprint's own gate and `login_required`'s two ways in. A stub gate would assert nothing
about the one that matters.

The three assertions worth naming:

  * **A browser session is refused**, even one belonging to a full superuser. "This device"
    is not a question a console session can answer, and answering it wrongly is worse than
    refusing.
  * **Registration is bearer-only and therefore CSRF-exempt**, for the reason app.py's own
    note gives: CSRF rides an ambient credential and a bearer header is not one. The
    content-type requirement still applies, because a JSON body read from a caller who did
    not declare JSON silently arrives as None.
  * **`view` is the gate and it is a real one.** A device paired without it is refused,
    which keeps push from being a way to learn that alerts exist for machines the same
    device's `/api/alerts` call would not show it.
"""
import os
import sys
import tempfile

import console_session

_TMPDIR = tempfile.mkdtemp(prefix="hub-push-web-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import apitokens        # noqa: E402
import permissions      # noqa: E402
import push             # noqa: E402
import app as hub       # noqa: E402

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


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def pair(db_path, email, capabilities):
    """Mint a device token directly. The pairing flow itself is test_apitokens_web's job."""
    token, row = apitokens.mint_token(
        db_path, email=email, device_name="Phone", platform="android",
        capabilities=list(capabilities))
    return token, row["token_id"]


# ================================
def test_a_browser_session_cannot_register(client):
    print("\n== Push registration is for a device, never for a browser ==")
    console_session.sign_in(client, {"email": "tech@x.com", "name": "tech",
                                     "directory_groups": []})
    r = client.post("/api/push/register", json={"kind": "fcm", "token": "tok"})
    check("a signed-in operator's browser is refused", r.status_code == 403)
    check("...with a reason rather than a bare 403",
          "paired device" in (r.get_json() or {}).get("error", ""))

    r = client.delete("/api/push/register")
    check("unregistering from a browser is refused too", r.status_code == 403)
    # Sent as JSON so it clears app.py's CSRF content-type check and actually reaches the
    # blueprint's own gate -- a 415 here would pass the assertion while proving nothing.
    r = client.post("/api/push/seen", json={})
    check("...and so is acknowledging", r.status_code == 403)

    console_session.sign_in(client, {"email": "super@x.com", "name": "super",
                                     "directory_groups": []})
    r = client.post("/api/push/register", json={"kind": "fcm", "token": "tok"})
    check("a break-glass superuser's browser is refused on the same rule",
          r.status_code == 403)
    console_session.sign_out(client)


def test_a_device_registers_itself(client, db_path):
    print("\n== A paired device registers, re-registers and unregisters itself ==")
    token, token_id = pair(db_path, "tech@x.com", [permissions.VIEW])

    r = client.get("/api/push/status", headers=auth(token))
    check("status answers before anything is registered", r.status_code == 200)
    body = r.get_json()
    check("...saying this hub has no push credentials", body["configured"] is False)
    check("...and that this device holds no registration", body["registration"] is None)
    check("...while naming the kinds it would accept", body["kinds"] == list(push.PUSH_KINDS))

    r = client.post("/api/push/register", json={"kind": "fcm", "token": "fcm-abc"},
                    headers=auth(token))
    check("the device registers", r.status_code == 200 and r.get_json()["registered"])
    check("...and the hub reports it holds the address",
          push.registration(db_path, token_id) == {"kind": "fcm", "registered": True})
    check("...without the token appearing in any answer",
          "fcm-abc" not in r.get_data(as_text=True))

    r = client.get("/api/push/status", headers=auth(token))
    check("status now reports the registration",
          r.get_json()["registration"]["registered"] is True)

    r = client.post("/api/push/seen", headers=auth(token))
    check("the device can say its alert list was opened", r.status_code == 200)

    r = client.delete("/api/push/register", headers=auth(token))
    check("the device unregisters", r.status_code == 200)
    check("...and the hub stops holding an address for it",
          push.registration(db_path, token_id) is None)


def test_one_device_cannot_register_for_another(client, db_path):
    print("\n== The device is read off the token, never off the body ==")
    mine, mine_id = pair(db_path, "tech@x.com", [permissions.VIEW])
    theirs, theirs_id = pair(db_path, "viewer@x.com", [permissions.VIEW])

    r = client.post("/api/push/register",
                    json={"kind": "fcm", "token": "hijacked",
                          # Every spelling a body might use to name a device. None of them
                          # is read; this asserts that, rather than assuming it.
                          "token_id": theirs_id, "device": theirs_id,
                          "device_id": theirs_id, "email": "viewer@x.com"},
                    headers=auth(mine))
    check("the request succeeds -- the extra fields are simply not read",
          r.status_code == 200)
    check("...registering the CALLER's device", push.registration(db_path, mine_id)
          == {"kind": "fcm", "registered": True})
    check("...and leaving the named one untouched",
          push.registration(db_path, theirs_id) is None)


def test_the_gate_is_view_and_it_is_real(client, db_path):
    print("\n== A device without `view` is not pushed to, and cannot pretend otherwise ==")
    token, _ = pair(db_path, "tech@x.com", [permissions.ISSUE_COMMANDS])
    r = client.post("/api/push/register", json={"kind": "fcm", "token": "tok"},
                    headers=auth(token))
    check("a device holding only issue_commands is refused registration",
          r.status_code == 403)

    print("\n== A revoked device is refused, and told which of the two it is ==")
    live, live_id = pair(db_path, "tech@x.com", [permissions.VIEW])
    r = client.post("/api/push/register", json={"kind": "fcm", "token": "tok"},
                    headers=auth(live))
    check("...it registers while live", r.status_code == 200)
    apitokens.revoke_token(db_path, live_id, actor="root@example.com")
    r = client.post("/api/push/register", json={"kind": "fcm", "token": "tok"},
                    headers=auth(live))
    check("a revoked device's token no longer authenticates at all", r.status_code == 401)


def test_the_bearer_path_is_csrf_exempt_but_still_needs_json(client, db_path):
    print("\n== CSRF rides an ambient credential; a bearer header is not one ==")
    token, _ = pair(db_path, "tech@x.com", [permissions.VIEW])
    # No session, no CSRF token, no Referer -- exactly what the app sends.
    r = client.post("/api/push/register", json={"kind": "fcm", "token": "tok"},
                    headers=auth(token))
    check("a bearer POST with no CSRF token is accepted", r.status_code == 200)

    r = client.post("/api/push/register", data="kind=fcm&token=tok",
                    content_type="application/x-www-form-urlencoded",
                    headers=auth(token))
    check("...but a body that does not declare JSON is refused rather than read as None",
          r.status_code == 415)

    r = client.post("/api/push/register", json={"kind": "gcm", "token": "tok"},
                    headers=auth(token))
    check("an unknown push kind is a 400 naming the problem",
          r.status_code == 400 and "fcm" in r.get_json()["error"])

    # CodeQL's py/stack-trace-exposure, pinned: the refusal is a string push.py returns for
    # a caller to read, never the `str()` of an exception that also carries filesystem
    # paths when fcm_config is the one that raised.
    saved = os.environ.get("FCM_SERVICE_ACCOUNT")
    os.environ["FCM_SERVICE_ACCOUNT"] = "/etc/fleethub-secret-path/creds.json"
    try:
        r = client.post("/api/push/register", json={"kind": "gcm", "token": "tok"},
                        headers=auth(token))
        text = r.get_data(as_text=True)
        check("...and no refusal leaks a hub-side path", "fleethub-secret-path" not in text)
        r = client.get("/api/push/status", headers=auth(token))
        check("a broken FCM config leaves status answering, not 500",
              r.status_code == 200 and r.get_json()["configured"] is False)
        check("...without naming the path it could not read",
              "fleethub-secret-path" not in r.get_data(as_text=True))
    finally:
        os.environ.pop("FCM_SERVICE_ACCOUNT", None)
        if saved is not None:
            os.environ["FCM_SERVICE_ACCOUNT"] = saved


def main():
    db_path = hub.DB_PATH
    apitokens.init_apitokens_db(db_path)
    push.init_push_db(db_path)

    permissions.create_group(
        db_path, "Techs", capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
        scope_mode=permissions.SCOPE_ALL, members=["tech@x.com"])
    permissions.create_group(
        db_path, "Viewers", capabilities=[permissions.VIEW],
        scope_mode=permissions.SCOPE_ALL, members=["viewer@x.com"])
    permissions.invalidate()
    # Captured by reference at import, so adding here is what makes super@x.com break-glass.
    hub.ALLOWED_EMAILS.add("super@x.com")

    client = hub.app.test_client()
    test_a_browser_session_cannot_register(client)
    test_a_device_registers_itself(client, db_path)
    test_one_device_cannot_register_for_another(client, db_path)
    test_the_gate_is_view_and_it_is_real(client, db_path)
    test_the_bearer_path_is_csrf_exempt_but_still_needs_json(client, db_path)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
