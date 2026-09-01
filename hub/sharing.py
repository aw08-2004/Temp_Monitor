"""Cross-hub machine sharing -- lend ONE machine to somebody else's hub (roadmap #15).

A colleague running his own FleetHub wants a second pair of eyes on his PC. He shares
**that one machine** with another hub, granting only remote view; it appears in the
borrower's console and does exactly that and nothing else. Widen the grant and it does
more.

The requirement that shapes the whole design is the negative one: **the borrowing hub must
never be able to repoint the agent** -- not its `HUB_URL`, not its `AGENT_ENROLLMENT_SECRET`.

That guarantee comes from REMOVING THE CHANNEL, not from adding a check. An agent has
exactly one home hub, and the borrowing hub never speaks to it:

    hub B (borrower)  --peer token-->  hub A (owner)  --ordinary command queue-->  agent

Hub A validates the grant and queues the work on **its own** queue to **its own** agent.
The agent is never told hub B exists, so there is no permission to get wrong and no request
to filter: a fully compromised borrowing hub can still only ask hub A for actions inside the
grant, and neither the hub URL nor the enrollment secret is an agent command verb in the
first place. Both are install-time config on the machine, which is why `settings.py` refuses
to hold secrets at all and `fleet.py` already says an agent must ignore anything that would
redirect it.

**A shared machine is never enrolled on the borrowing hub.** Nothing here writes a row in
`machines`, `machine_info` or any enrollment table -- borrowed machines live in their own
cache table, are excluded from fleet-wide operations and counts, and are badged as borrowed
everywhere they appear. It is somebody else's machine seen through a window, and the console
should never let that blur.

`apitokens.py` is the pattern, one level up -- a peer hub takes the device's place:

  * **A grant holds a SUBSET of its creator's capabilities, intersected LIVE.** A share
    minted while somebody was privileged must not outlive their privileges, which is the
    failure mode a long-lived credential introduces and a session does not. See
    `effective_share_capabilities`, and note the one departure from apitokens: a lapsed
    share is REFUSED BY NAME rather than quietly doing less, because the creator's access
    can lapse for a reason nobody would otherwise see (below).
  * **The pairing is a single-use expiring code, and no token exists until it is redeemed.**
    `create_pairing` -> `redeem_pairing` -> a peer token, exactly like create_grant ->
    redeem_grant -> mint_token. An abandoned pairing leaves no credential behind.
  * **Nothing but the hash is stored**, so neither table is worth stealing.

Three places this deliberately differs from `apitokens.py`, each because a peer hub is
further away than a laptop belonging to one of your own operators:

  1. **Shareable capabilities are an ALLOW-LIST, not a deny-list.** `apitokens` names four
     forbidden capabilities and permits the rest, which is right for a device whose owner is
     your colleague: a capability added later is one they could already exercise in the
     console anyway. A peer hub is a different organisation's console, so the list runs the
     other way -- `SHAREABLE_CAPABILITIES` is closed, and a capability added to
     `permissions.CAPABILITIES` next year is *not* shareable until somebody decides it is.
     Failing closed is the whole point; `tests/test_sharing.py` asserts every capability is
     classified one way or the other.
  2. **Only three capabilities are shareable at all**, and that is a statement about what a
     share IS. `view`, `issue_commands` and `remote_control` are the three that mean
     something about ONE MACHINE. Everything else on the list -- approvals, maintenance
     windows, backup destinations, the package library, rules, the audit trail -- is a
     statement about hub A's own configuration, and there is no per-machine slice of it to
     hand over. Sharing a machine is not a way into a hub.
  3. **Nothing rides along implicitly -- the owner picks each capability.** In particular
     `view` does not travel with `remote_control`. A remote-view-only share shows a stub:
     hostname, online state, a Connect button, and no telemetry at all. `view` is its own
     toggle everywhere else in this product, and quietly bundling it here would make the
     smallest possible share bigger than "look at my screen", which is the exact case the
     feature exists for.

**A lapsed share is refused by name.** `permissions.effective_permissions` resolves stored
group membership; a grant reached through a mapped DIRECTORY group is session-scoped by
design (the hub never queries the directory, it believes what the issuer signed at sign-in).
So a share created by somebody whose access to that machine came only from a directory group
would intersect to nothing the moment their session ended. Quietly serving a smaller grant
would make that invisible -- an operator on hub B would see a machine that had simply stopped
working. Instead the share reports `lapsed` with the capabilities its creator no longer
holds, hub A's console shows it as suspended, and the peer's request is refused with the
reason. A failure somebody can read is worth more than one that degrades politely.

**Telemetry is proxied live, never mirrored.** Revocation is immediate and must kill sessions
in flight, and a mirrored copy of somebody else's fleet data would outlive the grant that
justified it. `borrowed_machines` is a RENDERING CACHE and nothing else: it holds what the
last successful catalogue read said, so that a borrowing hub whose peer is unreachable can
show the machine badged with when it was last heard from rather than dropping it off the page
-- and every action refuses, naming the owner hub, rather than failing generically.

**One share is one machine.** A peer link may carry many shares, but the share row is the
unit of grant, of audit and of revocation. Sharing two machines with the same colleague is
two decisions, and revoking one is not a re-negotiation of the other.

Kept free of Flask so it can be unit-tested in isolation, exactly like `fleet.py`,
`permissions.py` and `apitokens.py`. Authorization decisions live here (they are model
questions); session handling and HTTP live in `sharing_web.py`.
"""
import hashlib
import hmac
import json
import secrets
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import fleet
import permissions

