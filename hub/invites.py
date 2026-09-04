"""Invite links -- a bounded, self-service way onto this hub (roadmap #22).

Until now nobody could be *invited*. An operator got access one of three ways, all of
them out of band: an admin hand-edited ALLOWED_EMAILS in `.env` and restarted the hub, an
admin typed their address into a permission group's member list, or somebody upstream put
them in a mapped directory group. In every case the admin had to know the address first,
and the person was never told anything. An invite link closes that: an admin decides what
the redeemer will get, hands over a URL, and the person signs in through it.

**An invite link is a bearer credential that confers console access, and console access on
this hub is arbitrary code execution as SYSTEM on the fleet.** Every rule below exists
because of that sentence, so none of them is a nicety:

  * **Seats.** `max_uses` bounds how many DISTINCT people the link admits; 1 is a
    single-use invite. The claim is one conditional UPDATE (see `redeem`) so two people
    racing the last seat cannot both win.
  * **Expiry is optional but defaulted.** `expires_at IS NULL` means never, which an admin
    has to choose deliberately; the form defaults to DEFAULT_TTL_DAYS. A link forwarded
    into a mailbox outlives the conversation it was sent in.
  * **Pinned addresses.** An invite may name the exact addresses it will accept. Checked
    BEFORE the seat claim, so a forwarded link costs the invite nothing.
  * **A creator ceiling.** An invite can never grant more than the admin who created it
    already holds. Without it, an operator with `manage_permission_groups` scoped to five
    machines could mint themselves a link into a fleet-wide group -- privilege escalation
    dressed as an invitation.
  * **Only the hash of the code is stored**, so the table is not worth stealing.

**What an invite grants is only ever a list of permission-group ids.** It is tempting to
let the invite carry its own capabilities and scope, and that was rejected: roadmap #8 is
explicit that the hub has one authorization model and a second one is the thing to avoid.
An admin who wants a bespoke grant gets a permission group created for them at invite time
(invites_web does that), which means revocation, machine scope, the audit trail and the
permissions cache all keep working with no special case for "someone who arrived by link".

**An invite can never grant break-glass.** ALLOWED_EMAILS stays env-only and nothing here
writes to it -- superuser is a decision made on the server's filesystem, not over HTTP.

Flask-free on purpose, like permissions.py and users.py; the HTTP surface is invites_web.py.
"""
import hashlib
import json
import secrets
import time
import uuid

import fleet
import permissions

#: Default lifetime the admin form offers. Long enough to survive a weekend, short enough
#: that a forgotten link is not a standing door.
DEFAULT_TTL_DAYS = 7

#: An upper bound on seats, not a policy. A four-digit seat count on a link that grants
#: SYSTEM on the fleet is a typo, not an intention, and a typo that reads as success is
#: exactly the failure this module is written to prevent.
MAX_USES_LIMIT = 100

#: Labels are shown in a list and in the invitee's landing page, so they only have to be
#: readable.
MAX_LABEL_CHARS = 80

#: How the code is generated. 32 bytes urlsafe -- the same strength apitokens uses for a
#: pairing code, and for the same reason: it is guessed at over the network or not at all.
CODE_BYTES = 32

#: Status values `list_invites` derives. Rendered through literal i18n keys in the page --
#: see the note in invites.js about why these are not interpolated into a key.
STATUS_ACTIVE = "active"
STATUS_USED_UP = "used_up"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"


class InviteError(Exception):
    """An invite that cannot be created or redeemed, with a message meant for a human.

    A separate class from ValueError so `redeem`'s refusals can be told apart at the
    sign-in call site, where the message is pasted into a 403 the invitee actually reads.
    """


