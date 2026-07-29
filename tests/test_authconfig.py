"""Unit tests for authconfig.py + envfile.py -- editable sign-in provider configuration.

The thing being edited here is the way back in, so the emphasis is lopsided on purpose:
almost every test below is about REFUSING something, because the cost of a wrongly-accepted
configuration is that nobody can sign in to correct it.

Specifically:
  * a config with no working provider must be refused
  * a half-filled provider must be refused, not silently treated as "off"
  * an http:// issuer must be refused (it chooses who the hub believes you are)
  * a secret must never be read back, and the "unchanged" placeholder must never be saved
    as if it were the secret
  * .env must never gain a BOM, which python-dotenv folds into the first key name
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import authconfig
import envfile

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


def rejects(label, fn):
    try:
        fn()
        check(label, False)
    except authconfig.AuthConfigError:
        check(label, True)


GOOGLE_ONLY = {"GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gsecret"}
OIDC_ONLY = {"OIDC_CLIENT_ID": "oid", "OIDC_CLIENT_SECRET": "osecret",
             "OIDC_ISSUER": "https://login.example.com"}


def main():
    print("\n== Reading the current configuration ==")
    cfg = authconfig.load(GOOGLE_ONLY)
    check("Google is enabled when both halves are present",
          authconfig.google_enabled(cfg))
    check("OIDC is not", not authconfig.oidc_enabled(cfg))

    cfg = authconfig.load(OIDC_ONLY)
    check("OIDC is enabled from issuer + id + secret", authconfig.oidc_enabled(cfg))
    # The convenience that makes "add Entra" a paste of one URL.
    check("the discovery URL is derived from the issuer",
          cfg["oidc_metadata_url"]
          == "https://login.example.com/.well-known/openid-configuration")
    check("a trailing slash on the issuer does not double up",
          authconfig.load({**OIDC_ONLY, "OIDC_ISSUER": "https://login.example.com/"})
          ["oidc_metadata_url"]
          == "https://login.example.com/.well-known/openid-configuration")
    check("an explicit discovery URL wins over the derived one",
          authconfig.load({**OIDC_ONLY, "OIDC_METADATA_URL": "https://x/custom"})
          ["oidc_metadata_url"] == "https://x/custom")
    check("the display name defaults",
          cfg["oidc_display_name"] == authconfig.DEFAULT_OIDC_DISPLAY_NAME)
    check("the scopes default", cfg["oidc_scopes"] == authconfig.DEFAULT_OIDC_SCOPES)
    check("an empty environment enables nothing",
          not authconfig.any_enabled(authconfig.load({})))

    print("\n== Validation refuses a hub nobody could sign in to ==")
    # THE guard. Everything else here is a usability refusal; this one is the difference
    # between an error message and needing shell access on the host to recover.
    rejects("a config with no provider at all is refused",
            lambda: authconfig.validate(authconfig.load({})))
    check("...and the message says why it cannot be undone from the console",
          "cannot be fixed from this page" in _message(
              lambda: authconfig.validate(authconfig.load({}))))
    check("but tests may opt out explicitly",
          authconfig.validate(authconfig.load({}), allow_no_provider=True) is not None)

    print("\n== A half-configured provider is refused, not ignored ==")
    # Silently treating these as "off" is how somebody spends an afternoon wondering why
    # the button they configured never appeared.
    rejects("Google with no secret",
            lambda: authconfig.validate(authconfig.load(
                {"GOOGLE_CLIENT_ID": "gid", **OIDC_ONLY})))
    rejects("Google with no client ID",
            lambda: authconfig.validate(authconfig.load(
                {"GOOGLE_CLIENT_SECRET": "s", **OIDC_ONLY})))
    rejects("OIDC with no secret",
            lambda: authconfig.validate(authconfig.load(
                {**GOOGLE_ONLY, "OIDC_CLIENT_ID": "oid",
                 "OIDC_ISSUER": "https://x.example.com"})))
    rejects("OIDC with no issuer",
            lambda: authconfig.validate(authconfig.load(
                {**GOOGLE_ONLY, "OIDC_CLIENT_ID": "oid", "OIDC_CLIENT_SECRET": "s"})))
    check("...and the message names what is missing",
          "client secret" in _message(lambda: authconfig.validate(authconfig.load(
              {**GOOGLE_ONLY, "OIDC_CLIENT_ID": "oid",
               "OIDC_ISSUER": "https://x.example.com"}))))

    print("\n== The issuer must be https ==")
    # Not pedantry: the discovery document names the token endpoint and the signing keys,
    # so anything that can rewrite it in flight chooses who this hub believes you are.
    rejects("an http:// issuer is refused",
            lambda: authconfig.validate(authconfig.load(
                {**OIDC_ONLY, "OIDC_ISSUER": "http://login.example.com"})))
    check("...and the message explains the downgrade, not just 'invalid'",
          "https" in _message(lambda: authconfig.validate(authconfig.load(
              {**OIDC_ONLY, "OIDC_ISSUER": "http://login.example.com"}))))
    rejects("a non-URL issuer is refused",
            lambda: authconfig.validate(authconfig.load(
                {**OIDC_ONLY, "OIDC_ISSUER": "login.example.com"})))
    rejects("an http:// discovery URL is refused too",
            lambda: authconfig.validate(authconfig.load(
                {**OIDC_ONLY, "OIDC_ISSUER": "", "OIDC_METADATA_URL": "http://x/y"})))

    print("\n== Scopes must include openid ==")
    # Without it the provider need not return an ID token, and sign-in silently degrades
    # to a plain OAuth grant with no verified identity.
    rejects("OIDC without the openid scope is refused",
            lambda: authconfig.validate(authconfig.load(
                {**OIDC_ONLY, "OIDC_SCOPES": "email profile"})))
    check("a config that does include it passes",
          authconfig.validate(authconfig.load(
              {**OIDC_ONLY, "OIDC_SCOPES": "openid email groups"})) is not None)
    check("the scope check does not fire when OIDC is off",
          authconfig.validate(authconfig.load(
              {**GOOGLE_ONLY, "OIDC_SCOPES": "nonsense"})) is not None)

    print("\n== Merging a console submission ==")
    current = authconfig.load({**GOOGLE_ONLY, **OIDC_ONLY})
    # A form that renders one provider must not be able to blank the other.
    merged = authconfig.merge(current, {"oidc_display_name": "Microsoft"})
    check("a key the caller did not send is left alone",
          merged["google_client_id"] == "gid" and merged["google_client_secret"] == "gsecret")
    check("the key they did send is applied", merged["oidc_display_name"] == "Microsoft")
    check("an unknown key is ignored rather than stored",
          "nonsense" not in authconfig.merge(current, {"nonsense": "x"}))

    # THE placeholder trap: saving it verbatim would make the client secret the literal
    # string of bullets, breaking sign-in with nothing in any log to say why.
    merged = authconfig.merge(current, {"google_client_secret": authconfig.UNCHANGED})
    check("the unchanged placeholder keeps the stored secret",
          merged["google_client_secret"] == "gsecret")
    merged = authconfig.merge(current, {"google_client_secret": "brand-new"})
    check("a real new secret replaces it", merged["google_client_secret"] == "brand-new")
    merged = authconfig.merge(current, {"google_client_id": "", "google_client_secret": ""})
    check("explicitly blanking both turns the provider off",
          not authconfig.google_enabled(merged))

    print("\n== Changing the issuer re-derives the discovery URL ==")
    # Otherwise a re-pointed hub keeps a discovery URL aimed at the previous tenant --
    # which would still work, and would still sign the wrong people in.
    merged = authconfig.merge(current, {"oidc_issuer": "https://new.example.com"})
    check("the derived URL follows the new issuer",
          merged["oidc_metadata_url"]
          == "https://new.example.com/.well-known/openid-configuration")
    merged = authconfig.merge(current, {"oidc_issuer": "https://new.example.com",
                                        "oidc_metadata_url": "https://new.example.com/dd"})
    check("an explicitly supplied discovery URL is still honoured",
          merged["oidc_metadata_url"] == "https://new.example.com/dd")
    check("clearing the display name restores the default",
          authconfig.merge(current, {"oidc_display_name": ""})["oidc_display_name"]
          == authconfig.DEFAULT_OIDC_DISPLAY_NAME)

    print("\n== The console view never carries a secret ==")
    view = authconfig.redacted(current)
    blob = repr(view)
    check("no secret value appears anywhere in it",
          "gsecret" not in blob and "osecret" not in blob)
    check("but it says a Google secret is set", view["google_client_secret_set"] is True)
    check("and that an OIDC secret is set", view["oidc_client_secret_set"] is True)
    check("client IDs are not secret and are shown", view["oidc_client_id"] == "oid")
    check("it carries the placeholder the editor should render",
          view["unchanged_placeholder"] == authconfig.UNCHANGED)
    check("an unset secret reports false",
          authconfig.redacted(authconfig.load({}))["google_client_secret_set"] is False)

    print("\n== Audit describes fields, never values ==")
    after = authconfig.merge(current, {"oidc_client_secret": "rotated"})
    changed = authconfig.describe_changes(current, after)
    check("the changed field is named", changed == ["oidc_client_secret"])
    check("the value is NOT in the change list", "rotated" not in repr(changed))
    check("an unchanged save reports nothing",
          authconfig.describe_changes(current, dict(current)) == [])

    # ================================================================
    # envfile
    # ================================================================
    fd, env_path = tempfile.mkstemp(suffix=".env")
    os.close(fd)
    try:
        print("\n== .env writing preserves the rest of the file ==")
        with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# a comment\nFLASK_SECRET_KEY=keepme\n\nALLOWED_EMAILS=a@x.com\n")
        envfile.set_vars(env_path, {"GOOGLE_CLIENT_ID": "new-id"})
        values = envfile.read_all(env_path)
        check("the new key is written", values["GOOGLE_CLIENT_ID"] == "new-id")
        check("unrelated keys survive", values["FLASK_SECRET_KEY"] == "keepme")
        check("...and so do the others", values["ALLOWED_EMAILS"] == "a@x.com")
        with open(env_path, encoding="utf-8") as fh:
            raw = fh.read()
        check("comments are preserved", "# a comment" in raw)

        print("\n== No BOM, ever ==")
        # python-dotenv folds a leading BOM into the FIRST key name, so a BOM here leaves
        # the hub unable to read its own first setting on the next restart. Windows tooling
        # writes them by default, which is exactly why this is asserted on the bytes.
        with open(env_path, "rb") as fh:
            head = fh.read(3)
        check("the file does not start with a UTF-8 BOM", head != b"\xef\xbb\xbf")
        # And one somebody else left behind must not be carried forward.
        with open(env_path, "w", encoding="utf-8-sig", newline="\n") as fh:
            fh.write("EXISTING=1\n")
        envfile.set_vars(env_path, {"NEW": "2"})
        with open(env_path, "rb") as fh:
            head = fh.read(3)
        check("an existing BOM is stripped rather than carried forward",
              head != b"\xef\xbb\xbf")
        check("...and the key it was attached to is read correctly",
              envfile.read_all(env_path).get("EXISTING") == "1")

        print("\n== None deletes a key rather than writing it empty ==")
        envfile.set_vars(env_path, {"TO_REMOVE": "x"})
        check("written first", "TO_REMOVE" in envfile.read_all(env_path))
        envfile.set_vars(env_path, {"TO_REMOVE": None})
        check("None removes the line entirely",
              "TO_REMOVE" not in envfile.read_all(env_path))

        print("\n== Multi-key writes are one rewrite ==")
        # Writing a client id and secret as two passes leaves a window where the file has
        # a new id beside an old secret; a hub restarting in that window cannot sign
        # anybody in.
        changed = envfile.set_vars(env_path, {"K1": "a", "K2": "b"})
        check("both are written", envfile.read_all(env_path)["K1"] == "a"
              and envfile.read_all(env_path)["K2"] == "b")
        check("both are reported changed", changed == {"K1", "K2"})
        check("re-writing identical values reports no change",
              envfile.set_vars(env_path, {"K1": "a", "K2": "b"}) == set())
        check("changing one reports only that one",
              envfile.set_vars(env_path, {"K1": "a", "K2": "c"}) == {"K2"})

        print("\n== save() updates .env AND os.environ together ==")
        # Writing one without the other gives either a change that evaporates on restart,
        # or one that does nothing until it happens.
        for key in authconfig.FIELD_NAMES:
            os.environ.pop(key, None)
        config = authconfig.load({**GOOGLE_ONLY, **OIDC_ONLY})
        authconfig.save(env_path, config)
        check("the file has the client id",
              envfile.read_all(env_path)["GOOGLE_CLIENT_ID"] == "gid")
        check("the live process sees it too", os.environ.get("GOOGLE_CLIENT_ID") == "gid")
        check("load_current() reads back what was saved",
              authconfig.load_current()["oidc_client_id"] == "oid")
        # Turning a provider off must clear both, or it comes back on the next restart.
        off = authconfig.merge(config, {"google_client_id": "", "google_client_secret": ""})
        authconfig.save(env_path, off)
        check("turning Google off removes it from the file",
              "GOOGLE_CLIENT_ID" not in envfile.read_all(env_path))
        check("...and from the live process", not os.environ.get("GOOGLE_CLIENT_ID"))
        check("...and load_current() agrees it is off",
              not authconfig.google_enabled(authconfig.load_current()))
    finally:
        for key in authconfig.FIELD_NAMES:
            os.environ.pop(key, None)
        try:
            os.remove(env_path)
        except OSError:
            pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


def _message(fn):
    try:
        fn()
    except authconfig.AuthConfigError as e:
        return str(e)
    return ""


if __name__ == "__main__":
    sys.exit(main())
