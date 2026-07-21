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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
        rejects("unknown scope mode rejected",
                lambda: permissions.create_group(db_path, name="Bad2",
                                                 scope_mode="ad_ou"))
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
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
