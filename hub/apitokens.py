"""User API tokens -- the credential a native FleetHub client signs in with (roadmap #11).

Every console endpoint until now has been reachable exactly one way: a signed Flask
session cookie, minted by the OAuth/OIDC flow in app.py. That works for a browser and
cannot work for a native app, because there is no password to type into one -- sign-in is
OAuth-only, deliberately, and adding a local password would be the largest step backwards
this product could take.

So a device gets a BEARER TOKEN, and it gets it by proving it can drive a real sign-in:

    app -> opens the system browser at /app/pair          (apitokens_web.py)
    hub -> authenticates that browser the ordinary way    (app.py's OAuth, untouched)
    hub -> operator confirms the device on a consent page
    hub -> redirects to the app's loopback listener with a one-time CODE
    app -> exchanges the code for the token, exactly once

**Nothing is minted until the code is exchanged.** The consent page records a GRANT --
who, which device, which capabilities -- and the token itself does not exist until the
device actually collects it. An abandoned pairing therefore leaves no credential behind,
and no plaintext token is ever at rest anywhere, not even for the sixty seconds the code
is alive. That is why `create_grant` and `mint_token` are separate calls.

Three properties carry the security of this, and each is a deliberate answer to how a
long-lived credential on a laptop differs from a session cookie in a browser:

  * **A token holds a SUBSET of its owner's capabilities, intersected LIVE.** The subset
    is chosen at pairing; the effective set is recomputed against the owner's real
    permissions on every request (see permissions_web.Access.current). Without the
    intersection, a token minted while someone was an admin would keep admin after they
    were demoted -- the credential would outlive the grant, which is precisely the
    failure mode a token introduces and a session does not.
  * **Administrative capabilities are refused at mint time** (DEVICE_FORBIDDEN_CAPABILITIES).
    Console administration stays in the console. A laptop in a bag must not be able to
    rewrite the permission model, and the cheapest way to guarantee that is for the
    credential to have never been able to.
  * **Tokens EXPIRE, and the expiry slides on use.** A device that stops checking in
    loses access on its own, rather than leaving a permanent credential nobody remembers
    issuing. Each row carries its own `lifetime_days`, so changing the fleet default never
    silently rewrites the lifetime an operator already agreed to -- the same rule
    packages.py applies to retry policy.

The wire format is `Authorization: Bearer tmu_<token_id>:<secret>`, and the `tmu_` prefix
is load-bearing rather than decorative. Agent auth is `Bearer <agent_id>:<token>`, parsed
by five copies of `_bearer_agent` (fleet_web, remote_web, bios_web, backups_web,
packages_web). A user token must never authenticate at an agent endpoint and an agent
token must never authenticate at a console one; a distinguishable prefix makes each of
those a parse failure at the door instead of a database lookup that happens to miss.

Kept free of Flask so it can be unit-tested in isolation, exactly like fleet.py,
permissions.py and wake.py.
"""
import hashlib
import hmac
import json
import secrets
import time
import uuid

import fleet
import permissions

# ================================
# VOCABULARY
# ================================
#: Distinguishes a user token from an agent token in the SAME header. See the module
#: docstring -- this is an authentication boundary, not a naming convenience.
TOKEN_PREFIX = "tmu_"

#: How long a pairing code is worth anything. Sixty seconds is the time between a browser
#: redirect and a local HTTP handler reading it; anything longer is a window in which a
#: code sitting in a browser history or a proxy log is still a live fleet credential.
PAIRING_CODE_TTL_SECONDS = 60

#: Fleet default when the caller does not name one. Overridable per hub via the
#: `api.token_lifetime_days` setting; the value in force at pairing is COPIED onto the row.
DEFAULT_LIFETIME_DAYS = 90

#: Capabilities a device may never hold, whatever its owner holds. Administration is a
#: browser activity: these four either rewrite who can do what, or write firmware -- the
#: one action in the product with no restore path (see permissions.MANAGE_FIRMWARE).
#: Refused at mint time rather than filtered at request time, so the credential that could
#: do it is never created and nothing later has to remember to check.
DEVICE_FORBIDDEN_CAPABILITIES = frozenset({
    permissions.MANAGE_SETTINGS,
    permissions.MANAGE_USERS,
    permissions.MANAGE_PERMISSION_GROUPS,
    permissions.MANAGE_FIRMWARE,
})

