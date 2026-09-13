"""Sign a Flask test client in to the console, the way a browser is signed in.

**Not a test module** -- run_all.py globs `test_*.py`, so this file is a helper and nothing
else. It exists because signing in stopped being one line.

A console session is now TWO things, not one: the signed cookie that says who you are, and
a per-session CSRF token that `login_required` requires back in an `X-CSRF-Token` header on
every POST, PUT, PATCH and DELETE. In the browser the token is minted at sign-in, published
in a `<meta>` tag by base.html and echoed by a fetch interceptor in common.js, so nothing
in the console has to think about it. A test client has neither the meta tag nor the
interceptor, so every module that drove the app through `app.app.test_client()` started
getting 403s on every write the moment the token check landed -- six modules at once, each
failing in its own way and none of them mentioning CSRF.

So the rule lives here once. `sign_in(client, email)` seeds both halves and pins the header
onto the client's `environ_base`, which werkzeug merges into every subsequent request --
the closest thing a test client has to common.js.

**The 415 content-type layer is deliberately left alone.** It is the older half of the same
gate and still runs after the token check, so a test that posts a form content type with a
valid token still gets its 415. Making this helper send JSON too would have quietly disabled
the control test_csrf.py exists to pin.
"""

# Any 64 hex chars will do: the check is that the header equals what the session holds, and
# the session is seeded here. A fixed value rather than a random one so a failing request is
# reproducible and greppable in a log.
TOKEN = "c5f" * 21 + "d"


def sign_in(client, email, token=TOKEN, **session_values):
    """Give `client` a signed-in console session, CSRF token included.

    Extra keyword arguments are written into the session as-is, for the modules that seed
    more than an email (a pending OAuth state, a device pairing, a chosen language).

    Returns the token, so a test that wants to assert on the gate itself -- send a wrong
    one, or none -- has the right one to compare against.
    """
    with client.session_transaction() as session:
        session["user"] = {"email": email} if isinstance(email, str) else email
        session["csrf_token"] = token
        session.update(session_values)
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token
    return token


def sign_out(client):
    """Drop both halves. A client that has been signed out must not keep sending a header
    for a session it no longer has -- that would test a state no browser can be in."""
    with client.session_transaction() as session:
        session.clear()
    client.environ_base.pop("HTTP_X_CSRF_TOKEN", None)