# ================================
# VOCABULARY
# ================================
#: Distinguishes a PEER HUB token from the two credentials that already arrive in the same
#: header: `tmu_` is a user device (apitokens.py) and a bare `<agent_id>:<token>` is an
#: agent. Each must be a parse failure at the other's door rather than a lookup that
#: happens to miss -- see apitokens.TOKEN_PREFIX, which this deliberately mirrors.
TOKEN_PREFIX = "tmh_"

#: How long a pairing code is worth anything. Far longer than apitokens' sixty seconds, and
#: for a concrete reason: that code is caught by a loopback listener the same second it is
#: issued, while this one is read out to a colleague who then walks to another console and
#: pastes it. Fifteen minutes is a phone call. It is still single-use and still hashed.
PAIRING_CODE_TTL_SECONDS = 15 * 60

#: What a peer token is worth once minted. A share carries its own optional expiry (see
#: `create_share`); this is the outer bound on the LINK, so a pairing everybody forgot about
#: does not stay a live credential forever. Slides on use, like a device token's.
DEFAULT_PEER_LIFETIME_DAYS = 365

#: Only touch `last_seen_at` (and slide the expiry) when the row is at least this stale. A
#: borrowing hub polls the catalogue; writing a row per poll would put a steady write load
#: on the same WAL database telemetry lands in, to maintain a column only a human-readable
#: peer list reads. Same reason and same value as apitokens.TOUCH_INTERVAL_SECONDS.
TOUCH_INTERVAL_SECONDS = 60

#: A borrowed machine whose catalogue entry is older than this is shown as stale -- the
#: owner hub has not answered recently. Three missed one-minute polls: long enough that a
#: single slow request does not flap the badge, short enough that "unreachable" means it.
BORROWED_STALE_SECONDS = 180

#: What a share may EVER carry. An allow-list, not a deny-list -- see the module docstring
#: for why this is the opposite shape to apitokens.DEVICE_FORBIDDEN_CAPABILITIES. Order is
#: permissions.CAPABILITIES order, so a stored set and a displayed set cannot disagree.
SHAREABLE_CAPABILITIES = (
    permissions.VIEW,
    permissions.ISSUE_COMMANDS,
    permissions.REMOTE_CONTROL,
)

#: What the share dialog ticks by default: the case the feature exists for, and nothing
#: else. Deliberately NOT `view` as well -- see the module docstring.
DEFAULT_SHARE_CAPABILITIES = (permissions.REMOTE_CONTROL,)

#: Labels are shown in a peer list and beside a borrowed machine's hostname, so they only
#: have to be readable.
MAX_LABEL_CHARS = 64

#: A hub that is lending out its whole fleet is not using this feature, it is merging two
#: fleets. Bounds the catalogue a single link can produce.
MAX_SHARES_PER_PEER = 200


class SharingError(Exception):
    """A sharing request that cannot be honoured, with a message meant for a human."""


# ================================
# SCHEMA
# ================================
def init_sharing_db(db_path):
    """Create the sharing tables if absent. Idempotent -- safe to call on every hub start
    beside the other init_*_db functions.

    Note which side of the relationship each table serves. One hub is usually both: it
    lends some machines and borrows others, and there is no mode switch anywhere.
    """
    with fleet.get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        # -- OWNER SIDE ------------------------------------------------------------
        # A pairing code that has been generated but not yet redeemed. Holds no secret of
        # its own beyond the code's hash: the peer token is minted at redemption.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS share_pairings (
                code_hash   TEXT PRIMARY KEY,
                label       TEXT NOT NULL DEFAULT '',
                created_by  TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL
            )
        """)

        # One row per PAIRED PEER HUB. `label` is what this hub's operator called it; the
        # peer additionally tells us what it calls itself at redemption (`peer_label`), and
        # the two are kept apart on purpose -- one is a local note and the other is a claim
        # by the far side, which is not the same kind of fact.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS share_peers (
                peer_id       TEXT PRIMARY KEY,
                token_hash    TEXT NOT NULL,
                label         TEXT NOT NULL DEFAULT '',
                peer_label    TEXT NOT NULL DEFAULT '',
                created_by    TEXT NOT NULL,
                lifetime_days INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,
                last_seen_at  INTEGER,
                expires_at    INTEGER NOT NULL,
                revoked       INTEGER NOT NULL DEFAULT 0
            )
        """)

        # One machine, lent to one peer, with the capabilities the owner ticked.
        # `created_by` is load-bearing rather than decorative: it is whose live permissions
        # the grant is intersected against on every request. See
        # effective_share_capabilities.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                share_id          TEXT PRIMARY KEY,
                peer_id           TEXT NOT NULL,
                machine           TEXT NOT NULL,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                created_by        TEXT NOT NULL,
                created_at        INTEGER NOT NULL,
                expires_at        INTEGER,
                revoked           INTEGER NOT NULL DEFAULT 0,
                revoked_at        INTEGER
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shares_peer ON shares(peer_id, revoked)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shares_machine ON shares(machine, revoked)")

        # -- BORROWER SIDE ---------------------------------------------------------
        # An outbound link to a hub that has lent us something. The peer TOKEN is not here:
        # it is a live credential to another system, so it lives in the master-key-wrapped
        # secret store beside the backup destination credentials and the BIOS setup
        # password, keyed on link_id. Same rule as backups.py -- a credential does not go in
        # a table the console renders.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS share_links (
                link_id       TEXT PRIMARY KEY,
                base_url      TEXT NOT NULL,
                label         TEXT NOT NULL DEFAULT '',
                peer_id       TEXT NOT NULL DEFAULT '',
                created_by    TEXT NOT NULL,
                created_at    INTEGER NOT NULL,
                last_ok_at    INTEGER,
                last_error    TEXT,
                last_error_at INTEGER
            )
        """)

        # The rendering cache described in the module docstring. NOT a mirror, NOT authority,
        # and deliberately not joined to anything in the fleet tables -- a borrowed machine
        # has no row there and must never acquire one.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS borrowed_machines (
                link_id           TEXT NOT NULL,
                share_id          TEXT NOT NULL,
                hostname          TEXT NOT NULL DEFAULT '',
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                online            INTEGER NOT NULL DEFAULT 0,
                last_seen         INTEGER,
                expires_at        INTEGER,
                lapsed            INTEGER NOT NULL DEFAULT 0,
                cached_at         INTEGER NOT NULL,
                PRIMARY KEY (link_id, share_id)
            )
        """)