# ================================
# SCHEMA
# ================================
def init_invites_db(db_path):
    with permissions.get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One row per invite link. `used_count` is the authoritative seat counter and is
        # only ever moved by the conditional UPDATE in `redeem` -- invite_redemptions
        # below is the record of WHO, not the bound on HOW MANY. Two counters would be
        # two things to keep in step, and the one that drifted would be the one that
        # bounds access.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                invite_id          TEXT PRIMARY KEY,
                code_hash          TEXT NOT NULL UNIQUE,
                label              TEXT NOT NULL DEFAULT '',
                group_ids_json     TEXT NOT NULL DEFAULT '[]',
                pinned_emails_json TEXT NOT NULL DEFAULT '[]',
                max_uses           INTEGER NOT NULL,
                used_count         INTEGER NOT NULL DEFAULT 0,
                -- NULL means never expires. Deliberately nullable rather than a sentinel
                -- far-future timestamp, so the SQL that bounds a redemption has to say
                -- "IS NULL OR" out loud instead of relying on a magic number holding.
                expires_at         INTEGER,
                created_at         INTEGER NOT NULL,
                created_by         TEXT NOT NULL DEFAULT 'unknown',
                revoked            INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Who came in through which invite. Keyed on (invite_id, email) so a second
        # sign-in by the same person is a no-op rather than a second seat -- see `redeem`.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invite_redemptions (
                invite_id   TEXT NOT NULL,
                email       TEXT NOT NULL,
                redeemed_at INTEGER NOT NULL,
                PRIMARY KEY (invite_id, email)
            )
        """)


# ================================
# HELPERS
# ================================
def _hash_code(code):
    """Store only the hash, so a database leak hands out no live invite.

    Deliberately the SAME rule as apitokens._hash_secret and fleet._hash_token (sha256 of
    the utf-8 bytes, hex) -- three token stores that hash three ways is three things to
    get right, and there is no reason for them to differ.
    """
    return hashlib.sha256(str(code).encode("utf-8")).hexdigest()


def _json_list(raw):
    try:
        loaded = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in loaded] if isinstance(loaded, list) else []


def _validate_label(label):
    text = str(label or "").strip()
    if not text:
        raise InviteError("An invite needs a label, so it can be told apart in the list.")
    return text[:MAX_LABEL_CHARS]


def _validate_max_uses(max_uses):
    try:
        seats = int(max_uses)
    except (TypeError, ValueError):
        raise InviteError("The number of uses must be a whole number.")
    if seats < 1:
        raise InviteError("An invite needs at least one use.")
    if seats > MAX_USES_LIMIT:
        raise InviteError(f"An invite is limited to {MAX_USES_LIMIT} uses.")
    return seats


def _validate_pinned_emails(emails):
    """Normalise the addresses an invite will accept, if any. An empty list means the
    invite is open to whoever holds the link, up to its seat count."""
    cleaned = []
    for raw in (emails or []):
        email = permissions.normalize_email(raw)
        if not email:
            continue
        if "@" not in email:
            raise InviteError(f"{raw!r} is not an email address.")
        if email not in cleaned:
            cleaned.append(email)
    return sorted(cleaned)


def _validate_group_ids(db_path, group_ids):
    """Every named group must exist. A group id that resolves to nothing would produce an
    invite that looks like a grant in the list and admits people to nothing -- the exact
    shape of failure this module is meant not to have."""
    cleaned = []
    for raw in (group_ids or []):
        gid = str(raw or "").strip()
        if not gid or gid in cleaned:
            continue
        if permissions.get_group(db_path, gid) is None:
            raise InviteError("That permission group no longer exists.")
        cleaned.append(gid)
    if not cleaned:
        raise InviteError("An invite has to grant at least one permission group.")
    return cleaned


def _check_creator_ceiling(db_path, group_ids, creator_permissions):
    """Refuse an invite that grants more than its creator holds.

    `creator_permissions` is an `effective_permissions` dict, or None to skip the check --
    None is for callers that are not acting for a signed-in human (tests, a future CLI),
    never a way for a route to opt out.

    Superuser passes everything, which is what break-glass means. For everyone else both
    halves are checked, because either one alone is an escalation: capabilities, and
    machine scope (`machines is None` on the CREATOR side means unrestricted and passes;
    `None` on the GROUP side means the group is fleet-wide and only an unrestricted
    creator may hand that out).
    """
    if creator_permissions is None or creator_permissions.get("superuser"):
        return

    held_caps = set(creator_permissions.get("capabilities") or ())
    held_machines = creator_permissions.get("machines")

    for gid in group_ids:
        group = permissions.get_group(db_path, gid)
        if group is None:
            raise InviteError("That permission group no longer exists.")

        extra = sorted(set(group["capabilities"]) - held_caps)
        if extra:
            raise InviteError(
                f"You cannot create an invite granting {group['name']!r}: it includes "
                f"capabilities you do not hold ({', '.join(extra)}).")

        if held_machines is None:
            continue
        if group["scope_mode"] == permissions.SCOPE_ALL:
            raise InviteError(
                f"You cannot create an invite granting {group['name']!r}: it covers every "
                f"machine and your own access does not.")
        beyond = sorted(set(group["machines"]) - set(held_machines))
        if beyond:
            raise InviteError(
                f"You cannot create an invite granting {group['name']!r}: it covers "
                f"machines outside your own access ({', '.join(beyond)}).")


def _status(row, now):
    """What this invite is right now. Order matters: a revoked invite reads as revoked
    even after it also expires, because that is the fact the admin acted on."""
    if row["revoked"]:
        return STATUS_REVOKED
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return STATUS_EXPIRED
    if row["used_count"] >= row["max_uses"]:
        return STATUS_USED_UP
    return STATUS_ACTIVE


def _decode(row, now, redeemers=()):
    """One invite as the API returns it. **Never carries the code** -- the plaintext is
    returned exactly once, by create_invite, and is unrecoverable afterwards."""
    return {
        "invite_id": row["invite_id"],
        "label": row["label"],
        "group_ids": _json_list(row["group_ids_json"]),
        "pinned_emails": _json_list(row["pinned_emails_json"]),
        "max_uses": row["max_uses"],
        "used_count": row["used_count"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "revoked": bool(row["revoked"]),
        "status": _status(row, now),
        "redeemed_by": list(redeemers),
    }


# ================================
# WRITES
# ================================
# Auditing lives here rather than in the HTTP layer, for the same reason permissions.py
# gives: an invite is a grant, and the record of one must exist no matter which caller
# made it. Every entry is LEVEL_SECURITY -- these rows are the answer to "how did this
# person get access", which is a security question whoever is asking it.

def create_invite(db_path, label, group_ids=(), pinned_emails=(), max_uses=1,
                  ttl_days=DEFAULT_TTL_DAYS, actor="unknown", creator_permissions=None,
                  now=None):
    """Create an invite and return (invite_id, code). The code is plaintext, returned
    exactly once and never stored -- only its hash is.

    `ttl_days` of None means the invite never expires; that is a choice the admin makes
    explicitly in the form, not a default anything falls into.
    """
    label = _validate_label(label)
    seats = _validate_max_uses(max_uses)
    pinned = _validate_pinned_emails(pinned_emails)
    groups = _validate_group_ids(db_path, group_ids)
    _check_creator_ceiling(db_path, groups, creator_permissions)

    now = int(now if now is not None else time.time())
    if ttl_days is None:
        expires_at = None
    else:
        try:
            days = int(ttl_days)
        except (TypeError, ValueError):
            raise InviteError("The expiry must be a number of days.")
        if days < 1:
            raise InviteError("An invite must be valid for at least a day.")
        expires_at = now + days * 86400

    invite_id = uuid.uuid4().hex
    code = secrets.token_urlsafe(CODE_BYTES)

    with permissions.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO invites(invite_id, code_hash, label, group_ids_json, "
            "                    pinned_emails_json, max_uses, used_count, expires_at, "
            "                    created_at, created_by, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0)",
            (invite_id, _hash_code(code), label, json.dumps(groups),
             json.dumps(pinned), seats, expires_at, now, actor),
        )

    fleet.audit(db_path, actor, "invite.create", label, {
        "invite_id": invite_id, "group_ids": groups, "max_uses": seats,
        "expires_at": expires_at, "pinned_emails": pinned,
    }, level=fleet.LEVEL_SECURITY)
    return invite_id, code


def preview(db_path, code, now=None):
    """What the landing page shows somebody holding this link, or raise InviteError.

    **Consumes nothing.** A preview that claimed a seat would spend the invite on the mail
    client that fetched the link to render a thumbnail, and the first real person to click
    would be refused by an invite that had never met anyone.

    Returns only what the invitee needs to decide whether to sign in: the label, who sent
    it, and the names of the groups it grants. Never the capability list, the machine
    scope, the seat count or the other redeemers -- this is the one unauthenticated page
    on the hub and it must not be a reconnaissance surface.
    """
    now = int(now if now is not None else time.time())
    row = _row_for_code(db_path, code)
    if row is None:
        raise InviteError("This invite link is not valid.")
    _refuse_unusable(row, now)

    names = []
    for gid in _json_list(row["group_ids_json"]):
        group = permissions.get_group(db_path, gid)
        if group is not None:
            names.append(group["name"])
    return {
        "label": row["label"],
        "invited_by": row["created_by"],
        "group_names": sorted(names),
        "expires_at": row["expires_at"],
    }


def redeem(db_path, code, email, now=None):
    """Admit `email` through this invite: add them to every group it grants, record the
    redemption, and claim a seat. Returns a summary dict, or raises InviteError.

    Membership is written through `permissions.update_group`, never with SQL against
    permission_group_members directly. That keeps the permissions cache invalidation and
    the permission_group.update audit row in the one place that already gets them right --
    a direct INSERT here would grant access that the running hub could not see until
    something else happened to invalidate the cache.

    Two properties are worth being explicit about:

      * **The seat claim is a single conditional UPDATE.** rowcount 1 is what makes the
        caller the one who claimed the seat, so two people racing the last seat cannot
        both be admitted. Checking `used_count < max_uses` in Python and then writing
        would admit both, and this hub does run a threaded server.
      * **Redemption is idempotent per address.** Someone who signs in through the same
        link twice re-applies their membership and does NOT eat a second seat. A five-seat
        invite must admit five people, not one person who bounced off the login page five
        times.
    """
    now = int(now if now is not None else time.time())
    email = permissions.normalize_email(email)
    if not email or "@" not in email:
        raise InviteError("An invite can only be redeemed by a signed-in account.")

    row = _row_for_code(db_path, code)
    if row is None:
        raise InviteError("This invite link is not valid.")
    invite_id = row["invite_id"]

    # Pinned addresses are checked before anything is claimed, so a link that reached the
    # wrong inbox costs the invite nothing at all.
    pinned = _json_list(row["pinned_emails_json"])
    if pinned and email not in pinned:
        raise InviteError("This invite was issued to a different email address.")

    with permissions.get_conn(db_path) as conn:
        already = conn.execute(
            "SELECT 1 FROM invite_redemptions WHERE invite_id = ? AND email = ?",
            (invite_id, email),
        ).fetchone() is not None

    if not already:
        # Everything that bounds this invite is in the WHERE clause, so the check and the
        # claim cannot come apart. An expired or revoked invite fails here rather than in
        # a prior `if`, which is why _refuse_unusable is called afterwards purely to
        # explain a failure the database already decided.
        with permissions.get_conn(db_path) as conn:
            claimed = conn.execute(
                "UPDATE invites SET used_count = used_count + 1 "
                " WHERE invite_id = ? AND revoked = 0 AND used_count < max_uses "
                "   AND (expires_at IS NULL OR expires_at > ?)",
                (invite_id, now),
            ).rowcount
        if claimed != 1:
            # Re-read to say WHICH bound stopped this. "Invalid link" would send the
            # invitee to an admin who then looks in the wrong place -- expired, spent and
            # revoked have three different fixes.
            fresh = _row_by_id(db_path, invite_id)
            if fresh is not None:
                _refuse_unusable(fresh, now)
            raise InviteError("This invite link is not valid.")
        with permissions.get_conn(db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO invite_redemptions(invite_id, email, redeemed_at) "
                "VALUES (?, ?, ?)",
                (invite_id, email, now),
            )

    granted = []
    for gid in _json_list(row["group_ids_json"]):
        group = permissions.get_group(db_path, gid)
        if group is None:
            # A group deleted between creation and redemption. Skipped rather than fatal:
            # an invite naming two groups should still deliver the one that survives, and
            # the audit row below records what was actually granted.
            continue
        members = list(group["members"])
        if email not in members:
            members.append(email)
            permissions.update_group(db_path, gid, members=members,
                                     actor=f"invite:{invite_id}")
        granted.append(group["name"])

    fleet.audit(db_path, email, "invite.redeem", row["label"], {
        "invite_id": invite_id, "groups": granted, "repeat": already,
    }, level=fleet.LEVEL_SECURITY)
    return {"invite_id": invite_id, "label": row["label"], "groups": granted,
            "repeat": already}


def revoke_invite(db_path, invite_id, actor="unknown"):
    """Kill an invite without deleting its history. Raises KeyError if unknown.

    Revoke rather than delete is the default action in the UI because the redemption rows
    are the answer to "who came in through this link", and deleting the invite would take
    that answer with it.
    """
    invite_id = str(invite_id or "").strip()
    row = _row_by_id(db_path, invite_id)
    if row is None:
        raise KeyError(invite_id)
    with permissions.get_conn(db_path) as conn:
        conn.execute("UPDATE invites SET revoked = 1 WHERE invite_id = ?", (invite_id,))
    fleet.audit(db_path, actor, "invite.revoke", row["label"],
                {"invite_id": invite_id, "used_count": row["used_count"]},
                level=fleet.LEVEL_SECURITY)
    return True


def delete_invite(db_path, invite_id, actor="unknown"):
    """Remove an invite and its redemption rows. Raises KeyError if unknown.

    **Does not revoke anyone's access.** Someone admitted by this invite is a member of a
    permission group now, exactly as if an admin had typed their address in, and that
    membership is removed on the Permission Groups page. Same rule roadmap #8 states for
    deleting a user profile, and for the same reason: one grant store, one place to revoke.
    """
    invite_id = str(invite_id or "").strip()
    row = _row_by_id(db_path, invite_id)
    if row is None:
        raise KeyError(invite_id)
    with permissions.get_conn(db_path) as conn:
        conn.execute("DELETE FROM invite_redemptions WHERE invite_id = ?", (invite_id,))
        conn.execute("DELETE FROM invites WHERE invite_id = ?", (invite_id,))
    fleet.audit(db_path, actor, "invite.delete", row["label"],
                {"invite_id": invite_id, "used_count": row["used_count"]},
                level=fleet.LEVEL_SECURITY)
    return True


# ================================
# READS
# ================================
def _row_for_code(db_path, code):
    raw = str(code or "").strip()
    if not raw:
        return None
    with permissions.get_conn(db_path) as conn:
        return conn.execute("SELECT * FROM invites WHERE code_hash = ?",
                            (_hash_code(raw),)).fetchone()


def _row_by_id(db_path, invite_id):
    with permissions.get_conn(db_path) as conn:
        return conn.execute("SELECT * FROM invites WHERE invite_id = ?",
                            (str(invite_id or "").strip(),)).fetchone()


def _refuse_unusable(row, now):
    """Raise the InviteError that explains why this invite cannot be used, or return.

    One function so the landing page and the redemption path give the same reason for the
    same invite -- an invitee told "expired" on one screen and "not valid" on the next has
    been told nothing.
    """
    status = _status(row, now)
    if status == STATUS_REVOKED:
        raise InviteError("This invite link has been revoked.")
    if status == STATUS_EXPIRED:
        raise InviteError("This invite link has expired. Ask for a new one.")
    if status == STATUS_USED_UP:
        raise InviteError("This invite link has already been used up.")


def list_invites(db_path, now=None):
    """Every invite, newest first, each with its derived status and its redeemers."""
    now = int(now if now is not None else time.time())
    with permissions.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM invites ORDER BY created_at DESC").fetchall()
        redeemers = {}
        for r in conn.execute(
            "SELECT invite_id, email FROM invite_redemptions ORDER BY redeemed_at"
        ):
            redeemers.setdefault(r["invite_id"], []).append(r["email"])
    return [_decode(row, now, redeemers.get(row["invite_id"], [])) for row in rows]


def get_invite(db_path, invite_id, now=None):
    """One invite, or None."""
    now = int(now if now is not None else time.time())
    row = _row_by_id(db_path, invite_id)
    if row is None:
        return None
    with permissions.get_conn(db_path) as conn:
        redeemers = [r["email"] for r in conn.execute(
            "SELECT email FROM invite_redemptions WHERE invite_id = ? "
            "ORDER BY redeemed_at", (row["invite_id"],))]
    return _decode(row, now, redeemers)
