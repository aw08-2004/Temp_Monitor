"""Model test for invites.py: a seat limit that does not actually bound how many people
get in.

That is the failure this file exists to catch, and it is a silent one -- an invite that
admits six people on five seats looks exactly like an invite that worked, on a page whose
whole job is to say how many were let in. The same shape covers the other three bounds
(expiry, revocation, pinned addresses): each of them can fail by admitting somebody, and
none of them announces it.

The other half is escalation. An invite is created by an admin and redeemed by somebody
who is not one, so "an invite grants more than its creator holds" is a privilege escalation
with an audit trail that reads as an ordinary invitation. `_check_creator_ceiling` is
asserted on directly.

Deliberately NOT covered here: the HTTP gates and the public landing route, which are
test_invites_web.py's subject.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import invites
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


def audit_actions(db_path):
    with fleet.get_conn(db_path) as conn:
        return [r["action"] for r in conn.execute(
            "SELECT action FROM audit_log ORDER BY id")]


def members(db_path, group_id):
    return permissions.get_group(db_path, group_id)["members"]


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        permissions.init_permissions_db(db_path)
        invites.init_invites_db(db_path)
        permissions.invalidate()

        techs = permissions.create_group(
            db_path, name="Techs", capabilities=[permissions.VIEW],
            scope_mode=permissions.SCOPE_LIST, machines=["PC1"], actor="root@x.com")
        auditors = permissions.create_group(
            db_path, name="Auditors", capabilities=[permissions.VIEW_AUDIT_LOG],
            scope_mode=permissions.SCOPE_ALL, actor="root@x.com")

        print("\n== Creating an invite ==")
        invite_id, code = invites.create_invite(
            db_path, label="Two techs", group_ids=[techs], max_uses=2,
            actor="root@x.com")
        check("returns an id and a code", bool(invite_id) and bool(code))
        row = invites.get_invite(db_path, invite_id)
        check("starts unused", row["used_count"] == 0 and row["max_uses"] == 2)
        check("starts active", row["status"] == invites.STATUS_ACTIVE)
        check("defaults to an expiry", row["expires_at"] is not None)
        check("the read-back never carries the code", "code" not in row)
        # The whole reason the row is not worth stealing.
        with permissions.get_conn(db_path) as conn:
            stored = conn.execute("SELECT code_hash FROM invites WHERE invite_id = ?",
                                  (invite_id,)).fetchone()["code_hash"]
        check("only the hash is stored", stored != code and len(stored) == 64)
        check("create is audited", "invite.create" in audit_actions(db_path))

        print("\n== Redemption grants the group ==")
        result = invites.redeem(db_path, code, "Ann@X.com")
        check("returns the groups granted", result["groups"] == ["Techs"])
        check("email normalised into the group", "ann@x.com" in members(db_path, techs))
        check("a seat was claimed",
              invites.get_invite(db_path, invite_id)["used_count"] == 1)
        check("the redeemer is recorded",
              invites.get_invite(db_path, invite_id)["redeemed_by"] == ["ann@x.com"])
        check("redemption is audited", "invite.redeem" in audit_actions(db_path))
        # Not a detail: the permissions cache is process-wide, so a grant written behind
        # its back is a grant the running hub cannot see.
        check("the grant is visible through effective_permissions",
              permissions.VIEW in permissions.effective_permissions(
                  db_path, "ann@x.com")["capabilities"])

        print("\n== The same person twice costs one seat ==")
        again = invites.redeem(db_path, code, "ann@x.com")
        check("second redemption reports itself as a repeat", again["repeat"] is True)
        check("still one seat used",
              invites.get_invite(db_path, invite_id)["used_count"] == 1)
        check("still one redeemer",
              invites.get_invite(db_path, invite_id)["redeemed_by"] == ["ann@x.com"])

        print("\n== Seats actually bound how many people get in ==")
        invites.redeem(db_path, code, "bob@x.com")
        check("second person claims the last seat",
              invites.get_invite(db_path, invite_id)["used_count"] == 2)
        try:
            invites.redeem(db_path, code, "carl@x.com")
            check("a third person is refused", False)
        except invites.InviteError as e:
            check("a third person is refused", "used up" in str(e))
        check("the refused person got nothing",
              "carl@x.com" not in members(db_path, techs))
        check("used_count never passed max_uses",
              invites.get_invite(db_path, invite_id)["used_count"] == 2)
        check("a spent invite reads as used up",
              invites.get_invite(db_path, invite_id)["status"] == invites.STATUS_USED_UP)

        print("\n== Expiry ==")
        _, short = invites.create_invite(db_path, label="Short", group_ids=[techs],
                                         ttl_days=1, actor="root@x.com")
        later = int(time.time()) + 2 * 86400
        try:
            invites.redeem(db_path, short, "dana@x.com", now=later)
            check("an expired invite is refused", False)
        except invites.InviteError as e:
            check("an expired invite is refused", "expired" in str(e))
        check("the expired invite admitted nobody",
              "dana@x.com" not in members(db_path, techs))

        never_id, never = invites.create_invite(db_path, label="Standing",
                                                group_ids=[techs], ttl_days=None,
                                                actor="root@x.com")
        check("ttl_days=None stores no expiry",
              invites.get_invite(db_path, never_id)["expires_at"] is None)
        invites.redeem(db_path, never, "erin@x.com", now=later + 365 * 86400)
        check("a never-expiring invite still works a year on",
              "erin@x.com" in members(db_path, techs))

        print("\n== Revocation ==")
        rev_id, rev = invites.create_invite(db_path, label="Oops", group_ids=[techs],
                                            max_uses=5, actor="root@x.com")
        invites.revoke_invite(db_path, rev_id, actor="root@x.com")
        try:
            invites.redeem(db_path, rev, "mallory@x.com")
            check("a revoked invite is refused", False)
        except invites.InviteError as e:
            check("a revoked invite is refused", "revoked" in str(e))
        check("the revoked invite admitted nobody",
              "mallory@x.com" not in members(db_path, techs))
        check("revoke is audited", "invite.revoke" in audit_actions(db_path))

        print("\n== Pinned addresses ==")
        pin_id, pin = invites.create_invite(
            db_path, label="For Carol only", group_ids=[techs], max_uses=3,
            pinned_emails=["Carol@X.com"], actor="root@x.com")
        try:
            invites.redeem(db_path, pin, "stranger@y.com")
            check("a wrong address is refused", False)
        except invites.InviteError as e:
            check("a wrong address is refused", "different email" in str(e))
        # The point of checking pins before the seat claim: a link that reached the wrong
        # inbox must not quietly spend the invite it was meant to deliver.
        check("a refused address costs no seat",
              invites.get_invite(db_path, pin_id)["used_count"] == 0)
        invites.redeem(db_path, pin, "carol@x.com")
        check("the pinned address is admitted", "carol@x.com" in members(db_path, techs))

        print("\n== An invite cannot grant more than its creator holds ==")
        scoped = {"email": "scoped@x.com", "superuser": False,
                  "capabilities": {permissions.VIEW, permissions.MANAGE_PERMISSION_GROUPS},
                  "machines": {"PC1"}, "groups": []}
        try:
            invites.create_invite(db_path, label="Escalate", group_ids=[auditors],
                                  actor="scoped@x.com", creator_permissions=scoped)
            check("a capability the creator lacks is refused", False)
        except invites.InviteError as e:
            check("a capability the creator lacks is refused",
                  "capabilities you do not hold" in str(e))

        wide = permissions.create_group(
            db_path, name="Wide", capabilities=[permissions.VIEW],
            scope_mode=permissions.SCOPE_ALL, actor="root@x.com")
        try:
            invites.create_invite(db_path, label="Whole fleet", group_ids=[wide],
                                  actor="scoped@x.com", creator_permissions=scoped)
            check("a fleet-wide group is refused to a scoped creator", False)
        except invites.InviteError as e:
            check("a fleet-wide group is refused to a scoped creator",
                  "every machine" in str(e))

        other = permissions.create_group(
            db_path, name="Other floor", capabilities=[permissions.VIEW],
            scope_mode=permissions.SCOPE_LIST, machines=["PC9"], actor="root@x.com")
        try:
            invites.create_invite(db_path, label="Someone else's machines",
                                  group_ids=[other], actor="scoped@x.com",
                                  creator_permissions=scoped)
            check("a machine outside the creator's scope is refused", False)
        except invites.InviteError as e:
            check("a machine outside the creator's scope is refused",
                  "outside your own access" in str(e))

        ok_id, _ = invites.create_invite(db_path, label="Within reach",
                                         group_ids=[techs], actor="scoped@x.com",
                                         creator_permissions=scoped)
        check("a grant within the creator's own access is allowed", bool(ok_id))

        root = {"email": "root@x.com", "superuser": True,
                "capabilities": set(permissions.CAPABILITIES), "machines": None,
                "groups": []}
        su_id, _ = invites.create_invite(db_path, label="Break glass",
                                         group_ids=[auditors], actor="root@x.com",
                                         creator_permissions=root)
        check("a superuser may grant anything", bool(su_id))

        print("\n== Refusals before anything is written ==")
        for label, kwargs, why in [
            ("", {"group_ids": [techs]}, "a blank label"),
            ("No groups", {"group_ids": []}, "no groups"),
            ("Ghost", {"group_ids": ["nope"]}, "an unknown group"),
            ("Too many", {"group_ids": [techs], "max_uses": 9999}, "an absurd seat count"),
            ("Zero", {"group_ids": [techs], "max_uses": 0}, "zero seats"),
            ("Bad pin", {"group_ids": [techs], "pinned_emails": ["not-an-email"]},
             "a pinned value that is not an address"),
        ]:
            before = len(invites.list_invites(db_path))
            try:
                invites.create_invite(db_path, label=label, actor="root@x.com", **kwargs)
                check(f"{why} is refused", False)
            except invites.InviteError:
                check(f"{why} is refused", True)
            check(f"{why} wrote nothing", len(invites.list_invites(db_path)) == before)

        print("\n== Unknown codes and deletion ==")
        try:
            invites.redeem(db_path, "not-a-real-code", "x@y.com")
            check("an unknown code is refused", False)
        except invites.InviteError as e:
            check("an unknown code is refused", "not valid" in str(e))
        try:
            invites.preview(db_path, "not-a-real-code")
            check("preview refuses an unknown code", False)
        except invites.InviteError:
            check("preview refuses an unknown code", True)

        # Preview must not consume anything -- a mail client fetching the link to render a
        # thumbnail would otherwise spend the invite before a person ever clicked it.
        peek_id, peek = invites.create_invite(db_path, label="Peek", group_ids=[techs],
                                              actor="root@x.com")
        seen = invites.preview(db_path, peek)
        check("preview names the groups", seen["group_names"] == ["Techs"])
        check("preview names the sender", seen["invited_by"] == "root@x.com")
        check("preview consumes no seat",
              invites.get_invite(db_path, peek_id)["used_count"] == 0)

        invites.delete_invite(db_path, peek_id, actor="root@x.com")
        check("delete removes the invite", invites.get_invite(db_path, peek_id) is None)
        check("delete is audited", "invite.delete" in audit_actions(db_path))
        # Deleting the invite is not an access change, exactly as deleting a user profile
        # is not (roadmap #8). Ann came in through an invite that is now gone; her
        # membership is a permission group's business and stays until someone removes it.
        check("deleting an invite does not revoke anyone",
              "ann@x.com" in members(db_path, techs))
        try:
            invites.delete_invite(db_path, "nope", actor="root@x.com")
            check("deleting an unknown invite raises KeyError", False)
        except KeyError:
            check("deleting an unknown invite raises KeyError", True)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
