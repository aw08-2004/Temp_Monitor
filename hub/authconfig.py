"""The sign-in provider configuration -- Google OAuth and generic OIDC -- as editable data.

Historically these were `.env`-only and read once at import, so changing an issuer meant
shell access on the hub host plus a service restart. This module makes them editable from
the console by the break-glass admins, and is the flask-free half of that: read, validate,
and write. `auth_web.py` is the HTTP surface; `app.configure_oauth()` is what re-registers
the Authlib clients so a change takes effect without a restart.

**Why break-glass only, and not `manage_settings`.** This is the perimeter itself. Someone
who can point the hub at an OIDC issuer can point it at one THEY control, assert any email
they like, and sign in as anybody -- so this must not be delegable to the capability that
otherwise means "tune the hub". `ALLOWED_EMAILS` members already hold every capability
over every machine, including code execution as SYSTEM, so for them it is not an
escalation; for everyone else it would be.

**The lockout question, which is the whole risk here.** Every guard below exists because
the thing being edited is the way back in, and a hub with no working provider cannot be
fixed from the console that just broke. Hence: a config leaving zero providers enabled is
refused (`validate`); a partially-filled provider is refused rather than silently ignored;
and the caller is expected to keep the OLD configuration live if re-registration fails.
What this module deliberately does NOT try to prevent is a *valid but wrong* provider --
correct-looking credentials pointing at the wrong tenant. Nothing here can tell that from
a correct one, which is why auth_web.py makes the operator confirm and audits the change.
"""
import re

import envfile

# ---------------------------------------------------------------- the fields
# Each entry: (env var, secret?). `secret` fields are never read back to the console --
# the API reports whether one is set, never what it is, the same rule the TURN secret and
# the AD bind password already follow.
GOOGLE_FIELDS = (
    ("GOOGLE_CLIENT_ID", False),
    ("GOOGLE_CLIENT_SECRET", True),
)
OIDC_FIELDS = (
    ("OIDC_CLIENT_ID", False),
    ("OIDC_CLIENT_SECRET", True),
    ("OIDC_ISSUER", False),
    ("OIDC_METADATA_URL", False),
    ("OIDC_DISPLAY_NAME", False),
    ("OIDC_SCOPES", False),
)
ALL_FIELDS = GOOGLE_FIELDS + OIDC_FIELDS
FIELD_NAMES = tuple(name for name, _ in ALL_FIELDS)
SECRET_FIELDS = frozenset(name for name, secret in ALL_FIELDS if secret)

DEFAULT_OIDC_DISPLAY_NAME = "SSO"
DEFAULT_OIDC_SCOPES = "openid email profile"

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_SCOPES = "openid email profile"

# A value the console sends back to mean "leave the stored secret alone". The editor has to
# render SOMETHING in a password field for a secret that is set, and if that placeholder
# were saved verbatim the client secret would become the literal string below -- breaking
# sign-in in a way whose cause is invisible in every log.
UNCHANGED = "••••••••"


class AuthConfigError(ValueError):
    """A rejected configuration. The message is written for an operator to read in the
    console -- these are all mistakes a human can fix, not internal errors."""


def _clean(value):
    return str(value or "").strip()


def load(environ):
    """Read the current provider configuration out of an environment mapping."""
    get = environ.get
    issuer = _clean(get("OIDC_ISSUER")).rstrip("/")
    metadata_url = _clean(get("OIDC_METADATA_URL"))
    # Either form works: an admin is as likely to have the issuer on hand as the full
    # discovery URL, and deriving one from the other here means the rest of the hub only
    # ever deals with a metadata URL.
    if issuer and not metadata_url:
        metadata_url = issuer + "/.well-known/openid-configuration"
    return {
        "google_client_id": _clean(get("GOOGLE_CLIENT_ID")),
        "google_client_secret": _clean(get("GOOGLE_CLIENT_SECRET")),
        "oidc_client_id": _clean(get("OIDC_CLIENT_ID")),
        "oidc_client_secret": _clean(get("OIDC_CLIENT_SECRET")),
        "oidc_issuer": issuer,
        "oidc_metadata_url": metadata_url,
        "oidc_display_name": _clean(get("OIDC_DISPLAY_NAME")) or DEFAULT_OIDC_DISPLAY_NAME,
        "oidc_scopes": _clean(get("OIDC_SCOPES")) or DEFAULT_OIDC_SCOPES,
    }


