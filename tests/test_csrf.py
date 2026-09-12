"""The CSRF gate in app.login_required.

A console session can run arbitrary code as SYSTEM on any enrolled machine, so a CSRF
against a signed-in operator is fleet-wide RCE. Three controls carry that: SameSite=Lax on
the session cookie, a per-session token echoed as `X-CSRF-Token`, and the older requirement
of a JSON content type.

**The two server-side layers cover different method sets, and this file exists to keep them
apart.** The token covers POST, PUT, PATCH and DELETE, because it costs a caller nothing --
common.js attaches it to everything that is not a read. The content type covers POST alone,
because a cross-site HTML form is the only state-changing request that arrives without a
preflight and a form can only issue GET or POST.

Merging the two sets is not extra caution; it is an outage. It happened once already: for
one commit both checks used one set, and the twenty-six bodyless `fetch(url, {method:
'DELETE'})` calls in hub/static/js -- delete a machine, delete a package, revoke an invite,
remove a firmware image -- all began answering 415, because the interceptor attaches a token
header and no content type. So the last test below sends a bodyless DELETE with a good token
and asserts it is NOT refused.

The content-type layer still earns its place for the reason it was added: bodies are read
with get_json(silent=True), which returns None on a wrong content type rather than refusing,
so before it was enforced the requirement held only for views that then failed over a missing
field. Around fifteen state-changing endpoints read no body at all, and for those the
documented control did not exist.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-csrf-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import alerts
import app
import console_session

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


client = app.app.test_client()
TOKEN = console_session.sign_in(client, "tester@example.com")

# A second client signed in the old way: a session cookie and no token. It stands in for
# every caller that is not the console -- a cross-site form, a stale tab, a script somebody
# wrote against the API with a copied cookie.
tokenless = app.app.test_client()
with tokenless.session_transaction() as sess:
    sess["user"] = {"email": "tester@example.com"}
    sess["csrf_token"] = TOKEN


def test_form_content_types_are_refused():
    """The three content types an HTML form can produce, on a route that reads no body.

    These are the only ones that reach us without a preflight, so they are the whole
    attack. A 415 here rather than a 404/200 is the difference between the gate running
    before the view and not running at all.
    """
    print("\n-- a cross-site form's content types are refused on a body-less POST --")
    aid = alerts.upsert_duplicate(app.DB_PATH, "SER-CSRF-1", ["m1", "m2"])
    url = f"/api/alerts/{aid}/dismiss"

    # WITH a valid token, so what is being measured here is the content-type layer alone.
    for ctype in ("application/x-www-form-urlencoded", "multipart/form-data",
                  "text/plain"):
        r = client.post(url, data="x=1", content_type=ctype)
        check(f"{ctype} -> 415", r.status_code == 415)
    check("no content type at all -> 415", client.post(url).status_code == 415)
    check("the alert is still open -- nothing was dismissed",
          alerts.get(app.DB_PATH, aid)["status"] == "open")

    # And without one, which is the layer that actually stops a cross-site request: the
    # token check runs first, so this is a 403 before the content type is ever considered.
    r = tokenless.post(url, json={})
    check("a signed-in caller with no token is refused (403)", r.status_code == 403)
    check("...and a wrong token is refused the same way",
          client.post(url, json={},
                      headers={"X-CSRF-Token": "0" * 64}).status_code == 403)
    check("the alert is STILL open", alerts.get(app.DB_PATH, aid)["status"] == "open")

    r = client.post(url, json={})
    check("the real console call still works", r.status_code == 200)
    check("...and it actually dismissed the alert",
          alerts.get(app.DB_PATH, aid)["status"] == "dismissed")


def test_charset_parameter_is_tolerated():
    """`application/json; charset=utf-8` is the same content type, and some clients send
    it. Matching on request.mimetype rather than the raw header is what keeps this from
    being a gate that refuses correct callers."""
    print("\n-- a charset parameter does not break a legitimate call --")
    aid = alerts.upsert_duplicate(app.DB_PATH, "SER-CSRF-2", ["m1", "m2"])
    r = client.post(f"/api/alerts/{aid}/dismiss", data="{}",
                    content_type="application/json; charset=utf-8")
    check("application/json; charset=utf-8 -> 200", r.status_code == 200)


def test_reads_and_preflighted_methods_are_untouched():
    """GET is not state-changing, and PUT/PATCH/DELETE cannot come from a form -- a
    cross-origin one has to use fetch, which preflights and fails. Requiring a content
    type on those would break working callers to stop a request no browser sends.

    **This is the regression guard, not a formality.** The console issues twenty-six bodyless
    DELETEs and several bodyless PUTs, none of which sets a content type, because common.js
    attaches a token header and nothing else. A 415 here means every one of those buttons is
    dead, and the symptom an operator reports is "Delete does nothing" -- which reads like a
    broken button rather than like a security control, so nobody looks here.
    """
    print("\n-- GET and the preflighted methods are not gated --")
    check("GET /api/alerts is unaffected", client.get("/api/alerts").status_code == 200)

    # The two method sets are the design. Pinned literally so re-merging them fails HERE,
    # with this comment attached, rather than in six unrelated modules at once.
    check("the token covers every state-changing method",
          app.CSRF_TOKEN_METHODS == {"POST", "PUT", "DELETE", "PATCH"})
    check("the content-type rule covers POST alone",
          app.CSRF_CHECKED_METHODS == {"POST"})

    # A DELETE with no body reaches its view; 404 is the view answering, not the gate.
    r = client.delete("/api/machines/no-such-machine")
    check("a bodyless DELETE with a token reaches the view, not a 415",
          r.status_code != 415 and r.status_code in (200, 400, 403, 404))
    check("...and without a token it is still refused, by the token check",
          tokenless.delete("/api/machines/no-such-machine").status_code == 403)


def test_agent_endpoints_are_not_gated():
    """/api/agent/* authenticates with a bearer token, which no browser attaches on its
    own -- there is no ambient credential to ride, so there is nothing to protect against
    and gating them would break every agent in the field. They do not pass through
    login_required, and this pins that they still do not."""
    print("\n-- agent-facing endpoints keep their own content types --")
    r = client.post("/api/agent/enroll", data="x=1",
                    content_type="application/x-www-form-urlencoded")
    check("enroll is refused on its own terms (403/400), not by the CSRF gate",
          r.status_code != 415)
    # The open telemetry ingress is likewise not behind login_required.
    r = client.post("/api/report", json={"machine": "CSRF-PC", "temp": 40.0})
    check("/api/report still accepts a normal agent report", r.status_code == 200)


def test_device_tokens_are_not_gated_either():
    """A device token (roadmap #11) is a bearer credential, so the rule that exempts
    /api/agent/* exempts it too -- for the same reason, not as a favour.

    Worth pinning HERE rather than only in test_apitokens_web.py: the two halves are one
    decision, and a change that relaxed the cookie path while "keeping" the token path
    would pass a test that only looked at tokens.
    """
    print("\n-- a bearer credential is not ambient, so the content-type rule does not apply --")
    import apitokens
    import permissions

    apitokens.init_apitokens_db(app.DB_PATH)
    token, _row = apitokens.mint_token(
        app.DB_PATH, "tester@example.com", "CSRF test device", "windows",
        [permissions.VIEW, permissions.ISSUE_COMMANDS])
    headers = {"Authorization": f"Bearer {token}"}

    # The same route, the same body, the same content type -- the ONLY difference is which
    # credential is presented.
    form = "machine=CSRF-PC"
    ctype = "application/x-www-form-urlencoded"
    cookie_resp = client.post("/api/fleet/commands", data=form, content_type=ctype)
    token_resp = client.post("/api/fleet/commands", data=form, content_type=ctype,
                             headers=headers)
    check("the cookie caller is refused (415, on the content type)",
          cookie_resp.status_code == 415)
    check("the bearer caller is not", token_resp.status_code != 415)

    check("a GET with a device token is authenticated at all",
          client.get("/api/machines", headers=headers).status_code == 200)


def test_uploads_are_exempt_but_narrowly():
    """The file-upload endpoints post multipart and cannot send JSON. They are exempted by
    ENDPOINT NAME rather than by allowing multipart everywhere, so a future route cannot
    inherit the exemption by accident.

    The set is pinned literally, and that is the point of this test: multipart is the one
    state-changing shape a cross-site HTML form can still produce, so every name added here
    is a route that stays reachable from any page on the internet with an operator's cookie
    attached. Three of the four are safe because they are INERT -- they store bytes and return
    an id, creating no package, no payload record, no deployment and no command -- and the JSON
    call that gives those bytes meaning is covered by the rule. A name appearing here without
    that property is the bug this assertion exists to catch, and the fourth is the one that
    does not have it, which is precisely why this assertion is written by hand."""
    print("\n-- the upload exemption is by endpoint name, not by content type --")
    check("exactly the known uploads are exempt",
          app.CSRF_UPLOAD_ENDPOINTS
          == {"packages.upload_package_file", "bios.upload_firmware_image",
              # The file explorer's upload. Inert in the same way: it spools the file and
              # answers with a transfer id, and POST .../files/push -- which takes JSON --
              # is what aims it at a folder and tells the machine to collect it.
              "files.upload_file_to_spool",
              # The provisioning APK, and it is NOT inert: it changes what the next device to
              # be provisioned installs as its device owner. It is here on a different
              # argument, spelled out beside the set in app.py -- a forged request needs a
              # logged-in operator holding `manage_settings` AND a validly signed APK whose
              # signing block parses, and the result is visible on the provisioning page,
              # which shows the hosted file, its digest and its checksum.
              "provisioning.upload_provisioning_apk"})
    # All are real, registered endpoints -- a typo here would silently un-exempt an
    # upload, which fails loudly, but a rename would silently exempt nothing at all.
    registered = set(app.app.view_functions)
    check("every exempt endpoint actually exists",
          app.CSRF_UPLOAD_ENDPOINTS <= registered)


if __name__ == "__main__":
    test_form_content_types_are_refused()
    test_charset_parameter_is_tolerated()
    test_reads_and_preflighted_methods_are_untouched()
    test_agent_endpoints_are_not_gated()
    test_device_tokens_are_not_gated_either()
    test_uploads_are_exempt_but_narrowly()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