# ================================
# HELPERS
# ================================
def _hash_secret(value):
    """Store only the hash, so a database leak hands out no live credential.

    Deliberately the SAME rule as fleet._hash_token and apitokens._hash_secret (sha256 of
    the utf-8 bytes, hex): three token stores that hash differently is three things to get
    right, and there is no reason for them to differ.
    """
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _clean(value, limit=MAX_LABEL_CHARS):
    return str(value or "").strip()[:limit]


def _json_list(raw):
    try:
        loaded = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in loaded] if isinstance(loaded, list) else []


def normalize_share_capabilities(requested):
    """Validate a requested capability set, or raise SharingError naming the problem.

    Returns them in SHAREABLE_CAPABILITIES order (which is permissions.CAPABILITIES order),
    so a stored set and a displayed set cannot disagree about ordering and two identical
    shares compare equal.

    The refusal distinguishes "there is no such capability" from "that capability exists and
    is not shareable", because those are different mistakes and the second one has an
    explanation an operator deserves to read.
    """
    wanted = {str(c or "").strip() for c in (requested or ())}
    wanted.discard("")
    if not wanted:
        raise SharingError("A share needs at least one capability.")

    unknown = sorted(w for w in wanted if w not in permissions.CAPABILITIES)
    if unknown:
        raise SharingError(f"Unknown capability: {', '.join(unknown)}.")

    not_shareable = sorted(w for w in wanted if w not in SHAREABLE_CAPABILITIES)
    if not_shareable:
        raise SharingError(
            "These capabilities cannot be shared with another hub, because they are "
            "statements about this hub's own configuration rather than about one "
            f"machine: {', '.join(not_shareable)}.")

    return [c for c in SHAREABLE_CAPABILITIES if c in wanted]


def parse_peer_authorization(header_value):
    """Split 'Bearer tmh_<peer_id>:<secret>' into (peer_id, secret), or (None, None).

    Returns (None, None) for a USER token and for an AGENT token as well as for junk -- see
    the module docstring. The caller must not fall back to trying it as anything else.
    """
    raw = str(header_value or "").strip()
    if not raw.startswith("Bearer "):
        return None, None
    raw = raw[len("Bearer "):].strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None, None
    peer_id, sep, secret = raw[len(TOKEN_PREFIX):].partition(":")
    if not peer_id or not sep or not secret:
        return None, None
    return peer_id, secret


def normalize_peer_url(url):
    """Return `url` reduced to the origin this hub will send a peer token to, or raise.

    **This is the sharpest edge on the borrowing side.** Whatever comes back from here is
    where a long-lived credential to somebody else's fleet gets sent, on every poll, in a
    header. So it is validated the way apitokens.validate_loopback_redirect validates its
    redirect -- by rules that are stated rather than by a pattern that happens to match.

    `https` only, with no exception for a hub on the LAN. A peer link is the one place in
    this product where a credential leaves the hub to an address an operator typed, and TLS
    is what stops the hop in between reading it. The hub's own `HUB_URL` is required to be
    https for the same reason, so this asks a peer for nothing this hub does not already do.

    A query string or fragment is refused rather than stripped: both mean the operator
    pasted something other than a hub address -- usually a whole console URL -- and silently
    keeping the part that parses is how a peer link ends up pointing somewhere surprising.
    """
    raw = str(url or "").strip()
    if not raw:
        raise SharingError("A peer hub address is required.")
    if "://" not in raw:
        raw = "https://" + raw

    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise SharingError(
            "A peer hub address must be https:// -- the peer token travels in a header, "
            "and there is nothing else protecting it in transit.")
    if parts.username or parts.password:
        raise SharingError("A peer hub address must not carry credentials.")
    if parts.query or parts.fragment:
        raise SharingError(
            "A peer hub address must be just the hub's address, with no query string or "
            "fragment. Paste the address bar up to the host name, not a whole page URL.")
    if not parts.hostname:
        raise SharingError("A peer hub address must name a host.")
    try:
        parts.port
    except ValueError:
        raise SharingError("A peer hub address has an invalid port.")

    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/"), "", ""))


