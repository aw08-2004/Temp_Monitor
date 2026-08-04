"""Model tests for apitokens.py -- the native client's credential (roadmap #11).

Flask-free, like the module under test. What is worth stating about the assertions:

  * **The token is never recoverable from the database.** Only its hash is stored, so the
    test that matters is that the stored row does not contain the secret anywhere -- not
    that authentication happens to work.
  * **Nothing is minted until a code is exchanged.** An abandoned pairing must leave no
    credential behind. That is the whole reason create_grant and mint_token are separate,
    so it is asserted directly.
  * **A pairing code is single-use and short-lived**, and an expired one is CONSUMED
    rather than left lying around for a second attempt.
  * **The loopback redirect rules are an allow-list.** This is the sharpest edge in the
    feature -- the redirect carries a code that becomes a fleet credential -- so the
    refusals are enumerated rather than sampled.
  * **Administrative capabilities cannot reach a device at all.** Refused at mint time, so
    the credential that could rewrite the permission model is never created.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import apitokens
import fleet
import permissions

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


def header(token):
    return f"Bearer {token}"


def test_capability_validation():
    print("\n== A device may hold a subset, and never an administrative capability ==")
    check("orders by the admin UI's own order",
          apitokens.normalize_capabilities(
              [permissions.ISSUE_COMMANDS, permissions.VIEW])
          == [permissions.VIEW, permissions.ISSUE_COMMANDS])
    check("deduplicates",
          apitokens.normalize_capabilities([permissions.VIEW, permissions.VIEW])
          == [permissions.VIEW])

    for forbidden in (permissions.MANAGE_SETTINGS, permissions.MANAGE_USERS,
                      permissions.MANAGE_PERMISSION_GROUPS, permissions.MANAGE_FIRMWARE):
        try:
            apitokens.normalize_capabilities([permissions.VIEW, forbidden])
            check(f"{forbidden} refused at mint time", False)
        except apitokens.PairingError as e:
            check(f"{forbidden} refused at mint time, by name", forbidden in str(e))

    try:
        apitokens.normalize_capabilities(["fly_the_plane"])
        check("an unknown capability is refused", False)
    except apitokens.PairingError as e:
        check("an unknown capability is refused, by name", "fly_the_plane" in str(e))

    try:
        apitokens.normalize_capabilities([])
        check("an empty set is refused", False)
    except apitokens.PairingError:
        check("an empty set is refused -- an inert device is a support call", True)

    check("DEVICE_CAPABILITIES excludes exactly the forbidden four",
          set(permissions.CAPABILITIES) - set(apitokens.DEVICE_CAPABILITIES)
          == apitokens.DEVICE_FORBIDDEN_CAPABILITIES)


def test_authorization_header_parsing():
    print("\n== A user token and an agent token cannot be confused ==")
    token_id, secret = apitokens.parse_authorization("Bearer tmu_abc123:s3cret")
    check("parses a user token", (token_id, secret) == ("abc123", "s3cret"))

    # The agent's format is '<agent_id>:<token>' with no prefix. It must not parse here,
    # or an agent credential could reach a console endpoint.
    check("an agent token does not parse as a user token",
          apitokens.parse_authorization("Bearer deadbeef:agenttoken") == (None, None))
    check("no prefix, no parse",
          apitokens.parse_authorization("Bearer abc:def") == (None, None))
    check("missing secret", apitokens.parse_authorization("Bearer tmu_abc:") == (None, None))
    check("missing id", apitokens.parse_authorization("Bearer tmu_:secret") == (None, None))
    check("no colon at all",
          apitokens.parse_authorization("Bearer tmu_abcdef") == (None, None))
    check("not a bearer header", apitokens.parse_authorization("Basic abc") == (None, None))
    check("no header at all", apitokens.parse_authorization(None) == (None, None))


def test_loopback_redirect():
    print("\n== The pairing redirect is an allow-list, not a pattern ==")
    ok = ("http://127.0.0.1:53219/cb",
          "http://127.0.0.1:1024/",
          "http://127.5.5.5:40000/callback?app=fleethub",
          "http://[::1]:53219/cb")
    for url in ok:
        try:
            check(f"accepts {url}", apitokens.validate_loopback_redirect(url) == url)
        except apitokens.PairingError as e:
            check(f"accepts {url} (refused: {e})", False)

    bad = {
        "https://evil.example.com/cb": "not loopback",
        "http://evil.example.com/cb": "not loopback",
        # The classic near-miss: a host that merely STARTS with the loopback digits.
        "http://127.0.0.1.evil.com:8080/cb": "a host that only looks like loopback",
        "http://10.0.0.1:8080/cb": "a private address is not loopback",
        "http://localhost:53219/cb": "a name resolves through a hosts file",
        "http://127.0.0.1/cb": "no port",
        "http://127.0.0.1:80/cb": "a privileged port",
        "http://127.0.0.1:53219/cb#frag": "a fragment",
        "http://user:pw@127.0.0.1:53219/cb": "credentials in the url",
        "https://127.0.0.1:53219/cb": "https on loopback is not the native-app flow",
        "javascript:alert(1)": "not a url at all",
        "": "empty",
    }
    for url, why in bad.items():
        try:
            apitokens.validate_loopback_redirect(url)
            check(f"refuses {url!r} ({why})", False)
        except apitokens.PairingError:
            check(f"refuses {url!r} ({why})", True)

    check("the localhost refusal names the fix",
          "127.0.0.1" in _refusal("http://localhost:53219/cb"))


def _refusal(url):
    try:
        apitokens.validate_loopback_redirect(url)
    except apitokens.PairingError as e:
        return str(e)
    return ""


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        permissions.init_permissions_db(db_path)
        apitokens.init_apitokens_db(db_path)

        test_capability_validation()
        test_authorization_header_parsing()
        test_loopback_redirect()

        print("\n== Nothing is minted until the code is exchanged ==")
        code = apitokens.create_grant(
            db_path, "carol@x.com", "Carol's laptop", "windows",
            [permissions.VIEW, permissions.ISSUE_COMMANDS])
        check("no device exists yet", apitokens.list_tokens(db_path) == [])

        token, row = apitokens.redeem_grant(db_path, code)
        check("exchange mints exactly one device", len(apitokens.list_tokens(db_path)) == 1)
        check("the token carries the prefix", token.startswith(apitokens.TOKEN_PREFIX))
        check("the row names the device", row["device_name"] == "Carol's laptop")
        check("...and its owner", row["email"] == "carol@x.com")
        check("...and only the granted capabilities",
              row["capabilities"] == [permissions.VIEW, permissions.ISSUE_COMMANDS])

        print("\n== The token is not recoverable from the database ==")
        with fleet.get_conn(db_path) as conn:
            stored = dict(conn.execute(
                "SELECT * FROM api_tokens WHERE token_id = ?",
                (row["token_id"],)).fetchone())
        secret = token.split(":", 1)[1]
        check("the secret appears in no column",
              not any(secret in str(v) for v in stored.values()))
        check("a listing never carries a token field",
              "token" not in apitokens.list_tokens(db_path)[0])

        print("\n== A pairing code works once, and only for a minute ==")
        try:
            apitokens.redeem_grant(db_path, code)
            check("a used code is refused", False)
        except apitokens.PairingError:
            check("a used code is refused", True)
        check("...and minted nothing the second time",
              len(apitokens.list_tokens(db_path)) == 1)

        stale = apitokens.create_grant(db_path, "carol@x.com", "Slow app", "windows",
                                       [permissions.VIEW], now=time.time() - 3600)
        try:
            apitokens.redeem_grant(db_path, stale)
            check("an expired code is refused", False)
        except apitokens.PairingError as e:
            check("an expired code is refused, and says so", "expired" in str(e).lower())
        try:
            apitokens.redeem_grant(db_path, stale)
            check("...and was CONSUMED, not left for a second attempt", False)
        except apitokens.PairingError as e:
            check("...and was CONSUMED, not left for a second attempt",
                  "expired" not in str(e).lower())

        print("\n== Authentication ==")
        identity = apitokens.authenticate(db_path, header(token))
        check("a good token resolves", identity is not None)
        check("...to its owner", identity["email"] == "carol@x.com")
        check("...carrying the ceiling, not a grant",
              identity["token_capabilities"]
              == [permissions.VIEW, permissions.ISSUE_COMMANDS])

        bad_secret = f"{apitokens.TOKEN_PREFIX}{row['token_id']}:wrong"
        check("a wrong secret resolves to nothing",
              apitokens.authenticate(db_path, header(bad_secret)) is None)
        check("an unknown token id resolves to nothing",
              apitokens.authenticate(
                  db_path, header(f"{apitokens.TOKEN_PREFIX}nope:secret")) is None)
        check("an agent-shaped token resolves to nothing",
              apitokens.authenticate(db_path, "Bearer someagent:sometoken") is None)

        print("\n== Expiry slides on use, against the row's OWN lifetime ==")
        short_code = apitokens.create_grant(db_path, "dave@x.com", "Kiosk", "windows",
                                            [permissions.VIEW], lifetime_days=30)
        short_token, short_row = apitokens.redeem_grant(db_path, short_code)
        check("the row keeps the lifetime it was paired with",
              short_row["lifetime_days"] == 30)

        later = int(time.time()) + 20 * 86400
        apitokens.authenticate(db_path, header(short_token), now=later)
        refreshed = apitokens.get_token(db_path, short_row["token_id"])
        check("using it pushes the expiry out by its own lifetime",
              refreshed["expires_at"] == later + 30 * 86400)
        check("...and records when it was last used", refreshed["last_used_at"] == later)

        way_later = int(time.time()) + 400 * 86400
        check("a device that stopped checking in is refused",
              apitokens.authenticate(db_path, header(short_token), now=way_later) is None)

        print("\n== Revocation ==")
        check("revoking someone else's device by id is a miss, not a revoke",
              apitokens.revoke_token(db_path, row["token_id"], actor="dave@x.com",
                                     email="dave@x.com") is False)
        check("...and the device still works",
              apitokens.authenticate(db_path, header(token)) is not None)

        check("the owner may revoke their own",
              apitokens.revoke_token(db_path, row["token_id"], actor="carol@x.com",
                                     email="carol@x.com") is True)
        check("a revoked device stops working at once",
              apitokens.authenticate(db_path, header(token)) is None)
        check("revoking twice is not a second revoke",
              apitokens.revoke_token(db_path, row["token_id"], actor="carol@x.com") is False)
        check("a revoked device is still LISTED for an admin, so 'why did it stop' has "
              "an answer",
              any(d["token_id"] == row["token_id"]
                  for d in apitokens.list_tokens(db_path, include_revoked=True)))
        check("...and hidden from the ordinary listing",
              all(d["token_id"] != row["token_id"] for d in apitokens.list_tokens(db_path)))

        print("\n== Revoking everything one person holds ==")
        for name in ("Phone", "Tablet"):
            c = apitokens.create_grant(db_path, "erin@x.com", name, "android",
                                       [permissions.VIEW])
            apitokens.redeem_grant(db_path, c)
        check("two devices for erin",
              len(apitokens.list_tokens(db_path, email="erin@x.com")) == 2)
        check("revoke_all reports how many it took",
              apitokens.revoke_all_for_email(db_path, "erin@x.com", actor="admin@x.com") == 2)
        check("...and leaves her none",
              apitokens.list_tokens(db_path, email="erin@x.com") == [])
        check("...without touching anybody else's",
              len(apitokens.list_tokens(db_path, email="dave@x.com")) == 1)

        print("\n== The audit trail ==")
        actions = [e["action"] for e in fleet.list_audit(db_path, limit=200)["entries"]]
        for action in ("device.pair", "device.first_use", "device.revoke"):
            check(f"{action} is recorded", action in actions)
        check("every device action is security level",
              all(e["level"] == fleet.LEVEL_SECURITY
                  for e in fleet.list_audit(db_path, limit=200)["entries"]
                  if e["action"].startswith("device.")))
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
