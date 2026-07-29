"""Unit tests for permissions.py -- the access-control core, with no Flask involved.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
Under pytest, conftest.py wraps `check` so a false condition fails the test properly.

The emphasis here is on the two ways this module can be WRONG in a way nobody notices:
granting more than intended (a stale capability string, an empty scope reading as
unrestricted) and granting less than intended (a merge quietly dropping a machine out
of every group).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
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


def audit_actions(db_path):
    with fleet.get_conn(db_path) as conn:
        return [r["action"] for r in conn.execute(
            "SELECT action FROM audit_log ORDER BY id")]


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        permissions.init_permissions_db(db_path)
        fleet.init_fleet_db(db_path)          # audit_log
        permissions.invalidate()

        superusers = {"root@x.com"}

        print("\n== The capability registry is self-consistent ==")
        # permissions_web.permissions_capabilities indexes CAPABILITY_LABELS by name, so a
        # capability added without a label is a 500 on the group editor, not a missing row.
        check("every capability has a label",
              set(permissions.CAPABILITY_LABELS) == set(permissions.CAPABILITIES))

        print("\n== Break-glass ==")
        p = permissions.effective_permissions(db_path, "root@x.com", superusers)
        check("superuser holds every capability",
              p["capabilities"] == set(permissions.CAPABILITIES))
        check("superuser scope is unrestricted (None, not a snapshot)",
              p["machines"] is None)
        check("superuser flagged", p["superuser"] is True)
        check("superuser matching is case/space insensitive",
              permissions.effective_permissions(
                  db_path, "  Root@X.com ", superusers)["superuser"] is True)
        check("machine_in_scope is true for anything when unrestricted",
              permissions.machine_in_scope(p, "anything-at-all"))
        check("visible_machine_filter is None when unrestricted",
              permissions.visible_machine_filter(p) is None)

        print("\n== A user in no group has nothing ==")
        p = permissions.effective_permissions(db_path, "nobody@x.com", superusers)
        check("no capabilities", p["capabilities"] == set())
        # The dangerous mistake: an empty scope must mean "nothing", never "everything".
        check("empty scope is a set, NOT None", p["machines"] == set())
        check("machine_in_scope false for an unknown machine",
              permissions.machine_in_scope(p, "PC-1") is False)

        print("\n== Creating a group ==")
        hospital = permissions.create_group(
            db_path, name="Hospital IT",
            description="Clinical PCs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PC-1", "PC-2"],
            members=["Ann@X.com"],
            actor="root@x.com",
        )
        group = permissions.get_group(db_path, hospital)
        check("group stored", group is not None and group["name"] == "Hospital IT")
        check("capabilities sorted into declaration order",
              group["capabilities"] == [permissions.VIEW, permissions.ISSUE_COMMANDS])
        check("machines normalised and sorted", group["machines"] == ["PC-1", "PC-2"])
        check("member email lowercased", group["members"] == ["ann@x.com"])
        check("scope defaults to the explicit list",
              group["scope_mode"] == permissions.SCOPE_LIST)
        check("creation is audited", "permission_group.create" in audit_actions(db_path))

        print("\n== Effective permissions from one group ==")
        p = permissions.effective_permissions(db_path, "ann@x.com", superusers)
        check("not a superuser", p["superuser"] is False)
        check("capabilities from the group",
              p["capabilities"] == {permissions.VIEW, permissions.ISSUE_COMMANDS})
        check("scope from the group", p["machines"] == {"PC-1", "PC-2"})
        check("in-scope machine allowed", permissions.machine_in_scope(p, "PC-1"))
        check("out-of-scope machine refused",
              permissions.machine_in_scope(p, "HR-9") is False)
        check("has_capability true for a held capability",
              permissions.has_capability(p, permissions.VIEW))
        check("has_capability false for one not held",
              permissions.has_capability(p, permissions.MANAGE_SETTINGS) is False)
        keep = permissions.visible_machine_filter(p)
        check("filter narrows a list",
              [m for m in ["PC-1", "HR-9", "PC-2"] if keep(m)] == ["PC-1", "PC-2"])

        print("\n== Union across groups ==")
        hr = permissions.create_group(
            db_path, name="HR IT",
            capabilities=[permissions.MANAGE_SETTINGS],
            machines=["HR-9"],
            members=["ann@x.com", "bob@x.com"],
            actor="root@x.com",
        )
        p = permissions.effective_permissions(db_path, "ann@x.com", superusers)
        check("capabilities union",
              p["capabilities"] == {permissions.VIEW, permissions.ISSUE_COMMANDS,
                                    permissions.MANAGE_SETTINGS})
        check("machine scope union", p["machines"] == {"PC-1", "PC-2", "HR-9"})
        p_bob = permissions.effective_permissions(db_path, "bob@x.com", superusers)
        check("a member of only one group gets only that group",
              p_bob["machines"] == {"HR-9"})
        check("groups_for_email returns both of Ann's",
              {g["id"] for g in permissions.groups_for_email(db_path, "ann@x.com")}
              == {hospital, hr})

        print("\n== scope_mode = all ==")
        auditors = permissions.create_group(
            db_path, name="Auditors", capabilities=[permissions.VIEW],
            scope_mode=permissions.SCOPE_ALL, members=["cat@x.com"], actor="root@x.com")
        p = permissions.effective_permissions(db_path, "cat@x.com", superusers)
        check("an 'all' group makes scope unrestricted", p["machines"] is None)
        check("but capabilities are still only what the group grants",
              p["capabilities"] == {permissions.VIEW})
        check("an 'all' group anywhere in the union wins",
              permissions.effective_permissions(
                  db_path, "cat@x.com", superusers)["machines"] is None)

        print("\n== Validation ==")
        def rejects(label, fn):
            try:
                fn()
                check(label, False)
            except ValueError:
                check(label, True)

        rejects("blank name rejected",
                lambda: permissions.create_group(db_path, name="   "))
        rejects("duplicate name rejected",
                lambda: permissions.create_group(db_path, name="Hospital IT"))
        rejects("duplicate name is case-insensitive",
                lambda: permissions.create_group(db_path, name="hospital it"))
        rejects("unknown capability rejected",
                lambda: permissions.create_group(db_path, name="Bad",
                                                 capabilities=["be_admin"]))
        # NB: "ad_ou" used to be the example of an unknown mode here. It is a real mode
        # since roadmap #4 landed, so this needs a genuinely unknown one -- otherwise the
        # check quietly stops testing anything the day the vocabulary grows.
        rejects("unknown scope mode rejected",
                lambda: permissions.create_group(db_path, name="Bad2",
                                                 scope_mode="everything_everywhere"))
        rejects("a non-email member rejected",
                lambda: permissions.create_group(db_path, name="Bad3",
                                                 members=["not-an-email"]))
        check("nothing was created by the rejected calls",
              len(permissions.list_groups(db_path)) == 3)

        print("\n== capabilities as a {name: bool} map (what the form posts) ==")
        mapped = permissions.create_group(
            db_path, name="Mapped",
            capabilities={permissions.VIEW: True, permissions.MANAGE_BACKUPS: False},
            actor="root@x.com")
        check("only the true entries are kept",
              permissions.get_group(db_path, mapped)["capabilities"] == [permissions.VIEW])
        permissions.delete_group(db_path, mapped, actor="root@x.com")

        print("\n== Updating ==")
        permissions.update_group(db_path, hospital, machines=["PC-1", "PC-3"],
                                 actor="ann@x.com")
        check("machines replaced wholesale",
              permissions.get_group(db_path, hospital)["machines"] == ["PC-1", "PC-3"])
        check("an omitted field is untouched",
              permissions.get_group(db_path, hospital)["members"] == ["ann@x.com"])
        permissions.update_group(db_path, hospital, members=[], actor="ann@x.com")
        check("an explicit empty list DOES clear",
              permissions.get_group(db_path, hospital)["members"] == [])
        p = permissions.effective_permissions(db_path, "ann@x.com", superusers)
        check("removing a member revokes their access through that group",
              p["machines"] == {"HR-9"})
        check("update is audited", "permission_group.update" in audit_actions(db_path))

        before = len(audit_actions(db_path))
        permissions.update_group(db_path, hospital, name="Hospital IT", actor="ann@x.com")
        check("a no-op update writes no audit row",
              len(audit_actions(db_path)) == before)

        try:
            permissions.update_group(db_path, "nope", name="x")
            check("updating an unknown group raises KeyError", False)
        except KeyError:
            check("updating an unknown group raises KeyError", True)

        print("\n== Machine lifecycle hooks ==")
        permissions.update_group(db_path, hospital, machines=["PC-1", "PC-3"],
                                 members=["ann@x.com"], actor="root@x.com")
        moved = permissions.rename_machine(db_path, "PC-3", "PC-3-RENAMED")
        check("rename_machine reports the row it moved", moved == 1)
        check("a merged machine keeps its grant under the survivor's name",
              "PC-3-RENAMED" in permissions.get_group(db_path, hospital)["machines"])
        check("and the old name is gone",
              "PC-3" not in permissions.get_group(db_path, hospital)["machines"])

        # A group already scoped to BOTH names must collapse, not collide on the PK.
        permissions.update_group(db_path, hospital, machines=["PC-1", "PC-4"],
                                 actor="root@x.com")
        permissions.rename_machine(db_path, "PC-4", "PC-1")
        check("renaming onto a name the group already has collapses cleanly",
              permissions.get_group(db_path, hospital)["machines"] == ["PC-1"])

        permissions.update_group(db_path, hospital, machines=["PC-1", "PC-2"],
                                 actor="root@x.com")
        removed = permissions.forget_machine(db_path, "PC-2")
        check("forget_machine reports what it dropped", removed == 1)
        check("a deleted machine is out of every group's scope",
              permissions.get_group(db_path, hospital)["machines"] == ["PC-1"])

        print("\n== members_of_machine ==")
        check("lists group members that can reach a machine",
              permissions.members_of_machine(db_path, "PC-1") == ["ann@x.com", "cat@x.com"])
        check("an 'all' group's members reach a machine nobody listed",
              "cat@x.com" in permissions.members_of_machine(db_path, "brand-new-pc"))

        print("\n== Deleting ==")
        permissions.delete_group(db_path, auditors, actor="root@x.com")
        check("group gone", permissions.get_group(db_path, auditors) is None)
        check("its member loses everything",
              permissions.effective_permissions(
                  db_path, "cat@x.com", superusers)["capabilities"] == set())
        check("delete is audited", "permission_group.delete" in audit_actions(db_path))
        with permissions.get_conn(db_path) as conn:
            orphans = conn.execute(
                "SELECT COUNT(*) AS n FROM permission_group_members WHERE group_id = ?",
                (auditors,)).fetchone()["n"]
        check("member rows are cleaned up too", orphans == 0)
        try:
            permissions.delete_group(db_path, auditors)
            check("deleting an unknown group raises KeyError", False)
        except KeyError:
            check("deleting an unknown group raises KeyError", True)

        print("\n== Break-glass survives an empty group table ==")
        for group in permissions.list_groups(db_path):
            permissions.delete_group(db_path, group["id"], actor="root@x.com")
        check("no groups left", permissions.list_groups(db_path) == [])
        check("the superuser still holds everything -- no lockout",
              permissions.effective_permissions(
                  db_path, "root@x.com", superusers)["capabilities"]
              == set(permissions.CAPABILITIES))

        print("\n== A capability retired from CAPABILITIES is revoked, not honoured ==")
        stale = permissions.create_group(db_path, name="Stale",
                                         capabilities=[permissions.VIEW],
                                         members=["dee@x.com"], actor="root@x.com")
        with permissions.get_conn(db_path) as conn:
            conn.execute(
                "UPDATE permission_groups SET capabilities_json = ? WHERE id = ?",
                ('["view", "become_root"]', stale))
        permissions.invalidate()
        check("an unknown capability string in the DB is dropped on read",
              permissions.effective_permissions(
                  db_path, "dee@x.com", superusers)["capabilities"] == {permissions.VIEW})

        print("\n== A corrupt capabilities blob fails CLOSED ==")
        with permissions.get_conn(db_path) as conn:
            conn.execute("UPDATE permission_groups SET capabilities_json = ? WHERE id = ?",
                         ("{not json", stale))
        permissions.invalidate()
        check("corrupt row grants nothing",
              permissions.effective_permissions(
                  db_path, "dee@x.com", superusers)["capabilities"] == set())

        print("\n== Cache coherence ==")
        permissions.invalidate()
        permissions.list_groups(db_path)      # warm it
        fresh = permissions.create_group(db_path, name="Fresh",
                                         capabilities=[permissions.VIEW],
                                         members=["eve@x.com"], actor="root@x.com")
        check("a write invalidates the cache for the next read",
              permissions.has_capability(
                  permissions.effective_permissions(db_path, "eve@x.com", superusers),
                  permissions.VIEW))
        returned = permissions.get_group(db_path, fresh)
        returned["machines"].append("SHOULD-NOT-STICK")
        check("callers get copies -- mutating a result can't poison the cache",
              permissions.get_group(db_path, fresh)["machines"] == [])
        returned = permissions.get_group(db_path, fresh)
        returned["directory_groups"].append("SHOULD-NOT-STICK")
        check("...directory_groups is copied too, not shared with the cache",
              permissions.get_group(db_path, fresh)["directory_groups"] == [])

        # ============================================================
        # DIRECTORY GROUP MAPPING (roadmap #4)
        # ============================================================
        # The risk here is asymmetric. Granting too little is visible -- someone cannot
        # sign in and says so. Granting too MUCH is silent: a token that matches more
        # loosely than the admin believed hands a stranger an operator's capabilities,
        # and nothing in the UI would look wrong. Most of these tests are about the
        # second kind.
        print("\n== Reading group tokens out of provider claims ==")
        claims = permissions.directory_groups_from_claims
        check("Entra's `groups` array is read",
              claims({"groups": ["8F4C1E02-AAAA", "b-222"]}) == ["8f4c1e02-aaaa", "b-222"])
        check("`roles` and `wids` union in with `groups`",
              set(claims({"groups": ["a"], "roles": ["b"], "wids": ["c"]}))
              == {"a", "b", "c"})
        check("a single-valued claim sent as a bare string still works",
              claims({"groups": "hospital-it"}) == ["hospital-it"])
        check("no group claims at all -> no tokens (not an error)",
              claims({"email": "x@y.com"}) == [])
        check("a claim of the wrong shape grants nothing",
              claims({"groups": {"nested": "object"}}) == [])
        # A non-string inside the array must be DROPPED, not str()'d: a claim of [None]
        # stringifying to "none" could collide with a real group actually named "None".
        check("non-string entries are dropped rather than stringified",
              claims({"groups": ["ok", None, 17, {"a": 1}]}) == ["ok"])
        check("duplicates across claims collapse",
              claims({"groups": ["Same"], "roles": ["same"]}) == ["same"])

        print("\n== Group-claim overage is distinguishable from 'no groups' ==")
        # Entra withholds `groups` past ~200 and sends _claim_names instead. Both look
        # like "in no mapped group" downstream; only one is a misconfiguration.
        check("an overage response is detected",
              permissions.has_group_claim_overage(
                  {"_claim_names": {"groups": "src1"},
                   "_claim_sources": {"src1": {"endpoint": "https://graph..."}}}))
        check("an ordinary response is not mistaken for one",
              not permissions.has_group_claim_overage({"groups": ["a"]}))
        check("a junk _claim_names does not raise",
              not permissions.has_group_claim_overage({"_claim_names": "nonsense"}))

        print("\n== A mapped directory group grants its permission group ==")
        permissions.invalidate()
        hospital = permissions.create_group(
            db_path, name="Hospital directory",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["HOSP-1"],
            directory_groups=["CN=Hospital IT,OU=Groups,DC=x"],
            actor="root@x.com")
        p = permissions.effective_permissions(
            db_path, "nobody@x.com", superusers,
            directory_groups=["cn=hospital it,ou=groups,dc=x"])
        check("a user in no group by email is granted via the directory token",
              p["capabilities"] == {permissions.VIEW, permissions.ISSUE_COMMANDS})
        check("...including its machine scope", p["machines"] == {"HOSP-1"})
        check("...and the group is named in `groups`",
              [g["id"] for g in p["groups"]] == [hospital])

        print("\n== Token matching is case-insensitive but not fuzzy ==")
        # DNs and GUIDs are case-insensitive at the source, so a mapping typed in one
        # case must match a claim sent in another -- but nothing looser than that.
        check("a differently-cased claim matches",
              permissions.effective_permissions(
                  db_path, "nobody@x.com", superusers,
                  directory_groups=["CN=HOSPITAL IT,OU=GROUPS,DC=X"]
              )["capabilities"] != set())
        check("surrounding whitespace is tolerated",
              permissions.effective_permissions(
                  db_path, "nobody@x.com", superusers,
                  directory_groups=["  cn=hospital it,ou=groups,dc=x  "]
              )["capabilities"] != set())
        # The important negatives: anything short of an exact (folded) match grants
        # NOTHING. A prefix match here would mean "CN=Hospital" opening the Hospital
        # group to any directory group whose name starts the same way.
        for wrong in ("cn=hospital it", "hospital it", "cn=hospital it,ou=groups,dc=x,dc=y",
                      "xcn=hospital it,ou=groups,dc=x", ""):
            check(f"a near-miss token grants nothing: {wrong!r}",
                  permissions.effective_permissions(
                      db_path, "nobody@x.com", superusers,
                      directory_groups=[wrong])["capabilities"] == set())
        check("an unmapped token grants nothing",
              permissions.effective_permissions(
                  db_path, "nobody@x.com", superusers,
                  directory_groups=["cn=some other group"])["capabilities"] == set())

        print("\n== Directory and email grants union, without double-counting ==")
        permissions.update_group(db_path, hospital, members=["frank@x.com"],
                                 actor="root@x.com")
        p = permissions.effective_permissions(
            db_path, "frank@x.com", superusers,
            directory_groups=["cn=hospital it,ou=groups,dc=x"])
        check("someone who is both a member AND in the mapped group sees it once",
              [g["id"] for g in p["groups"]].count(hospital) == 1)
        both = permissions.create_group(
            db_path, name="Lab directory", capabilities=[permissions.MANAGE_BACKUPS],
            machines=["LAB-1"], directory_groups=["lab-guid"], actor="root@x.com")
        p = permissions.effective_permissions(
            db_path, "frank@x.com", superusers,
            directory_groups=["cn=hospital it,ou=groups,dc=x", "lab-guid"])
        check("capabilities union across an email group and a directory group",
              p["capabilities"] == {permissions.VIEW, permissions.ISSUE_COMMANDS,
                                    permissions.MANAGE_BACKUPS})
        check("...and so do their machine scopes", p["machines"] == {"HOSP-1", "LAB-1"})

        print("\n== Passing no directory groups changes nothing ==")
        # Google sign-in sends no group claims at all, so this is the common path: it
        # must behave exactly as it did before the feature existed.
        check("omitting the argument entirely still resolves email membership",
              permissions.effective_permissions(
                  db_path, "frank@x.com", superusers)["capabilities"]
              == {permissions.VIEW, permissions.ISSUE_COMMANDS})
        check("an empty token list grants nothing extra",
              permissions.effective_permissions(
                  db_path, "nobody@x.com", superusers,
                  directory_groups=[])["capabilities"] == set())

        print("\n== The sign-in intersection (what the session is allowed to carry) ==")
        mapped = permissions.mapped_directory_groups(db_path)
        check("every configured token is reported",
              {"cn=hospital it,ou=groups,dc=x", "lab-guid"} <= mapped)
        check("a token nothing maps is not",
              "cn=some other group" not in mapped)

        print("\n== Editing and clearing mappings ==")
        permissions.update_group(db_path, hospital,
                                 directory_groups=["cn=new group"], actor="root@x.com")
        check("the old token stops granting immediately",
              permissions.effective_permissions(
                  db_path, "nobody@x.com", superusers,
                  directory_groups=["cn=hospital it,ou=groups,dc=x"]
              )["capabilities"] == set())
        check("the new one grants",
              permissions.effective_permissions(
                  db_path, "nobody@x.com", superusers,
                  directory_groups=["cn=new group"])["capabilities"] != set())
        # None means "leave alone" here exactly as it does for machines/members -- the
        # Follow-Fleet bug in backups.py was this distinction being got wrong once.
        permissions.update_group(db_path, hospital, name="Hospital directory",
                                 actor="root@x.com")
        check("update with directory_groups omitted leaves them untouched",
              permissions.get_group(db_path, hospital)["directory_groups"]
              == ["cn=new group"])
        permissions.update_group(db_path, hospital, directory_groups=[],
                                 actor="root@x.com")
        check("an explicit empty list clears them",
              permissions.get_group(db_path, hospital)["directory_groups"] == [])
        check("...and the token grants nothing afterwards",
              permissions.effective_permissions(
                  db_path, "nobody@x.com", superusers,
                  directory_groups=["cn=new group"])["capabilities"] == set())
        check("clearing the mapping did not disturb the email members",
              permissions.get_group(db_path, hospital)["members"] == ["frank@x.com"])

        print("\n== Editing one half of the member table leaves the other alone ==")
        # email rows and ad_group_dn rows share a table; a DELETE that forgot its
        # `ad_group_dn IS NOT NULL` guard would silently drop mappings on a member edit.
        permissions.update_group(db_path, both, members=["gina@x.com"],
                                 actor="root@x.com")
        check("saving members preserved the directory mappings",
              permissions.get_group(db_path, both)["directory_groups"] == ["lab-guid"])
        permissions.update_group(db_path, both, directory_groups=["lab-guid", "lab-2"],
                                 actor="root@x.com")
        check("saving mappings preserved the email members",
              permissions.get_group(db_path, both)["members"] == ["gina@x.com"])

        print("\n== Directory-group validation ==")
        for bad, why in ((["x" * 600], "over the length cap"), ([None], "not a string"),
                         ([{"dn": "x"}], "not a string")):
            try:
                permissions.create_group(db_path, name=f"Bad {why}",
                                         directory_groups=bad, actor="root@x.com")
                check(f"rejects a token {why}", False)
            except ValueError:
                check(f"rejects a token {why}", True)
        blank = permissions.create_group(db_path, name="Blank tokens",
                                         directory_groups=["  ", "", "real"],
                                         actor="root@x.com")
        check("blank tokens are dropped, not stored as grant-nothing rows",
              permissions.get_group(db_path, blank)["directory_groups"] == ["real"])

        print("\n== Deleting a group drops its mappings ==")
        permissions.delete_group(db_path, both, actor="root@x.com")
        check("the mapped token no longer grants",
              permissions.effective_permissions(
                  db_path, "nobody@x.com", superusers,
                  directory_groups=["lab-guid"])["capabilities"] == set())
        check("and it is gone from the sign-in intersection",
              "lab-guid" not in permissions.mapped_directory_groups(db_path))
        with permissions.get_conn(db_path) as conn:
            left = conn.execute(
                "SELECT COUNT(*) c FROM permission_group_members WHERE group_id = ?",
                (both,)).fetchone()["c"]
        check("no orphan member rows are left behind", left == 0)

        # ============================================================
        # AD OU SCOPE MODE (roadmap #4)
        # ============================================================
        # A group scoped to an OU derives its machine list when the cache is built. The
        # failure that matters is the derived list being WIDER than the OU -- an operator
        # silently gaining machines nobody granted them -- so most of these are negatives.
        print("\n== scope_mode = ad_ou ==")
        import directory
        with permissions.get_conn(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS machine_info ("
                         "machine TEXT PRIMARY KEY, asset_tag TEXT, serial_number TEXT, "
                         "model TEXT, updated_at TEXT)")
        directory.init_directory_db(db_path)
        with permissions.get_conn(db_path) as conn:
            for machine, ad_ou in (
                ("WARD-1", "OU=Ward 3,OU=Clinical,DC=corp,DC=local"),
                ("CLIN-1", "OU=Clinical,DC=corp,DC=local"),
                ("FIN-1", "OU=Finance,DC=corp,DC=local"),
                ("NOTCLIN-1", "OU=NotClinical,DC=corp,DC=local"),
                ("NOAD-1", None),
            ):
                conn.execute("INSERT OR REPLACE INTO machine_info(machine, ad_ou) "
                             "VALUES (?, ?)", (machine, ad_ou))
        permissions.invalidate()

        clinical = permissions.create_group(
            db_path, name="Clinical OU", capabilities=[permissions.VIEW],
            scope_mode=permissions.SCOPE_AD_OU,
            ous=["OU=Clinical,DC=corp,DC=local"],
            members=["hank@x.com"], actor="root@x.com")
        p = permissions.effective_permissions(db_path, "hank@x.com", superusers)
        check("an ad_ou group resolves to the machines in that OU",
              p["machines"] == {"WARD-1", "CLIN-1"})
        check("...which includes machines in NESTED OUs", "WARD-1" in p["machines"])
        check("a machine in a sibling OU is NOT in scope", "FIN-1" not in p["machines"])
        # The suffix-match bug, at the level that actually grants access.
        check("a similarly-named OU does not leak in", "NOTCLIN-1" not in p["machines"])
        check("a machine with no AD record is not in scope", "NOAD-1" not in p["machines"])
        check("scope is a real set, never unrestricted", p["machines"] is not None)
        check("machine_in_scope agrees",
              permissions.machine_in_scope(p, "CLIN-1")
              and not permissions.machine_in_scope(p, "FIN-1"))

        print("\n== An ad_ou group with no OUs grants NOTHING ==")
        # Fails closed. An empty scope must never read as "all machines".
        empty_ou = permissions.create_group(
            db_path, name="Empty OU", capabilities=[permissions.VIEW],
            scope_mode=permissions.SCOPE_AD_OU, members=["iris@x.com"],
            actor="root@x.com")
        p = permissions.effective_permissions(db_path, "iris@x.com", superusers)
        check("no OUs -> no machines", p["machines"] == set())
        check("...and not unrestricted", p["machines"] is not None)

        print("\n== The derived list follows the directory ==")
        # This is the invalidation contract: a sync moves a machine, invalidates, and the
        # next resolve reflects it. Without that a machine keeps its old group's access
        # until the hub restarts.
        with permissions.get_conn(db_path) as conn:
            conn.execute("UPDATE machine_info SET ad_ou = ? WHERE machine = ?",
                         ("OU=Finance,DC=corp,DC=local", "CLIN-1"))
        permissions.invalidate()
        p = permissions.effective_permissions(db_path, "hank@x.com", superusers)
        check("a machine moved out of the OU leaves scope", "CLIN-1" not in p["machines"])
        check("...and the rest of the OU is unaffected", p["machines"] == {"WARD-1"})
        with permissions.get_conn(db_path) as conn:
            conn.execute("UPDATE machine_info SET ad_ou = ? WHERE machine = ?",
                         ("OU=Clinical,DC=corp,DC=local", "NEWPC"))
            conn.execute("INSERT OR REPLACE INTO machine_info(machine, ad_ou) VALUES (?, ?)",
                         ("NEWPC", "OU=Clinical,DC=corp,DC=local"))
        permissions.invalidate()
        check("a machine moved INTO the OU joins scope",
              "NEWPC" in permissions.effective_permissions(
                  db_path, "hank@x.com", superusers)["machines"])

        print("\n== ad_ou composes with the other scope modes ==")
        permissions.create_group(
            db_path, name="Finance list", capabilities=[permissions.ISSUE_COMMANDS],
            machines=["FIN-1"], members=["hank@x.com"], actor="root@x.com")
        p = permissions.effective_permissions(db_path, "hank@x.com", superusers)
        check("an OU group and an explicit-list group union",
              {"WARD-1", "NEWPC", "FIN-1"} <= p["machines"])
        check("...and their capabilities union too",
              p["capabilities"] == {permissions.VIEW, permissions.ISSUE_COMMANDS})
        permissions.create_group(
            db_path, name="Everything", capabilities=[permissions.VIEW],
            scope_mode=permissions.SCOPE_ALL, members=["hank@x.com"], actor="root@x.com")
        check("a scope_mode=all group still wins over any derived list",
              permissions.effective_permissions(
                  db_path, "hank@x.com", superusers)["machines"] is None)

        print("\n== OU validation ==")
        for bad, why in ((["PC-1"], "a hostname, not a DN"),
                         ([None], "not a string"),
                         (["x" * 2000], "over the length cap")):
            try:
                permissions.create_group(db_path, name=f"Bad OU {why}",
                                         scope_mode=permissions.SCOPE_AD_OU,
                                         ous=bad, actor="root@x.com")
                check(f"rejects {why}", False)
            except ValueError:
                check(f"rejects {why}", True)
        kept = permissions.create_group(
            db_path, name="Case kept", scope_mode=permissions.SCOPE_AD_OU,
            ous=["OU=Clinical,DC=Corp", "  ", ""], actor="root@x.com")
        check("blank OUs are dropped and the admin's case is preserved",
              permissions.get_group(db_path, kept)["ous"] == ["OU=Clinical,DC=Corp"])
        rejects("an unknown scope mode is still refused",
                lambda: permissions.create_group(db_path, name="Bad mode",
                                                 scope_mode="whatever"))

        print("\n== Editing and clearing OUs ==")
        permissions.update_group(db_path, clinical, ous=["OU=Finance,DC=corp,DC=local"],
                                 actor="root@x.com")
        check("re-pointing the OU re-derives the group's machines",
              set(permissions.get_group(db_path, clinical)["machines"]) == {"FIN-1", "CLIN-1"})
        permissions.update_group(db_path, clinical, name="Clinical OU",
                                 actor="root@x.com")
        check("update with ous omitted leaves them alone",
              permissions.get_group(db_path, clinical)["ous"]
              == ["OU=Finance,DC=corp,DC=local"])
        permissions.update_group(db_path, clinical, ous=[], actor="root@x.com")
        check("an explicit empty list clears them",
              permissions.get_group(db_path, clinical)["ous"] == [])
        check("...and the group then grants no machines",
              permissions.get_group(db_path, clinical)["machines"] == [])

        print("\n== Deleting an ad_ou group cleans up its OU rows ==")
        permissions.delete_group(db_path, empty_ou, actor="root@x.com")
        with permissions.get_conn(db_path) as conn:
            left = conn.execute("SELECT COUNT(*) c FROM permission_group_ous "
                                "WHERE group_id = ?", (empty_ou,)).fetchone()["c"]
        check("no orphan OU rows are left behind", left == 0)

        print("\n== A mapping change is in the security audit trail ==")
        # Granting via a directory group is granting; if it were not audited, the widest
        # grant on the hub would be the one change with no record of who made it.
        actions = audit_actions(db_path)
        check("group edits are audited", "permission_group.update" in actions)
        with fleet.get_conn(db_path) as conn:
            rows = [r["detail_json"] for r in conn.execute(
                "SELECT detail_json FROM audit_log "
                "WHERE action = 'permission_group.update' ORDER BY id")]
        check("the audit details name directory_groups as a changed field",
              any("directory_groups" in (d or "") for d in rows))
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