# ================================
# OWNER SIDE -- PAIRING
# ================================
def create_pairing(db_path, label, created_by, ttl_seconds=PAIRING_CODE_TTL_SECONDS,
                   now=None):
    """Generate a one-time pairing code and return it in plaintext, once.

    No peer and no token exist yet. An abandoned pairing therefore leaves no credential
    behind -- which is the whole reason this is a separate call from `redeem_pairing`, and
    the same reason apitokens splits create_grant from mint_token.
    """
    created_by = permissions.normalize_email(created_by)
    if not created_by:
        raise SharingError("A pairing must be created by a signed-in operator.")

    now = int(now if now is not None else time.time())
    code = secrets.token_urlsafe(32)

    with fleet.get_conn(db_path) as conn:
        # Housekeeping on the way past. Pairings are short-lived and low-volume, so there is
        # no reason for a sweeper thread to exist for them.
        conn.execute("DELETE FROM share_pairings WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO share_pairings(code_hash, label, created_by, created_at, "
            "                           expires_at) VALUES (?, ?, ?, ?, ?)",
            (_hash_secret(code), _clean(label), created_by, now,
             now + max(60, int(ttl_seconds))),
        )
    fleet.audit(db_path, actor=created_by, action="share.pair_offer",
                level=fleet.LEVEL_SECURITY, target=_clean(label) or None)
    return code


def redeem_pairing(db_path, code, peer_label="", lifetime_days=DEFAULT_PEER_LIFETIME_DAYS,
                   now=None):
    """Exchange a pairing code for a peer token. Returns (token, peer_row).

    Single-use is enforced by the DELETE rather than by a flag: the caller that gets
    rowcount 1 is the one that claimed it, so two hubs racing the same code cannot both be
    served. Expiry is checked AFTER the claim, for the same reason apitokens does -- an
    expired code is consumed rather than left lying around for a second attempt.
    """
    now = int(now if now is not None else time.time())
    code_hash = _hash_secret(code)

    with fleet.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM share_pairings WHERE code_hash = ?", (code_hash,)).fetchone()
        if row is None:
            raise SharingError("This pairing code is not valid.")
        claimed = conn.execute(
            "DELETE FROM share_pairings WHERE code_hash = ?", (code_hash,))
        if claimed.rowcount != 1:
            raise SharingError("This pairing code has already been used.")

    if row["expires_at"] <= now:
        raise SharingError("This pairing code has expired. Generate another one.")

    return mint_peer_token(db_path, label=row["label"], peer_label=peer_label,
                           created_by=row["created_by"], lifetime_days=lifetime_days,
                           now=now)


def mint_peer_token(db_path, label, peer_label="", created_by="",
                    lifetime_days=DEFAULT_PEER_LIFETIME_DAYS, now=None):
    """Create a peer hub's token and return (token, row). The plaintext is returned exactly
    once and never stored -- the borrowing hub must keep it."""
    lifetime_days = max(1, int(lifetime_days or DEFAULT_PEER_LIFETIME_DAYS))
    now = int(now if now is not None else time.time())

    peer_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{peer_id}:{secret}"
    expires_at = now + lifetime_days * 86400
    created_by = permissions.normalize_email(created_by)

    with fleet.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO share_peers(peer_id, token_hash, label, peer_label, created_by, "
            "                        lifetime_days, created_at, last_seen_at, expires_at, "
            "                        revoked) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)",
            (peer_id, _hash_secret(secret), _clean(label), _clean(peer_label), created_by,
             lifetime_days, now, expires_at),
        )

    fleet.audit(db_path, actor=created_by or "unknown", action="share.peer_paired",
                level=fleet.LEVEL_SECURITY, target=peer_id,
                detail={"label": _clean(label), "peer_label": _clean(peer_label),
                        "expires_at": expires_at})
    return token, get_peer(db_path, peer_id)


def authenticate_peer(db_path, header_value, now=None, touch=True):
    """Resolve an Authorization header to a peer hub, or None.

    Returns None -- never a reason -- for every failure, exactly like apitokens.authenticate:
    a caller holding a bad token learns that it is bad and nothing else. Whether a peer id
    exists is not something an unauthenticated caller gets to probe.

    Note what this does NOT return: capabilities. A peer holds none by itself; every
    capability it has is attached to one share and is resolved per request by
    `authorize_peer_action`. There is deliberately no "what may this peer do" question that
    can be asked without naming a machine.
    """
    peer_id, secret = parse_peer_authorization(header_value)
    if not peer_id:
        return None
    now = int(now if now is not None else time.time())

    with fleet.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM share_peers WHERE peer_id = ?", (peer_id,)).fetchone()
        if row is None or row["revoked"]:
            return None
        if not hmac.compare_digest(row["token_hash"], _hash_secret(secret)):
            return None
        if row["expires_at"] <= now:
            return None

        first_use = row["last_seen_at"] is None
        if touch and (first_use or now - row["last_seen_at"] >= TOUCH_INTERVAL_SECONDS):
            # The expiry slides against the row's OWN lifetime, not the current default: a
            # peer paired under a 90-day policy stays a 90-day peer after somebody widens
            # the default. Same rule as apitokens, and packages' retry policy before it.
            conn.execute(
                "UPDATE share_peers SET last_seen_at = ?, expires_at = ? "
                "WHERE peer_id = ?",
                (now, now + row["lifetime_days"] * 86400, peer_id))

    if first_use:
        # Worth its own row: the gap between "a code was issued" and "a hub started using
        # the token" is where a pairing code that leaked in transit would show up.
        fleet.audit(db_path, actor=row["created_by"] or "unknown",
                    action="share.peer_first_use", level=fleet.LEVEL_SECURITY,
                    target=peer_id, detail={"label": row["label"]})

    return {"peer_id": peer_id, "label": row["label"], "peer_label": row["peer_label"],
            "created_by": row["created_by"]}


