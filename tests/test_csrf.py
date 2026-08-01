"""The CSRF gate in app.login_required.

A console session can run arbitrary code as SYSTEM on any enrolled machine, so a CSRF
against a signed-in operator is fleet-wide RCE. Two controls carry that: SameSite=Lax on
the session cookie, and a required JSON content type on every state-changing request.

Every blueprint's docstring has always claimed the second one, but until it was enforced
in login_required it was only INCIDENTALLY true -- bodies are read with
get_json(silent=True), which returns None on a wrong content type rather than refusing, so
the requirement held only for views that then failed over a missing field. The routes
below read no body at all, and for those the documented control did not exist. They are
the cases worth pinning: a `/cancel` or `/dismiss` that a cross-site form can fire is a
real one, and it is invisible in a code review of the route itself, because the control it
depends on is somewhere else.

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
with client.session_transaction() as sess:
    sess["user"] = {"email": "tester@example.com"}


def test_form_content_types_are_refused():
    """The three content types an HTML form can produce, on a route that reads no body.

    These are the only ones that reach us without a preflight, so they are the whole
    attack. A 415 here rather than a 404/200 is the difference between the gate running
    before the view and not running at all.
    """
    print("\n-- a cross-site form's content types are refused on a body-less POST --")
    aid = alerts.upsert_duplicate(app.DB_PATH, "SER-CSRF-1", ["m1", "m2"])
    url = f"/api/alerts/{aid}/dismiss"

    for ctype in ("application/x-www-form-urlencoded", "multipart/form-data",
                  "text/plain"):
        r = client.post(url, data="x=1", content_type=ctype)
        check(f"{ctype} -> 415", r.status_code == 415)
    check("no content type at all -> 415", client.post(url).status_code == 415)
    check("the alert is still open -- nothing was dismissed",
          alerts.get(app.DB_PATH, aid)["status"] == "open")

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
    type on those would break working callers to stop a request no browser sends."""
    print("\n-- GET and the preflighted methods are not gated --")
    check("GET /api/alerts is unaffected", client.get("/api/alerts").status_code == 200)
    # A DELETE with no body reaches its view; 404 is the view answering, not the gate.
    r = client.delete("/api/machines/no-such-machine")
    check("DELETE with no content type reaches the view",
          r.status_code in (200, 400, 403, 404))


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


def test_uploads_are_exempt_but_narrowly():
    """The two file-upload endpoints post multipart and cannot send JSON. They are
    exempted by ENDPOINT NAME rather than by allowing multipart everywhere, so a future
    route cannot inherit the exemption by accident."""
    print("\n-- the upload exemption is by endpoint name, not by content type --")
    check("exactly the two known uploads are exempt",
          app.CSRF_UPLOAD_ENDPOINTS
          == {"packages.upload_package_file", "bios.upload_firmware_image"})
    # Both are real, registered endpoints -- a typo here would silently un-exempt an
    # upload, which fails loudly, but a rename would silently exempt nothing at all.
    registered = set(app.app.view_functions)
    check("both exempt endpoints actually exist",
          app.CSRF_UPLOAD_ENDPOINTS <= registered)


if __name__ == "__main__":
    test_form_content_types_are_refused()
    test_charset_parameter_is_tolerated()
    test_reads_and_preflighted_methods_are_untouched()
    test_agent_endpoints_are_not_gated()
    test_uploads_are_exempt_but_narrowly()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