def load_current():
    """The live configuration, from os.environ -- which `save()` keeps in step with .env.
    One accessor so callers cannot accidentally read a stale snapshot taken at import."""
    import os
    return load(os.environ)


def google_enabled(config):
    return bool(config.get("google_client_id") and config.get("google_client_secret"))


def oidc_enabled(config):
    return bool(config.get("oidc_client_id") and config.get("oidc_client_secret")
                and config.get("oidc_metadata_url"))


def any_enabled(config):
    return google_enabled(config) or oidc_enabled(config)


# ---------------------------------------------------------------- validation
_HTTPS_URL = re.compile(r"^https://[^\s/@]+(/\S*)?$", re.IGNORECASE)


def _check_url(value, label):
    """Issuer/discovery URLs must be https. Not pedantry: the discovery document names the
    token endpoint and the signing keys, so anything that can rewrite it in flight chooses
    who this hub believes you are. An http:// issuer is a downgrade of the entire login."""
    if not _HTTPS_URL.match(value):
        if value.lower().startswith("http://"):
            raise AuthConfigError(
                f"{label} must use https. Over plain http, anything on the network path "
                f"can rewrite the discovery document and choose who this hub believes "
                f"you are.")
        raise AuthConfigError(f"{label} does not look like a URL: {value!r}. "
                              f"Expected something like https://login.example.com.")


def validate(config, *, allow_no_provider=False):
    """Check a fully-resolved config, raising AuthConfigError with an operator-readable
    message. Returns the config unchanged so it can be used inline.

    `allow_no_provider` exists only for tests; the console never passes it. Refusing a
    zero-provider config is the single most important guard here -- it is the difference
    between "you made a mistake" and "nobody can sign in to this hub again without shell
    access on the host".
    """
    # A half-filled provider is refused rather than treated as "off". Silently ignoring a
    # client id whose secret is missing is how someone spends an afternoon wondering why
    # the button they configured never appeared.
    if config.get("google_client_id") and not config.get("google_client_secret"):
        raise AuthConfigError("Google is missing its client secret.")
    if config.get("google_client_secret") and not config.get("google_client_id"):
        raise AuthConfigError("Google is missing its client ID.")

    oidc_parts = [bool(config.get("oidc_client_id")),
                  bool(config.get("oidc_client_secret")),
                  bool(config.get("oidc_metadata_url"))]
    if any(oidc_parts) and not all(oidc_parts):
        missing = []
        if not config.get("oidc_client_id"):
            missing.append("client ID")
        if not config.get("oidc_client_secret"):
            missing.append("client secret")
        if not config.get("oidc_metadata_url"):
            missing.append("issuer URL")
        raise AuthConfigError("The OIDC provider is missing its "
                              + " and ".join(missing) + ".")

    if config.get("oidc_issuer"):
        _check_url(config["oidc_issuer"], "The OIDC issuer")
    if config.get("oidc_metadata_url"):
        _check_url(config["oidc_metadata_url"], "The OIDC discovery URL")

    scopes = (config.get("oidc_scopes") or "").split()
    if oidc_enabled(config) and "openid" not in scopes:
        # Without `openid` the provider need not return an ID token at all, and the whole
        # sign-in silently degrades to a plain OAuth grant with no verified identity.
        raise AuthConfigError(
            "The OIDC scopes must include 'openid' -- without it the provider is not "
            "obliged to return an ID token, and there is no verified identity to sign in.")

    if not allow_no_provider and not any_enabled(config):
        raise AuthConfigError(
            "That would leave no way to sign in to this hub. Configure Google or an OIDC "
            "provider (or both) before saving -- a hub with no provider cannot be fixed "
            "from this page, only by editing .env on the server.")
    return config