#: What a device MAY be granted, in the admin UI's own order.
DEVICE_CAPABILITIES = tuple(
    c for c in permissions.CAPABILITIES if c not in DEVICE_FORBIDDEN_CAPABILITIES
)

#: What the pairing page ticks by default: read the fleet, and press the buttons that make
#: a phone worth having (wake, favourites, restart). ISSUE_COMMANDS is what wake_web.py
#: gates Wake-on-LAN on, so leaving it out would make the default pairing unable to do the
#: single most useful thing the app does.
DEFAULT_DEVICE_CAPABILITIES = (permissions.VIEW, permissions.ISSUE_COMMANDS)

#: Only touch `last_used_at` (and slide the expiry) when the row is at least this stale.
#: The app polls every ten seconds; writing a row per poll would put a steady write load on
#: the same WAL database telemetry is landing in, to maintain a column nothing but a
#: human-readable device list reads.
TOUCH_INTERVAL_SECONDS = 60

#: Device names are shown in a revoke list, so they only have to be readable.
MAX_DEVICE_NAME_CHARS = 64
MAX_PLATFORM_CHARS = 32


class PairingError(Exception):
    """A pairing request that cannot be honoured, with a message meant for a human."""


# ================================
# SCHEMA
# ================================
def init_apitokens_db(db_path):
    with fleet.get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One row per PAIRED DEVICE. `email` is the owner rather than a user id because
        # that is the key the whole authorization model already uses (permission group
        # membership, the audit trail, ALLOWED_EMAILS) -- a second identifier here would
        # be a second thing to keep in step.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_tokens (
                token_id              TEXT PRIMARY KEY,
                token_hash            TEXT NOT NULL,
                email                 TEXT NOT NULL,
                device_name           TEXT NOT NULL DEFAULT '',
                platform              TEXT NOT NULL DEFAULT '',
                capabilities_json     TEXT NOT NULL DEFAULT '[]',
                directory_groups_json TEXT NOT NULL DEFAULT '[]',
                lifetime_days         INTEGER NOT NULL,
                created_at            INTEGER NOT NULL,
                last_used_at          INTEGER,
                expires_at            INTEGER NOT NULL,
                revoked               INTEGER NOT NULL DEFAULT 0,
                -- Unused in v1 and deliberately present: phase 2 registers an FCM/APNs
                -- device token for push, and a device IS this row. Adding the columns now
                -- costs nothing and keeps push from needing a second registry that could
                -- disagree with this one about which devices exist.
                push_kind             TEXT,
                push_token            TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_tokens_email ON api_tokens(email)")

        # A pairing that has been consented to but not yet collected. Holds no secret of
        # its own beyond the code's hash: the token is minted at exchange time.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_token_grants (
                code_hash             TEXT PRIMARY KEY,
                email                 TEXT NOT NULL,
                device_name           TEXT NOT NULL DEFAULT '',
                platform              TEXT NOT NULL DEFAULT '',
                capabilities_json     TEXT NOT NULL DEFAULT '[]',
                directory_groups_json TEXT NOT NULL DEFAULT '[]',
                lifetime_days         INTEGER NOT NULL,
                created_at            INTEGER NOT NULL,
                expires_at            INTEGER NOT NULL
            )
        """)


# ================================
# HELPERS
# ================================
def _hash_secret(value):
    """Store only the hash, so a database leak hands out no live credential.

    Deliberately the SAME rule as fleet._hash_token (sha256 of the utf-8 bytes, hex) --
    two token stores that hash differently is two things to get right, and there is no
    reason for them to differ.
    """
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _clean(value, limit):
    return str(value or "").strip()[:limit]


def _json_list(raw):
    try:
        loaded = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in loaded] if isinstance(loaded, list) else []


def normalize_capabilities(requested):
    """Validate a requested capability set, or raise PairingError naming the problem.

    Returns them in permissions.CAPABILITIES order so a stored set and a displayed set
    cannot disagree about ordering, and so two identical grants compare equal.
    """
    wanted = {str(c or "").strip() for c in (requested or ())}
    wanted.discard("")
    if not wanted:
        raise PairingError("A device needs at least one capability.")

    unknown = sorted(w for w in wanted if w not in permissions.CAPABILITIES)
    if unknown:
        raise PairingError(f"Unknown capability: {', '.join(unknown)}.")

    forbidden = sorted(w for w in wanted if w in DEVICE_FORBIDDEN_CAPABILITIES)
    if forbidden:
        raise PairingError(
            "These capabilities cannot be granted to a device, only used in the "
            f"console: {', '.join(forbidden)}.")

    return [c for c in permissions.CAPABILITIES if c in wanted]


def parse_authorization(header_value):
    """Split 'Bearer tmu_<token_id>:<secret>' into (token_id, secret), or (None, None).

    Returns (None, None) for an AGENT token as well as for junk -- see the module
    docstring. The caller must not fall back to trying it as anything else.
    """
    raw = str(header_value or "").strip()
    if not raw.startswith("Bearer "):
        return None, None
    raw = raw[len("Bearer "):].strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None, None
    token_id, sep, secret = raw[len(TOKEN_PREFIX):].partition(":")
    if not token_id or not sep or not secret:
        return None, None
    return token_id, secret


def validate_loopback_redirect(url):
    """Return `url` if it is a loopback address this hub may redirect a pairing code to,
    else raise PairingError.

    **This is the sharpest edge in the feature.** The redirect carries a one-time code
    that becomes a fleet credential, and the URL arrives in a query string -- so an
    unvalidated redirect here is a link that mails somebody else's console access to
    whoever crafted it. The rule is therefore an allow-list of literal loopback
    addresses (RFC 8252's native-app flow), not a pattern and not a host suffix check.

    `localhost` is refused ON PURPOSE even though it usually resolves to 127.0.0.1: it is
    a name, and a name resolves through a hosts file and a DNS server that the pairing
    flow has no reason to trust. The refusal names the fix rather than being silent about
    it, because an app author hitting this needs to change one string.
    """
    from urllib.parse import urlsplit

    raw = str(url or "").strip()
    if not raw:
        raise PairingError("A pairing redirect is required.")

    parts = urlsplit(raw)
    if parts.scheme != "http":
        raise PairingError(
            "A pairing redirect must be http:// on a loopback address.")
    if parts.username or parts.password:
        raise PairingError("A pairing redirect must not carry credentials.")
    if parts.fragment:
        raise PairingError("A pairing redirect must not carry a fragment.")

    host = parts.hostname or ""
    if host == "localhost":
        raise PairingError(
            "Use the literal address 127.0.0.1 rather than the name 'localhost' -- a "
            "name resolves through a hosts file this hub cannot vouch for.")
    if host != "::1":
        octets = host.split(".")
        if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
            raise PairingError(
                f"A pairing redirect must be a loopback address, not {host!r}.")
        if int(octets[0]) != 127:
            raise PairingError(
                f"A pairing redirect must be a loopback address, not {host!r}.")

    try:
        port = parts.port
    except ValueError:
        raise PairingError("A pairing redirect has an invalid port.")
    # An unprivileged port, because the app that listens on it is an ordinary user
    # process. A redirect aimed at a privileged port is aimed at something else.
    if port is None or not (1024 <= port <= 65535):
        raise PairingError(
            "A pairing redirect must name a port between 1024 and 65535.")

    return raw


# ================================
# PAIRING
# ================================
def create_grant(db_path, email, device_name, platform, capabilities,
                 directory_groups=(), lifetime_days=DEFAULT_LIFETIME_DAYS, now=None):
    """Record a consented pairing and return the one-time code, in plaintext, once.

    No token exists yet -- see the module docstring. The code's hash is what is stored,
    for the same reason the token's hash is: the row must not be worth stealing.
    """
    email = permissions.normalize_email(email)
    if not email:
        raise PairingError("A device must be paired to a signed-in operator.")

    capabilities = normalize_capabilities(capabilities)
    lifetime_days = max(1, int(lifetime_days or DEFAULT_LIFETIME_DAYS))
    now = int(now if now is not None else time.time())
    code = secrets.token_urlsafe(32)

    with fleet.get_conn(db_path) as conn:
        # Housekeeping on the way past. Grants are short-lived and low-volume, so there is
        # no reason for a sweeper thread to exist for them.
        conn.execute("DELETE FROM api_token_grants WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO api_token_grants(code_hash, email, device_name, platform, "
            "                             capabilities_json, directory_groups_json, "
            "                             lifetime_days, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_hash_secret(code), email,
             _clean(device_name, MAX_DEVICE_NAME_CHARS),
             _clean(platform, MAX_PLATFORM_CHARS),
             json.dumps(capabilities),
             json.dumps(sorted({str(g) for g in (directory_groups or ())})),
             lifetime_days, now, now + PAIRING_CODE_TTL_SECONDS),
        )
    return code


def redeem_grant(db_path, code, now=None):
    """Exchange a pairing code for a token. Returns (token, row).

    Single-use is enforced by the DELETE, not by a flag: the caller that gets rowcount 1
    is the one that claimed it, so two apps racing the same code cannot both be served.
    Expiry is checked AFTER the claim for the same reason -- an expired code is consumed
    rather than left for a second attempt.
    """
    now = int(now if now is not None else time.time())
    code_hash = _hash_secret(code)

    with fleet.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM api_token_grants WHERE code_hash = ?", (code_hash,)
        ).fetchone()
        if row is None:
            raise PairingError("This pairing code is not valid.")
        claimed = conn.execute(
            "DELETE FROM api_token_grants WHERE code_hash = ?", (code_hash,))
        if claimed.rowcount != 1:
            raise PairingError("This pairing code has already been used.")

    if row["expires_at"] <= now:
        raise PairingError("This pairing code has expired. Pair the device again.")

    return mint_token(
        db_path,
        email=row["email"],
        device_name=row["device_name"],
        platform=row["platform"],
        capabilities=_json_list(row["capabilities_json"]),
        directory_groups=_json_list(row["directory_groups_json"]),
        lifetime_days=row["lifetime_days"],
        now=now,
    )


def mint_token(db_path, email, device_name, platform, capabilities,
               directory_groups=(), lifetime_days=DEFAULT_LIFETIME_DAYS, now=None):
    """Create a device token and return (token, row). The plaintext is returned exactly
    once and never stored -- the device must keep it."""
    email = permissions.normalize_email(email)
    capabilities = normalize_capabilities(capabilities)
    lifetime_days = max(1, int(lifetime_days or DEFAULT_LIFETIME_DAYS))
    now = int(now if now is not None else time.time())

    token_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{token_id}:{secret}"
    expires_at = now + lifetime_days * 86400

    with fleet.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO api_tokens(token_id, token_hash, email, device_name, platform, "
            "                       capabilities_json, directory_groups_json, "
            "                       lifetime_days, created_at, last_used_at, expires_at, "
            "                       revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)",
            (token_id, _hash_secret(secret), email,
             _clean(device_name, MAX_DEVICE_NAME_CHARS),
             _clean(platform, MAX_PLATFORM_CHARS),
             json.dumps(capabilities),
             json.dumps(sorted({str(g) for g in (directory_groups or ())})),
             lifetime_days, now, expires_at),
        )

    fleet.audit(db_path, actor=email, action="device.pair",
                level=fleet.LEVEL_SECURITY, target=token_id,
                detail={"device_name": _clean(device_name, MAX_DEVICE_NAME_CHARS),
                        "platform": _clean(platform, MAX_PLATFORM_CHARS),
                        "capabilities": capabilities,
                        "expires_at": expires_at})
    return token, get_token(db_path, token_id)


# ================================
# AUTHENTICATION
# ================================
def authenticate(db_path, header_value, now=None, touch=True):
    """Resolve an Authorization header to a device identity, or None.

    The returned dict is deliberately the shape app.py's session `user` has, plus the
    device fields, so the request-identity helpers do not need to care which of the two
    ways a caller authenticated.

    Returns None -- never a reason -- for every failure: a caller holding a bad token
    learns that it is bad, and nothing else. Whether a token id exists is not something an
    unauthenticated caller gets to probe.
    """
    token_id, secret = parse_authorization(header_value)
    if not token_id:
        return None
    now = int(now if now is not None else time.time())

    with fleet.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM api_tokens WHERE token_id = ?", (token_id,)).fetchone()
        if row is None or row["revoked"]:
            return None
        if not hmac.compare_digest(row["token_hash"], _hash_secret(secret)):
            return None
        if row["expires_at"] <= now:
            return None

        first_use = row["last_used_at"] is None
        if touch and (first_use or now - row["last_used_at"] >= TOUCH_INTERVAL_SECONDS):
            # The expiry slides against the row's OWN lifetime, not the current fleet
            # default: a device paired under a 30-day policy stays a 30-day device even
            # after someone widens the default to 90.
            conn.execute(
                "UPDATE api_tokens SET last_used_at = ?, expires_at = ? WHERE token_id = ?",
                (now, now + row["lifetime_days"] * 86400, token_id))

    if first_use:
        # Worth its own row: the gap between "a token was minted" and "a device started
        # using it" is where a code that leaked in transit would show up.
        fleet.audit(db_path, actor=row["email"], action="device.first_use",
                    level=fleet.LEVEL_SECURITY, target=token_id,
                    detail={"device_name": row["device_name"],
                            "platform": row["platform"]})

    return {
        "email": row["email"],
        "name": row["device_name"] or row["email"],
        "directory_groups": _json_list(row["directory_groups_json"]),
        # The subset this device may use. permissions_web.Access intersects it with the
        # owner's LIVE capabilities on every request -- this is a ceiling, not a grant.
        "token_capabilities": _json_list(row["capabilities_json"]),
        "token_id": token_id,
        "device_name": row["device_name"],
        "platform": row["platform"],
    }


# ================================
# ADMINISTRATION
# ================================
def _row(row):
    """One device, as the console shows it. The token itself appears nowhere -- there is
    no 'reveal', because the hub does not have it to reveal."""
    return {
        "token_id": row["token_id"],
        "email": row["email"],
        "device_name": row["device_name"],
        "platform": row["platform"],
        "capabilities": _json_list(row["capabilities_json"]),
        "lifetime_days": row["lifetime_days"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "expires_at": row["expires_at"],
        "revoked": bool(row["revoked"]),
    }


def get_token(db_path, token_id):
    with fleet.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM api_tokens WHERE token_id = ?", (str(token_id),)).fetchone()
    return _row(row) if row is not None else None


def list_tokens(db_path, email=None, include_revoked=False):
    """Paired devices, newest first. `email` narrows to one operator's own devices --
    which is the self-service path, and the reason it is a parameter rather than a filter
    the web layer applies afterwards."""
    sql = "SELECT * FROM api_tokens"
    where, params = [], []
    if email:
        where.append("email = ?")
        params.append(permissions.normalize_email(email))
    if not include_revoked:
        where.append("revoked = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rowid DESC"
    with fleet.get_conn(db_path) as conn:
        return [_row(r) for r in conn.execute(sql, params).fetchall()]


def revoke_token(db_path, token_id, actor, email=None):
    """Revoke one device. Returns True if a row was actually revoked.

    `email` scopes the revoke to one owner, for the self-service path: passing it means a
    token id belonging to somebody else is a miss rather than a revoke. The check is in
    the UPDATE rather than a read-then-write, so it cannot be raced.
    """
    sql = "UPDATE api_tokens SET revoked = 1 WHERE token_id = ? AND revoked = 0"
    params = [str(token_id)]
    if email:
        sql += " AND email = ?"
        params.append(permissions.normalize_email(email))

    with fleet.get_conn(db_path) as conn:
        changed = conn.execute(sql, params).rowcount

    if changed:
        fleet.audit(db_path, actor=actor or "unknown", action="device.revoke",
                    level=fleet.LEVEL_SECURITY, target=str(token_id))
    return bool(changed)


def revoke_all_for_email(db_path, email, actor):
    """Revoke every device belonging to one operator. The lever for a departure -- a
    permission-group removal already stops the token working (capabilities are intersected
    live), but a revoked device also stops holding a credential at all."""
    email = permissions.normalize_email(email)
    with fleet.get_conn(db_path) as conn:
        changed = conn.execute(
            "UPDATE api_tokens SET revoked = 1 WHERE email = ? AND revoked = 0",
            (email,)).rowcount
    if changed:
        fleet.audit(db_path, actor=actor or "unknown", action="device.revoke",
                    level=fleet.LEVEL_SECURITY, target=email,
                    detail={"devices": changed})
    return changed