def _peer_row(row):
    return {
        "peer_id": row["peer_id"],
        "label": row["label"],
        "peer_label": row["peer_label"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "expires_at": row["expires_at"],
        "revoked": bool(row["revoked"]),
    }


def get_peer(db_path, peer_id):
    with fleet.get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM share_peers WHERE peer_id = ?",
                           (str(peer_id),)).fetchone()
    return _peer_row(row) if row is not None else None


def list_peers(db_path, include_revoked=False):
    sql = "SELECT * FROM share_peers"
    if not include_revoked:
        sql += " WHERE revoked = 0"
    sql += " ORDER BY created_at DESC, rowid DESC"
    with fleet.get_conn(db_path) as conn:
        return [_peer_row(r) for r in conn.execute(sql).fetchall()]


def revoke_peer(db_path, peer_id, actor, now=None):
    """Revoke a peer hub and every share it holds. Returns how many shares went with it, or
    None if there was no live peer to revoke.

    Both halves in one transaction, and the shares are revoked rather than left dangling
    behind a dead token: a share is a grant to a named peer, and re-pairing that colleague
    tomorrow must not silently reconnect them to yesterday's machines.
    """
    now = int(now if now is not None else time.time())
    with fleet.get_conn(db_path) as conn:
        changed = conn.execute(
            "UPDATE share_peers SET revoked = 1 WHERE peer_id = ? AND revoked = 0",
            (str(peer_id),)).rowcount
        if not changed:
            return None
        shares = conn.execute(
            "UPDATE shares SET revoked = 1, revoked_at = ? "
            "WHERE peer_id = ? AND revoked = 0", (now, str(peer_id))).rowcount

    fleet.audit(db_path, actor=actor or "unknown", action="share.peer_revoke",
                level=fleet.LEVEL_SECURITY, target=str(peer_id),
                detail={"shares_revoked": shares})
    return shares


# ================================
# OWNER SIDE -- SHARES
# ================================
def _share_row(row):
    return {
        "share_id": row["share_id"],
        "peer_id": row["peer_id"],
        "machine": row["machine"],
        "capabilities": _json_list(row["capabilities_json"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked": bool(row["revoked"]),
        "revoked_at": row["revoked_at"],
    }


def create_share(db_path, peer_id, machine, capabilities, created_by, expires_at=None,
                 now=None):
    """Lend one machine to one peer. Returns the share row.

    `expires_at` is optional and both shapes are real: a few hours for "come look at this",
    none at all for two hubs that habitually cooperate. There is no default expiry, because
    guessing one would make the second case quietly stop working on a Tuesday.
    """
    machine = permissions.normalize_machine(machine)
    if not machine:
        raise SharingError("A share must name a machine.")
    created_by = permissions.normalize_email(created_by)
    if not created_by:
        raise SharingError("A share must be created by a signed-in operator.")

    capabilities = normalize_share_capabilities(capabilities)
    now = int(now if now is not None else time.time())
    if expires_at is not None:
        expires_at = int(expires_at)
        if expires_at <= now:
            raise SharingError("A share's expiry must be in the future.")

    peer = get_peer(db_path, peer_id)
    if peer is None or peer["revoked"]:
        raise SharingError("That peer hub is not paired with this one.")

    with fleet.get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT share_id FROM shares WHERE peer_id = ? AND machine = ? AND revoked = 0",
            (peer["peer_id"], machine)).fetchone()
        if existing is not None:
            # Edit the live grant rather than stacking a second one. Two rows for the same
            # (peer, machine) would make "what may they do" a union nobody authored, and
            # revoking one of them would look like it had done nothing.
            raise SharingError(
                "This machine is already shared with that hub. Edit or revoke that share "
                "instead of creating a second one.")
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM shares WHERE peer_id = ? AND revoked = 0",
            (peer["peer_id"],)).fetchone()["n"]
        if count >= MAX_SHARES_PER_PEER:
            raise SharingError(
                f"That hub already has {MAX_SHARES_PER_PEER} machines shared with it.")

        share_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO shares(share_id, peer_id, machine, capabilities_json, created_by, "
            "                   created_at, expires_at, revoked, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)",
            (share_id, peer["peer_id"], machine, json.dumps(capabilities), created_by,
             now, expires_at),
        )

    fleet.audit(db_path, actor=created_by, action="share.create",
                level=fleet.LEVEL_SECURITY, target=machine,
                detail={"share_id": share_id, "peer_id": peer["peer_id"],
                        "peer_label": peer["label"], "capabilities": capabilities,
                        "expires_at": expires_at})
    return get_share(db_path, share_id)