# ---------------------------------------------------------------- applying
def merge(current, submitted):
    """Fold a console submission onto the current config.

    Two rules, both about not destroying what you cannot see:
      * a key the caller did not send is LEFT ALONE, so a form that renders one provider
        cannot blank the other;
      * a secret sent back as the UNCHANGED placeholder keeps its stored value, which is
        what lets the editor show "a secret is set" without ever reading it out.
    """
    merged = dict(current)
    for key, value in (submitted or {}).items():
        if key not in merged:
            continue
        text = _clean(value)
        if key in ("google_client_secret", "oidc_client_secret") and text == UNCHANGED:
            continue
        merged[key] = text
    # Re-derive so a caller that changed only the issuer gets a matching discovery URL,
    # rather than keeping one pointing at the previous tenant.
    issuer = merged.get("oidc_issuer", "").rstrip("/")
    merged["oidc_issuer"] = issuer
    submitted_metadata = _clean((submitted or {}).get("oidc_metadata_url"))
    if issuer and not submitted_metadata:
        merged["oidc_metadata_url"] = issuer + "/.well-known/openid-configuration"
    if not merged.get("oidc_display_name"):
        merged["oidc_display_name"] = DEFAULT_OIDC_DISPLAY_NAME
    if not merged.get("oidc_scopes"):
        merged["oidc_scopes"] = DEFAULT_OIDC_SCOPES
    return merged


def to_env(config):
    """The .env representation of a config. Empty values become None, i.e. the key is
    REMOVED rather than written blank -- so turning a provider off leaves a file that
    honestly says it is not configured."""
    return {
        "GOOGLE_CLIENT_ID": config.get("google_client_id") or None,
        "GOOGLE_CLIENT_SECRET": config.get("google_client_secret") or None,
        "OIDC_CLIENT_ID": config.get("oidc_client_id") or None,
        "OIDC_CLIENT_SECRET": config.get("oidc_client_secret") or None,
        "OIDC_ISSUER": config.get("oidc_issuer") or None,
        "OIDC_METADATA_URL": config.get("oidc_metadata_url") or None,
        "OIDC_DISPLAY_NAME": config.get("oidc_display_name") or None,
        "OIDC_SCOPES": config.get("oidc_scopes") or None,
    }


def save(env_path, config):
    """Write a validated config to .env AND to os.environ, and report which keys moved.

    Both, always: the file is what survives a restart and os.environ is what the running
    process reads, so writing one without the other gives either a change that evaporates
    on restart or one that does nothing until it happens.
    """
    updates = to_env(config)
    changed = envfile.set_vars(env_path, updates)
    envfile.apply_to_environ(updates)
    return changed


def redacted(config):
    """The console-safe view: every field except the secrets, plus a flag per secret
    saying whether one is set. The secrets themselves never leave the server."""
    return {
        "google_client_id": config.get("google_client_id", ""),
        "google_client_secret_set": bool(config.get("google_client_secret")),
        "google_enabled": google_enabled(config),
        "oidc_client_id": config.get("oidc_client_id", ""),
        "oidc_client_secret_set": bool(config.get("oidc_client_secret")),
        "oidc_issuer": config.get("oidc_issuer", ""),
        "oidc_metadata_url": config.get("oidc_metadata_url", ""),
        "oidc_display_name": config.get("oidc_display_name", ""),
        "oidc_scopes": config.get("oidc_scopes", ""),
        "oidc_enabled": oidc_enabled(config),
        "unchanged_placeholder": UNCHANGED,
    }


def describe_changes(before, after):
    """Which fields changed, for the audit trail -- names only, never values.

    A client secret's old and new values in an audit row would put the credential in the
    database, and then in the hub-database backup, which is the exact thing keeping it out
    of the settings table was for.
    """
    changed = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed
