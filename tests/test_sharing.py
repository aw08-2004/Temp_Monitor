"""Model tests for sharing.py -- lending one machine to another hub (roadmap #15).

Flask-free, like the module under test. What is worth stating about the assertions:

  * **Shareable capabilities are an ALLOW-LIST.** The test that matters is not that the
    three permitted ones work; it is that EVERY capability in permissions.CAPABILITIES is
    classified, so a capability added next year is refused until somebody decides
    otherwise. That is asserted directly, against the live tuple.
  * **The grant is intersected LIVE against its creator's permissions.** Demoting the
    operator who lent a machine must take the share with them, and the refusal must NAME
    the capability rather than quietly serving less -- so both the lapse and its wording
    are asserted.
  * **Nothing is minted until a pairing code is redeemed**, the code is single-use, an
    expired one is CONSUMED, and the token is not recoverable from the database. The same
    four properties tests/test_apitokens.py asserts, because this is the same mechanism one
    level up and the two must not drift.
  * **A peer cannot enumerate shares it does not hold.** Somebody else's share id and a
    revoked one must both come back as the same generic miss.
  * **The borrowed cache is a cache.** A catalogue read replaces it wholesale, a hostile
    catalogue cannot smuggle a capability into it, and deleting a link takes its machines
    with it -- a borrowed machine must never outlive the window it was seen through.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import sharing

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


def _url_refusal(url):
    try:
        sharing.normalize_peer_url(url)
    except sharing.SharingError as e:
        return str(e)
    return ""


def test_capability_allow_list():
    print("\n== What a share may carry is an allow-list, closed by default ==")
    check("every capability is classified one way or the other",
          set(sharing.SHAREABLE_CAPABILITIES) <= set(permissions.CAPABILITIES))
    check("the three shareable ones are the per-MACHINE ones",
          set(sharing.SHAREABLE_CAPABILITIES)
          == {permissions.VIEW, permissions.ISSUE_COMMANDS, permissions.REMOTE_CONTROL})
    check("shareable order follows permissions.CAPABILITIES order",
          list(sharing.SHAREABLE_CAPABILITIES)
          == [c for c in permissions.CAPABILITIES if c in sharing.SHAREABLE_CAPABILITIES])

    # The point of an allow-list: everything not named is refused, including capabilities
    # nobody thought about when this was written.
    for capability in permissions.CAPABILITIES:
        if capability in sharing.SHAREABLE_CAPABILITIES:
            continue
        try:
            sharing.normalize_share_capabilities([permissions.VIEW, capability])
            check(f"{capability} cannot be shared", False)
        except sharing.SharingError as e:
            check(f"{capability} cannot be shared, by name", capability in str(e))

    check("the refusal explains itself",
          "this hub's own configuration"
          in _capability_refusal([permissions.MANAGE_PATCHES]))

    check("orders by the admin UI's own order",
          sharing.normalize_share_capabilities(
              [permissions.REMOTE_CONTROL, permissions.VIEW])
          == [permissions.VIEW, permissions.REMOTE_CONTROL])
    check("deduplicates",
          sharing.normalize_share_capabilities([permissions.VIEW, permissions.VIEW])
          == [permissions.VIEW])

    try:
        sharing.normalize_share_capabilities(["fly_the_plane"])
        check("an unknown capability is refused", False)
    except sharing.SharingError as e:
        check("an unknown capability is refused, by name", "fly_the_plane" in str(e))

    try:
        sharing.normalize_share_capabilities([])
        check("an empty set is refused", False)
    except sharing.SharingError:
        check("an empty set is refused -- an inert share is a support call", True)

    check("the default ticks remote_control and NOT view",
          tuple(sharing.DEFAULT_SHARE_CAPABILITIES) == (permissions.REMOTE_CONTROL,))


def _capability_refusal(capabilities):
    try:
        sharing.normalize_share_capabilities(capabilities)
    except sharing.SharingError as e:
        return str(e)
    return ""


def test_authorization_header_parsing():
    print("\n== Three credentials arrive in one header and must not be confused ==")
    peer_id, secret = sharing.parse_peer_authorization("Bearer tmh_abc123:s3cret")
    check("parses a peer token", (peer_id, secret) == ("abc123", "s3cret"))

    check("a USER token does not parse as a peer token",
          sharing.parse_peer_authorization("Bearer tmu_abc123:s3cret") == (None, None))
    check("an AGENT token does not parse as a peer token",
          sharing.parse_peer_authorization("Bearer deadbeef:agenttoken") == (None, None))
    check("no prefix, no parse",
          sharing.parse_peer_authorization("Bearer abc:def") == (None, None))
    check("missing secret",
          sharing.parse_peer_authorization("Bearer tmh_abc:") == (None, None))
    check("missing id",
          sharing.parse_peer_authorization("Bearer tmh_:secret") == (None, None))
    check("no colon at all",
          sharing.parse_peer_authorization("Bearer tmh_abcdef") == (None, None))
    check("not a bearer header",
          sharing.parse_peer_authorization("Basic abc") == (None, None))
    check("no header at all", sharing.parse_peer_authorization(None) == (None, None))


def test_peer_url():
    print("\n== A peer address is where a credential gets sent, so it is validated ==")
    check("keeps an ordinary hub address",
          sharing.normalize_peer_url("https://hub.example.com")
          == "https://hub.example.com")
    check("keeps a port", sharing.normalize_peer_url("https://hub.example.com:8443")
          == "https://hub.example.com:8443")
    check("keeps a path prefix",
          sharing.normalize_peer_url("https://example.com/fleet")
          == "https://example.com/fleet")
    check("drops a trailing slash, so two spellings are one link",
          sharing.normalize_peer_url("https://hub.example.com/")
          == "https://hub.example.com")
    check("assumes https when the operator typed a bare host",
          sharing.normalize_peer_url("hub.example.com") == "https://hub.example.com")

    bad = {
        "http://hub.example.com": "plain http would put the token on the wire",
        "https://user:pw@hub.example.com": "credentials in the url",
        "https://hub.example.com/machine?id=3": "a whole console url, not a hub address",
        "https://hub.example.com/#/machines": "a fragment",
        "ftp://hub.example.com": "not a hub protocol",
        "": "empty",
    }
    for url, why in bad.items():
        try:
            sharing.normalize_peer_url(url)
            check(f"refuses {url!r} ({why})", False)
        except sharing.SharingError:
            check(f"refuses {url!r} ({why})", True)

    check("the http refusal says why", "header" in _url_refusal("http://hub.example.com"))


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        permissions.init_permissions_db(db_path)
        sharing.init_sharing_db(db_path)
        permissions.invalidate()

        test_capability_allow_list()
        test_authorization_header_parsing()
        test_peer_url()

        # Owner-side operators. `owner@x.com` reaches PC-1 through a group; `root@x.com` is
        # break-glass and reaches everything without one.
        group_id = permissions.create_group(
            db_path, "Sharers",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS,
                          permissions.REMOTE_CONTROL],
            machines=["PC-1"], members=["owner@x.com"], actor="root@x.com")
        superusers = ("root@x.com",)

        print("\n== Nothing is minted until the pairing code is redeemed ==")
        code = sharing.create_pairing(db_path, "Bob's hub", "owner@x.com")
        check("no peer exists yet", sharing.list_peers(db_path) == [])

        token, peer = sharing.redeem_pairing(db_path, code, peer_label="bob-hub")
        check("redemption mints exactly one peer", len(sharing.list_peers(db_path)) == 1)
        check("the token carries the peer prefix", token.startswith(sharing.TOKEN_PREFIX))
        check("the row keeps OUR label for them", peer["label"] == "Bob's hub")
        check("...and, separately, what they call themselves",
              peer["peer_label"] == "bob-hub")
        check("...and who offered the pairing", peer["created_by"] == "owner@x.com")

        print("\n== The peer token is not recoverable from the database ==")
        with fleet.get_conn(db_path) as conn:
            stored = dict(conn.execute(
                "SELECT * FROM share_peers WHERE peer_id = ?",
                (peer["peer_id"],)).fetchone())
        secret = token.split(":", 1)[1]
        check("the secret appears in no column",
              not any(secret in str(v) for v in stored.values()))
        check("a listing never carries a token field",
              "token" not in sharing.list_peers(db_path)[0])

        print("\n== A pairing code works once ==")
        try:
            sharing.redeem_pairing(db_path, code)
            check("a used code is refused", False)
        except sharing.SharingError:
            check("a used code is refused", True)
        check("...and minted nothing the second time",
              len(sharing.list_peers(db_path)) == 1)

        stale = sharing.create_pairing(db_path, "Slow hub", "owner@x.com",
                                       now=time.time() - 7200)
        try:
            sharing.redeem_pairing(db_path, stale)
            check("an expired code is refused", False)
        except sharing.SharingError as e:
            check("an expired code is refused, and says so", "expired" in str(e).lower())
        try:
            sharing.redeem_pairing(db_path, stale)
            check("an expired code was CONSUMED, not left lying around", False)
        except sharing.SharingError as e:
            check("an expired code was CONSUMED, not left lying around",
                  "not valid" in str(e).lower())

        print("\n== Authenticating a peer ==")
        header = f"Bearer {token}"
        who = sharing.authenticate_peer(db_path, header)
        check("a good token resolves to the peer",
              (who or {}).get("peer_id") == peer["peer_id"])
        check("a peer identity carries NO capabilities of its own",
              "capabilities" not in (who or {}))
        check("a wrong secret is None",
              sharing.authenticate_peer(
                  db_path, f"Bearer {sharing.TOKEN_PREFIX}{peer['peer_id']}:wrong") is None)
        check("an unknown peer is None",
              sharing.authenticate_peer(
                  db_path, f"Bearer {sharing.TOKEN_PREFIX}nosuch:secret") is None)
        check("a user token is None at this door",
              sharing.authenticate_peer(db_path, "Bearer tmu_abc:def") is None)
        check("an expired peer token is None",
              sharing.authenticate_peer(
                  db_path, header, now=time.time() + 400 * 86400) is None)

        print("\n== A share is one machine, lent to one peer ==")
        share = sharing.create_share(
            db_path, peer["peer_id"], "PC-1",
            [permissions.REMOTE_CONTROL], created_by="owner@x.com")
        check("the share names the machine", share["machine"] == "PC-1")
        check("...and carries only what was ticked",
              share["capabilities"] == [permissions.REMOTE_CONTROL])
        check("...and records who lent it, which is what gets re-checked later",
              share["created_by"] == "owner@x.com")
        check("no expiry by default -- two hubs that cooperate should not stop on a Tuesday",
              share["expires_at"] is None)

        try:
            sharing.create_share(db_path, peer["peer_id"], "PC-1",
                                 [permissions.VIEW], created_by="owner@x.com")
            check("a second live share of the same machine is refused", False)
        except sharing.SharingError as e:
            check("a second live share of the same machine is refused, with the fix",
                  "revoke" in str(e).lower())

        try:
            sharing.create_share(db_path, peer["peer_id"], "PC-2", [permissions.VIEW],
                                 created_by="owner@x.com",
                                 expires_at=time.time() - 60)
            check("an expiry in the past is refused", False)
        except sharing.SharingError:
            check("an expiry in the past is refused", True)

        try:
            sharing.create_share(db_path, "nosuchpeer", "PC-2", [permissions.VIEW],
                                 created_by="owner@x.com")
            check("a share to an unpaired hub is refused", False)
        except sharing.SharingError:
            check("a share to an unpaired hub is refused", True)

        print("\n== The grant is intersected LIVE against its creator ==")
        state, err = sharing.authorize_peer_action(
            db_path, who, share["share_id"], permissions.REMOTE_CONTROL,
            superusers=superusers)
        check("the peer may do what it was lent", err is None and state["live"])

        state, err = sharing.authorize_peer_action(
            db_path, who, share["share_id"], permissions.ISSUE_COMMANDS,
            superusers=superusers)
        check("...and nothing it was not", state is None)
        check("...refused by name, not generically",
              permissions.ISSUE_COMMANDS in (err or ""))

        # Take remote_control off the group. The share row is untouched; what it can DO is
        # recomputed from the creator's live permissions on the next request.
        permissions.update_group(
            db_path, group_id,
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            actor="root@x.com")
        granted, lapsed = sharing.effective_share_capabilities(
            db_path, share, superusers=superusers)
        check("demoting the operator empties the grant", granted == [])
        check("...and reports exactly what lapsed",
              lapsed == [permissions.REMOTE_CONTROL])

        state, err = sharing.authorize_peer_action(
            db_path, who, share["share_id"], permissions.REMOTE_CONTROL,
            superusers=superusers)
        check("a lapsed share is refused", state is None)
        check("...NAMING the reason rather than degrading quietly",
              "no longer holds" in (err or ""))

        permissions.update_group(
            db_path, group_id,
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS,
                          permissions.REMOTE_CONTROL],
            actor="root@x.com")
        granted, lapsed = sharing.effective_share_capabilities(
            db_path, share, superusers=superusers)
        check("restoring the operator restores the share",
              granted == [permissions.REMOTE_CONTROL] and lapsed == [])

        # Losing SCOPE lapses the whole share, not part of it.
        permissions.update_group(db_path, group_id, machines=["PC-9"], actor="root@x.com")
        granted, lapsed = sharing.effective_share_capabilities(
            db_path, share, superusers=superusers)
        check("losing scope over the machine lapses the WHOLE share",
              granted == [] and lapsed == [permissions.REMOTE_CONTROL])
        permissions.update_group(db_path, group_id, machines=["PC-1"], actor="root@x.com")

        print("\n== A peer cannot enumerate what it does not hold ==")
        other_code = sharing.create_pairing(db_path, "Carol's hub", "root@x.com")
        other_token, other_peer = sharing.redeem_pairing(db_path, other_code)
        other_who = sharing.authenticate_peer(db_path, f"Bearer {other_token}")
        state, err = sharing.authorize_peer_action(
            db_path, other_who, share["share_id"], permissions.REMOTE_CONTROL,
            superusers=superusers)
        check("somebody else's share id is a miss", state is None)
        check("...and the miss says nothing about what exists", err == "No such share.")

        expiring = sharing.create_share(
            db_path, other_peer["peer_id"], "PC-1", [permissions.VIEW],
            created_by="root@x.com", expires_at=time.time() + 3600)
        state, err = sharing.authorize_peer_action(
            db_path, other_who, expiring["share_id"], permissions.VIEW,
            superusers=superusers, now=time.time() + 7200)
        check("an expired share is refused", state is None)
        check("...and says so, because the peer can act on that",
              "expired" in (err or "").lower())

        print("\n== A superuser's share never lapses on a group edit ==")
        granted, lapsed = sharing.effective_share_capabilities(
            db_path, expiring, superusers=superusers)
        check("break-glass holds every capability over every machine",
              granted == [permissions.VIEW] and lapsed == [])

        print("\n== The catalogue a borrowing hub is handed ==")
        roster = {"PC-1": {"online": True, "last_seen": 1700000000}}
        catalogue = sharing.catalogue_for_peer(
            db_path, who, superusers=superusers, roster=roster)
        check("one entry for the one live share", len(catalogue) == 1)
        entry = catalogue[0]
        check("it names the share, not the machine's identity on this hub",
              entry["share_id"] == share["share_id"])
        check("...and reports the machine online from the roster", entry["online"] is True)
        check("...carrying only the surviving capabilities",
              entry["capabilities"] == [permissions.REMOTE_CONTROL])

        gone_share = sharing.create_share(
            db_path, peer["peer_id"], "PC-404", [permissions.VIEW],
            created_by="root@x.com")
        catalogue = sharing.catalogue_for_peer(
            db_path, who, superusers=superusers, roster=roster)
        offline = [e for e in catalogue if e["hostname"] == "PC-404"]
        check("a machine missing from the roster is listed OFFLINE, not dropped",
              len(offline) == 1 and offline[0]["online"] is False)
        sharing.revoke_share(db_path, gone_share["share_id"], actor="root@x.com")

        expired_share = sharing.create_share(
            db_path, peer["peer_id"], "PC-3", [permissions.VIEW],
            created_by="root@x.com", expires_at=time.time() + 60)
        check("an expired share leaves the catalogue entirely",
              all(e["share_id"] != expired_share["share_id"]
                  for e in sharing.catalogue_for_peer(
                      db_path, who, superusers=superusers, roster=roster,
                      now=time.time() + 120)))

        print("\n== Editing and revoking ==")
        updated = sharing.update_share(
            db_path, share["share_id"],
            capabilities=[permissions.VIEW, permissions.REMOTE_CONTROL],
            actor="owner@x.com")
        check("a share can be widened in place",
              updated["capabilities"] == [permissions.VIEW, permissions.REMOTE_CONTROL])
        check("an unmentioned expiry is left alone, not cleared",
              updated["expires_at"] is None)
        with_expiry = sharing.update_share(
            db_path, share["share_id"], expires_at=int(time.time()) + 3600,
            actor="owner@x.com")
        check("an expiry can be set", with_expiry["expires_at"] is not None)
        cleared = sharing.update_share(db_path, share["share_id"], expires_at=None,
                                       actor="owner@x.com")
        check("...and cleared, which is why the sentinel is not None",
              cleared["expires_at"] is None)

        check("revoking reports that it did something",
              sharing.revoke_share(db_path, share["share_id"], actor="owner@x.com"))
        check("...and only once",
              not sharing.revoke_share(db_path, share["share_id"], actor="owner@x.com"))
        state, err = sharing.authorize_peer_action(
            db_path, who, share["share_id"], permissions.REMOTE_CONTROL,
            superusers=superusers)
        check("a revoked share is refused on the very next request", state is None)
        check("...as a plain miss, with nothing to probe", err == "No such share.")

        print("\n== Revoking a peer takes its shares with it ==")
        sharing.create_share(db_path, peer["peer_id"], "PC-1", [permissions.VIEW],
                             created_by="owner@x.com")
        sharing.create_share(db_path, peer["peer_id"], "PC-2", [permissions.VIEW],
                             created_by="root@x.com")
        # Three rows, not two: PC-3's share has passed its expiry but has never been
        # revoked. list_shares returns ROWS -- expiry is a serving decision made per
        # request, so an expired share is still something revoking the peer must sweep up.
        check("three unrevoked shares to that hub",
              len(sharing.list_shares(db_path, peer_id=peer["peer_id"])) == 3)
        check("revoking the peer reports how many shares went with it",
              sharing.revoke_peer(db_path, peer["peer_id"], actor="root@x.com") == 3)
        check("...leaving it none",
              sharing.list_shares(db_path, peer_id=peer["peer_id"]) == [])
        check("...and its token dead",
              sharing.authenticate_peer(db_path, header) is None)
        check("revoking it again is a no-op that says so",
              sharing.revoke_peer(db_path, peer["peer_id"], actor="root@x.com") is None)
        check("the other hub's share is untouched",
              len(sharing.list_shares(db_path, peer_id=other_peer["peer_id"])) == 1)

        print("\n== A machine leaving the fleet takes its shares with it ==")
        sharing.create_share(db_path, other_peer["peer_id"], "PC-7", [permissions.VIEW],
                             created_by="root@x.com")
        check("forget_machine revokes every share of it",
              sharing.forget_machine(db_path, "PC-7") == 1)
        check("...and there is nothing left to revoke",
              sharing.forget_machine(db_path, "PC-7") == 0)

        merged = sharing.create_share(db_path, other_peer["peer_id"], "OLD-NAME",
                                      [permissions.VIEW], created_by="root@x.com")
        check("rename_machine follows a merge",
              sharing.rename_machine(db_path, "OLD-NAME", "NEW-NAME") == 1)
        check("...so the share still resolves",
              sharing.get_share(db_path, merged["share_id"])["machine"] == "NEW-NAME")
        check("renaming to itself is a no-op",
              sharing.rename_machine(db_path, "NEW-NAME", "NEW-NAME") == 0)

        print("\n== The borrowing side: a link, and a cache that is only a cache ==")
        link = sharing.create_link(db_path, "https://owner.example.com/",
                                   "Alice's hub", created_by="borrower@x.com")
        check("the address is normalised on the way in",
              link["base_url"] == "https://owner.example.com")
        try:
            sharing.create_link(db_path, "https://owner.example.com", "Again",
                                created_by="borrower@x.com")
            check("the same hub cannot be linked twice", False)
        except sharing.SharingError:
            check("the same hub cannot be linked twice", True)

        with fleet.get_conn(db_path) as conn:
            link_row = dict(conn.execute(
                "SELECT * FROM share_links WHERE link_id = ?",
                (link["link_id"],)).fetchone())
        check("the peer token is nowhere in the link table -- it lives in the key store",
              not any("token" in col for col in link_row))

        added, removed = sharing.replace_borrowed(db_path, link["link_id"], [
            {"share_id": "s1", "hostname": "BOBS-PC", "online": True,
             "capabilities": [permissions.REMOTE_CONTROL]},
            {"share_id": "s2", "hostname": "BOBS-LAPTOP", "online": False,
             "capabilities": [permissions.VIEW]},
        ])
        check("a first read is all additions", (added, removed) == (["s1", "s2"], []))
        check("both machines are cached",
              len(sharing.list_borrowed(db_path, link["link_id"])) == 2)

        added, removed = sharing.replace_borrowed(db_path, link["link_id"], [
            {"share_id": "s1", "hostname": "BOBS-PC", "online": True,
             "capabilities": [permissions.REMOTE_CONTROL]},
        ])
        check("a revoked share disappears wholesale, not by merge",
              (added, removed) == ([], ["s2"]))
        check("...leaving one machine",
              len(sharing.list_borrowed(db_path, link["link_id"])) == 1)

        hostile, _ = sharing.replace_borrowed(db_path, link["link_id"], [
            {"share_id": "s1", "hostname": "BOBS-PC",
             "capabilities": [permissions.REMOTE_CONTROL, permissions.MANAGE_SETTINGS,
                              "fly_the_plane"]},
        ])
        cached = sharing.get_borrowed(db_path, link["link_id"], "s1")
        check("a catalogue is INPUT: an unshareable capability never reaches the cache",
              cached["capabilities"] == [permissions.REMOTE_CONTROL])
        check("borrowed_can decides which buttons to draw, and only that",
              sharing.borrowed_can(cached, permissions.REMOTE_CONTROL)
              and not sharing.borrowed_can(cached, permissions.VIEW))
        check("a borrowed machine is badged as borrowed everywhere",
              cached["borrowed"] is True)
        check("a fresh read is not stale", cached["stale"] is False)
        check("...and an old one is",
              sharing.get_borrowed(db_path, link["link_id"], "s1",
                                   now=time.time() + sharing.BORROWED_STALE_SECONDS + 60
                                   )["stale"] is True)

        sharing.record_link_result(db_path, link["link_id"], ok=False,
                                   error="peer unreachable")
        sharing.record_link_result(db_path, link["link_id"], ok=True)
        after = sharing.get_link(db_path, link["link_id"])
        check("a good poll records success", after["last_ok_at"] is not None)
        check("...without erasing that it was broken an hour ago",
              after["last_error"] == "peer unreachable")

        check("deleting the link reports that it did something",
              sharing.delete_link(db_path, link["link_id"], actor="borrower@x.com"))
        check("...and takes the borrowed machines with it",
              sharing.list_borrowed(db_path, link["link_id"]) == [])
        check("...and is a no-op the second time",
              not sharing.delete_link(db_path, link["link_id"], actor="borrower@x.com"))

        print("\n== The audit trail ==")
        entries = fleet.list_audit(db_path, limit=500)["entries"]
        actions = [e["action"] for e in entries]
        for action in ("share.pair_offer", "share.peer_paired", "share.peer_first_use",
                       "share.peer_revoke", "share.create", "share.update",
                       "share.revoke", "share.link_add", "share.link_remove"):
            check(f"{action} is recorded", action in actions)
        check("every sharing GRANT action is security level",
              all(e["level"] == fleet.LEVEL_SECURITY for e in entries
                  if e["action"].startswith("share.")
                  and e["action"] != "share.catalogue_change"))
        check("a share row names the machine as its target, with the peer in details",
              any(e["target"] == "PC-1" and (e["detail"] or {}).get("peer_id")
                  for e in entries if e["action"] == "share.create"))
        check("no audit detail ever carries a token",
              not any("tmh_" in str(e["detail"]) for e in entries))
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