def update_share(db_path, share_id, capabilities=None, expires_at=..., actor="unknown",
                 now=None):
    """Change what a live share carries. Returns the updated row, or None if there is none.

    `expires_at` uses an Ellipsis sentinel rather than None, because None is a MEANING here
    -- "no expiry" -- and a caller clearing an expiry must be distinguishable from a caller
    not mentioning it.
    """
    now = int(now if now is not None else time.time())
    share = get_share(db_path, share_id)
    if share is None or share["revoked"]:
        return None

    sets, params, detail = [], [], {}
    if capabilities is not None:
        capabilities = normalize_share_capabilities(capabilities)
        sets.append("capabilities_json = ?")
        params.append(json.dumps(capabilities))
        detail["capabilities"] = capabilities
    if expires_at is not ...:
        if expires_at is not None:
            expires_at = int(expires_at)
            if expires_at <= now:
                raise SharingError("A share's expiry must be in the future.")
        sets.append("expires_at = ?")
        params.append(expires_at)
        detail["expires_at"] = expires_at
    if not sets:
        return share

    params.append(str(share_id))
    with fleet.get_conn(db_path) as conn:
        conn.execute(f"UPDATE shares SET {', '.join(sets)} WHERE share_id = ?", params)

    detail["share_id"] = str(share_id)
    detail["peer_id"] = share["peer_id"]
    fleet.audit(db_path, actor=actor or "unknown", action="share.update",
                level=fleet.LEVEL_SECURITY, target=share["machine"], detail=detail)
    return get_share(db_path, share_id)


def get_share(db_path, share_id):
    with fleet.get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM shares WHERE share_id = ?",
                           (str(share_id),)).fetchone()
    return _share_row(row) if row is not None else None


def list_shares(db_path, peer_id=None, machine=None, include_revoked=False):
    """Shares this hub has granted, newest first."""
    sql, where, params = "SELECT * FROM shares", [], []
    if peer_id:
        where.append("peer_id = ?")
        params.append(str(peer_id))
    if machine:
        where.append("machine = ?")
        params.append(permissions.normalize_machine(machine))
    if not include_revoked:
        where.append("revoked = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rowid DESC"
    with fleet.get_conn(db_path) as conn:
        return [_share_row(r) for r in conn.execute(sql, params).fetchall()]


def revoke_share(db_path, share_id, actor, now=None):
    """Revoke one share. Returns True if a row was actually revoked.

    The check is in the UPDATE rather than a read-then-write, so it cannot be raced, and the
    revocation takes effect on the next peer request without any cache to invalidate --
    `authorize_peer_action` reads this row every time. Sessions already in flight are killed
    by the caller (see sharing_web.py), which is the half a row cannot do by itself.
    """
    now = int(now if now is not None else time.time())
    share = get_share(db_path, share_id)
    with fleet.get_conn(db_path) as conn:
        changed = conn.execute(
            "UPDATE shares SET revoked = 1, revoked_at = ? WHERE share_id = ? "
            "AND revoked = 0", (now, str(share_id))).rowcount
    if changed and share is not None:
        fleet.audit(db_path, actor=actor or "unknown", action="share.revoke",
                    level=fleet.LEVEL_SECURITY, target=share["machine"],
                    detail={"share_id": share["share_id"], "peer_id": share["peer_id"]})
    return bool(changed)


def forget_machine(db_path, machine):
    """Revoke every share of a machine that has left this fleet. Returns how many.

    Called from the same machine-lifecycle seam `permissions.forget_machine` is, and for the
    same reason: a machine that has been deleted or merged away must not stay lent out under
    a name that no longer resolves to anything.
    """
    machine = permissions.normalize_machine(machine)
    now = int(time.time())
    with fleet.get_conn(db_path) as conn:
        changed = conn.execute(
            "UPDATE shares SET revoked = 1, revoked_at = ? WHERE machine = ? "
            "AND revoked = 0", (now, machine)).rowcount
    if changed:
        fleet.audit(db_path, actor="system", action="share.revoke",
                    level=fleet.LEVEL_SECURITY, target=machine,
                    detail={"reason": "machine removed", "shares_revoked": changed})
    return changed


def rename_machine(db_path, old_machine, new_machine):
    """Follow a machine through a duplicate-serial merge, so a live share does not silently
    stop resolving. Mirrors permissions.rename_machine."""
    old_machine = permissions.normalize_machine(old_machine)
    new_machine = permissions.normalize_machine(new_machine)
    if not old_machine or not new_machine or old_machine == new_machine:
        return 0
    with fleet.get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE shares SET machine = ? WHERE machine = ?",
            (new_machine, old_machine)).rowcount


# ================================
# OWNER SIDE -- AUTHORIZATION
# ================================
def effective_share_capabilities(db_path, share, superusers=()):
    """What this share may ACTUALLY do right now: (granted, lapsed).

    The grant is intersected LIVE against the creator's own permissions, which is the whole
    reason `created_by` is stored. Without it, a share created while somebody was privileged
    would keep working after they were demoted or left -- the credential outliving the grant,
    which is precisely the failure mode a standing grant introduces and a session does not.

    `lapsed` is what the share was given and its creator no longer holds. It is returned
    rather than silently dropped because the caller must be able to REFUSE BY NAME: see the
    module docstring for why a politely-degraded share is worse than a loud one here.

    Losing scope over the machine lapses the WHOLE share, not part of it -- there is no
    capability that survives the creator no longer being allowed to see the machine.
    """
    capabilities = list((share or {}).get("capabilities") or ())
    creator = (share or {}).get("created_by") or ""
    live = permissions.effective_permissions(db_path, creator, superusers=superusers)
    if not permissions.machine_in_scope(live, (share or {}).get("machine")):
        return [], capabilities
    held = live.get("capabilities") or set()
    granted = [c for c in capabilities if c in held]
    lapsed = [c for c in capabilities if c not in held]
    return granted, lapsed


