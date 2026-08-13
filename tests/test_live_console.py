"""The console half of the live-telemetry watch: `POST /api/machines/<machine>/live/watch`.

This one endpoint is what multiplies a machine's telemetry by twelve, so what it is gated on
matters more than what it returns:

  * **`view` + machine scope**, the same gate that renders the charts it speeds up. An
    operator who cannot see a machine must not be able to make it report faster -- that is a
    machine they have no business touching at all, and "just a cadence" is still an
    instruction to a PC they were not given.

  * **It has a side effect and reads no body**, which is exactly the shape app.login_required's
    CSRF gate exists for (see test_csrf.py). A cross-site form must not be able to fire it.

  * **The cadence numbers are served, not hardcoded in the browser**, so the ping rate and
    the TTL it has to beat stay one decision.

Runs against the real app (like test_csrf.py) rather than a hand-wired blueprint, because
the decorators ARE what is being tested here.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-live-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "super@example.com"

import app
import live
import permissions
import settings

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


def sign_in(email):
    with client.session_transaction() as sess:
        sess["user"] = {"email": email}


JSON = {"Content-Type": "application/json"}


def main():
    permissions.create_group(
        app.DB_PATH, "Techs", capabilities=[permissions.VIEW],
        machines=["PC-1"], members=["tech@example.com"])
    settings.invalidate()

    print("\n-- opening the page is what turns the fast cadence on --")
    sign_in("super@example.com")
    check("nobody is watching to start with", live.is_watched(app.DB_PATH, "PC-1") is False)
    r = client.post("/api/machines/PC-1/live/watch", data="{}", headers=JSON)
    check("POST -> 200", r.status_code == 200)
    check("...and the machine is now watched", live.is_watched(app.DB_PATH, "PC-1"))
    body = r.get_json()
    check("the cadence is served, not hardcoded in the browser",
          body["poll_interval"] == live.POLL_INTERVAL_SECONDS
          and body["watch_ttl"] == live.WATCH_TTL_SECONDS
          and body["interval_seconds"] == live.FAST_INTERVAL_SECONDS)

    print("\n-- an operator can only do this to machines they can already see --")
    sign_in("tech@example.com")
    check("a scoped operator may speed up their own machine",
          client.post("/api/machines/PC-1/live/watch", data="{}", headers=JSON).status_code == 200)
    r = client.post("/api/machines/PC-2/live/watch", data="{}", headers=JSON)
    check("...and not one outside their scope -> 403", r.status_code == 403)
    check("...which left no watch behind either",
          live.is_watched(app.DB_PATH, "PC-2") is False)

    print("\n-- a cross-site form cannot fire it --")
    sign_in("super@example.com")
    live.clear_watch(app.DB_PATH, "PC-3")
    for ctype in ("application/x-www-form-urlencoded", "multipart/form-data",
                  "text/plain"):
        r = client.post("/api/machines/PC-3/live/watch", data="machine=PC-3",
                        content_type=ctype)
        check(f"{ctype} -> 415", r.status_code == 415)
    check("...and none of them registered a watch",
          live.is_watched(app.DB_PATH, "PC-3") is False)

    print("\n-- signed out is signed out --")
    with client.session_transaction() as sess:
        sess.clear()
    r = client.post("/api/machines/PC-1/live/watch", data="{}", headers=JSON)
    check("an anonymous caller cannot turn it on", r.status_code in (302, 401, 403))

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