def share_state(db_path, share, superusers=(), now=None):
    """One share as both hubs need to understand it: the row, plus what it can do today.

    Adds `granted`, `lapsed`, `expired` and `live`. `live` is the single question every
    caller actually asks -- not revoked, not expired, and with at least one capability
    surviving the intersection.
    """
    now = int(now if now is not None else time.time())
    granted, lapsed = effective_share_capabilities(db_path, share, superusers)
    expired = bool(share.get("expires_at")) and share["expires_at"] <= now
    state = dict(share)
    state["granted"] = granted
    state["lapsed"] = lapsed
    state["expired"] = expired
    state["live"] = bool(granted) and not expired and not share.get("revoked")
    return state


def authorize_peer_action(db_path, peer, share_id, capability, superusers=(), now=None):
    """The one door every peer request goes through. Returns (share_state, error).

    Exactly one of the two is None. The error is a sentence meant to be returned to the
    borrowing hub, and it is deliberately specific for a share this peer holds and generic
    for one it does not: which shares exist is not something a peer gets to enumerate by
    guessing ids, but why its OWN share stopped working is something it needs to be able to
    read without phoning the owner.
    """
    peer_id = (peer or {}).get("peer_id")
    if not peer_id:
        return None, "Not authorized."

    share = get_share(db_path, share_id)
    if share is None or share["peer_id"] != peer_id or share["revoked"]:
        return None, "No such share."

    state = share_state(db_path, share, superusers=superusers, now=now)
    if state["expired"]:
        return None, "This share has expired."
    if capability in state["lapsed"]:
        return None, (
            f"The operator who shared this machine no longer holds '{capability}' on it, "
            "so this share is suspended until they do.")
    if capability not in state["granted"]:
        return None, f"This share does not carry '{capability}'."
    return state, None


def catalogue_for_peer(db_path, peer, superusers=(), now=None, roster=None):
    """What a borrowing hub is told it has: one entry per live share.

    `roster` is the {machine: {...}} mapping the caller supplies -- the same roster the
    backup, firmware and wake schedulers use -- so this module needs to know nothing about
    how "online" is decided. A machine missing from it is reported offline rather than
    dropped: a share whose machine has not checked in is still a share, and the borrowing hub
    showing it as offline is more useful than it disappearing.

    A share whose grant has LAPSED is listed, marked, and carries no capabilities. The
    borrowing hub can then say why the machine stopped working instead of quietly losing it.
    """
    now = int(now if now is not None else time.time())
    roster = roster or {}
    entries = []
    for share in list_shares(db_path, peer_id=(peer or {}).get("peer_id")):
        state = share_state(db_path, share, superusers=superusers, now=now)
        if state["expired"]:
            continue
        info = roster.get(share["machine"]) or {}
        entries.append({
            "share_id": share["share_id"],
            "hostname": share["machine"],
            "capabilities": state["granted"],
            "lapsed": bool(state["lapsed"]),
            "online": bool(info.get("online")),
            "last_seen": info.get("last_seen"),
            "expires_at": share["expires_at"],
        })
    return entries


# ================================
# BORROWER SIDE -- LINKS
# ================================
def _link_row(row):
    return {
        "link_id": row["link_id"],
        "base_url": row["base_url"],
        "label": row["label"],
        "peer_id": row["peer_id"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "last_ok_at": row["last_ok_at"],
        "last_error": row["last_error"],
        "last_error_at": row["last_error_at"],
    }


def create_link(db_path, base_url, label, created_by, peer_id="", now=None):
    """Record an outbound link to a hub that has lent us something. Returns the row.

    The peer TOKEN is not passed here and is never stored in this table -- the caller puts it
    in the master-key-wrapped secret store keyed on `link_id`, beside the backup destination
    credentials. Two columns in one table, one of which is a live credential to another
    fleet, is how a console page ends up rendering one.
    """
    base_url = normalize_peer_url(base_url)
    created_by = permissions.normalize_email(created_by)
    if not created_by:
        raise SharingError("A peer link must be created by a signed-in operator.")
    now = int(now if now is not None else time.time())

    with fleet.get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT link_id FROM share_links WHERE base_url = ?", (base_url,)).fetchone()
        if existing is not None:
            raise SharingError("This hub is already linked. Remove that link first.")
        link_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO share_links(link_id, base_url, label, peer_id, created_by, "
            "                        created_at, last_ok_at, last_error, last_error_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (link_id, base_url, _clean(label), _clean(peer_id, 64), created_by, now),
        )

    fleet.audit(db_path, actor=created_by, action="share.link_add",
                level=fleet.LEVEL_SECURITY, target=base_url,
                detail={"link_id": link_id, "label": _clean(label)})
    return get_link(db_path, link_id)


def get_link(db_path, link_id):
    with fleet.get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM share_links WHERE link_id = ?",
                           (str(link_id),)).fetchone()
    return _link_row(row) if row is not None else None


def list_links(db_path):
    with fleet.get_conn(db_path) as conn:
        return [_link_row(r) for r in conn.execute(
            "SELECT * FROM share_links ORDER BY created_at DESC, rowid DESC").fetchall()]


def delete_link(db_path, link_id, actor):
    """Forget a peer hub entirely: the link and everything it was showing us.

    The cached machines go with it in the same transaction. A borrowed machine outliving the
    link it came through would be a row nothing can refresh, revoke or explain -- exactly the
    kind of orphan the "never enrolled here" rule exists to prevent.

    Deleting the stored token is the caller's job, for the same reason storing it was.
    """
    link = get_link(db_path, link_id)
    if link is None:
        return False
    with fleet.get_conn(db_path) as conn:
        conn.execute("DELETE FROM borrowed_machines WHERE link_id = ?", (str(link_id),))
        conn.execute("DELETE FROM share_links WHERE link_id = ?", (str(link_id),))
    fleet.audit(db_path, actor=actor or "unknown", action="share.link_remove",
                level=fleet.LEVEL_SECURITY, target=link["base_url"],
                detail={"link_id": link["link_id"], "label": link["label"]})
    return True


def record_link_result(db_path, link_id, ok, error=None, now=None):
    """Remember how the last catalogue read went, so the console can say so.

    A success does NOT clear `last_error`: the pair of timestamps is what tells an operator
    "it works now, but it was broken an hour ago", and wiping the error on the first good
    poll turns an intermittent peer into an invisible one.
    """
    now = int(now if now is not None else time.time())
    with fleet.get_conn(db_path) as conn:
        if ok:
            conn.execute("UPDATE share_links SET last_ok_at = ? WHERE link_id = ?",
                         (now, str(link_id)))
        else:
            conn.execute(
                "UPDATE share_links SET last_error = ?, last_error_at = ? "
                "WHERE link_id = ?", (_clean(error, 500), now, str(link_id)))


# ================================
# BORROWER SIDE -- BORROWED MACHINES
# ================================
def _borrowed_row(row, now):
    cached_at = row["cached_at"]
    return {
        "link_id": row["link_id"],
        "share_id": row["share_id"],
        "hostname": row["hostname"],
        "capabilities": _json_list(row["capabilities_json"]),
        "online": bool(row["online"]),
        "last_seen": row["last_seen"],
        "expires_at": row["expires_at"],
        "lapsed": bool(row["lapsed"]),
        "cached_at": cached_at,
        # Stale means "the owner hub has not answered recently", which is a different thing
        # from the machine being offline and must not be shown as one.
        "stale": (now - cached_at) > BORROWED_STALE_SECONDS,
        "borrowed": True,
    }


def replace_borrowed(db_path, link_id, entries, now=None):
    """Store a catalogue read for one link. Returns (added, removed) share id lists.

    Wholesale replacement rather than a merge, because the catalogue is authoritative and a
    row missing from it means the share is gone -- a merge would keep showing a machine whose
    grant was revoked, which is the one thing this cache must never do.

    The returned lists exist so the caller can audit an appearance and a disappearance. A
    machine vanishing from a console with no record of why is the complaint this answers.
    """
    now = int(now if now is not None else time.time())
    link_id = str(link_id)
    incoming = {}
    for entry in entries or ():
        share_id = str((entry or {}).get("share_id") or "").strip()
        if share_id:
            incoming[share_id] = entry

    with fleet.get_conn(db_path) as conn:
        existing = {r["share_id"] for r in conn.execute(
            "SELECT share_id FROM borrowed_machines WHERE link_id = ?",
            (link_id,)).fetchall()}
        conn.execute("DELETE FROM borrowed_machines WHERE link_id = ?", (link_id,))
        for share_id, entry in incoming.items():
            # Filtered through SHAREABLE_CAPABILITIES rather than stored as sent. The
            # catalogue is another hub's JSON, so it is input: a peer naming
            # `manage_settings` must not end up with the borrowing console drawing a button
            # for it, however loudly the owner hub would refuse the request behind it.
            capabilities = [c for c in SHAREABLE_CAPABILITIES
                            if c in set(entry.get("capabilities") or ())]
            conn.execute(
                "INSERT INTO borrowed_machines(link_id, share_id, hostname, "
                "        capabilities_json, online, last_seen, expires_at, lapsed, "
                "        cached_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (link_id, share_id, _clean(entry.get("hostname"), 255),
                 json.dumps(capabilities), 1 if entry.get("online") else 0,
                 entry.get("last_seen"), entry.get("expires_at"),
                 1 if entry.get("lapsed") else 0, now),
            )

    return (sorted(set(incoming) - existing), sorted(existing - set(incoming)))


def list_borrowed(db_path, link_id=None, now=None):
    """Every machine another hub is currently lending us."""
    now = int(now if now is not None else time.time())
    sql = "SELECT * FROM borrowed_machines"
    params = []
    if link_id:
        sql += " WHERE link_id = ?"
        params.append(str(link_id))
    sql += " ORDER BY hostname COLLATE NOCASE, share_id"
    with fleet.get_conn(db_path) as conn:
        return [_borrowed_row(r, now) for r in conn.execute(sql, params).fetchall()]


def get_borrowed(db_path, link_id, share_id, now=None):
    now = int(now if now is not None else time.time())
    with fleet.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM borrowed_machines WHERE link_id = ? AND share_id = ?",
            (str(link_id), str(share_id))).fetchone()
    return _borrowed_row(row, now) if row is not None else None


def borrowed_can(borrowed, capability):
    """Does the borrowing hub believe this share carries `capability`?

    A CLIENT-SIDE check over the rendering cache, and nothing more: it decides which buttons
    to draw, and it is never the reason an action is allowed. The owner hub re-decides every
    request in `authorize_peer_action`, against rows this hub cannot see, and that is the
    only decision that counts. Two hubs disagreeing here means the borrower's console is out
    of date, not that anything got through.
    """
    return capability in ((borrowed or {}).get("capabilities") or ())
