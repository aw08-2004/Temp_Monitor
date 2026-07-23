"""Unit tests for users.py -- the registered-users directory, with no Flask involved.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
Under pytest, conftest.py wraps `check` so a false condition fails the test properly.

The emphasis is on the two behaviours that are easy to get subtly wrong: the login
upsert (auto-register once, never stomp a name on repeat logins, always stamp the
timestamp) and the fact that this is a PROFILE store -- deleting a row here must not
touch anyone's permission-group membership.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet
import permissions
import users

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
        users.init_users_db(db_path)
        fleet.init_fleet_db(db_path)          # audit_log

        print("\n== Manual create ==")
        u = users.create_user(db_path, email="Ann@X.com", full_name="Ann Adams",
                              username="aadams", department="Hospital IT",
                              actor="root@x.com")
        check("email normalised (lowercased/stripped)", u["email"] == "ann@x.com")
        check("full name stored", u["full_name"] == "Ann Adams")
        check("source is manual", u["source"] == users.SOURCE_MANUAL)
        check("never-logged-in has no last_login_at", u["last_login_at"] is None)
        check("create is audited", "user.create" in audit_actions(db_path))
        check("get_user finds it by any casing",
              users.get_user(db_path, " ANN@x.com ")["username"] == "aadams")

        print("\n== Validation ==")
        def rejects(label, fn):
            try:
                fn()
                check(label, False)
            except ValueError:
                check(label, True)

        rejects("a non-email is rejected",
                lambda: users.create_user(db_path, email="not-an-email"))
        rejects("a blank email is rejected",
                lambda: users.create_user(db_path, email="   "))
        rejects("a duplicate email is rejected",
                lambda: users.create_user(db_path, email="ann@x.com"))
        rejects("an over-long field is rejected",
                lambda: users.create_user(db_path, email="x@y.com",
                                          full_name="z" * 5000))
        check("nothing was created by the rejected calls",
              len(users.list_users(db_path)) == 1)

        print("\n== Blank optional fields become NULL, not empty strings ==")
        u2 = users.create_user(db_path, email="bob@x.com", full_name="Bob Barr",
                              username="  ", phone="")
        check("blank username stored as None", u2["username"] is None)
        check("blank phone stored as None", u2["phone"] is None)

        print("\n== Update overwrites the whole record ==")
        users.update_user(db_path, "ann@x.com", full_name="Ann A. Adams",
                          username="aadams", title="Lead Tech", actor="root@x.com")
        after = users.get_user(db_path, "ann@x.com")
        check("full name updated", after["full_name"] == "Ann A. Adams")
        check("title set", after["title"] == "Lead Tech")
        check("a field not resent is cleared (whole-record semantics)",
              after["department"] is None)
        check("update is audited", "user.update" in audit_actions(db_path))

        before = len(audit_actions(db_path))
        users.update_user(db_path, "ann@x.com", full_name="Ann A. Adams",
                          username="aadams", title="Lead Tech", actor="root@x.com")
        check("a no-op update writes no audit row",
              len(audit_actions(db_path)) == before)

        try:
            users.update_user(db_path, "ghost@x.com", full_name="x")
            check("updating an unknown user raises KeyError", False)
        except KeyError:
            check("updating an unknown user raises KeyError", True)

        print("\n== Login upsert ==")
        users.upsert_from_login(db_path, "Carol@X.com", "Carol Carr")
        carol = users.get_user(db_path, "carol@x.com")
        check("a first-time signer-in is auto-registered", carol is not None)
        check("source is login", carol["source"] == users.SOURCE_LOGIN)
        check("full name from the claim", carol["full_name"] == "Carol Carr")
        check("last_login_at stamped", carol["last_login_at"] is not None)

        first_login = carol["last_login_at"]
        # Simulate a later login carrying a different display name.
        users.upsert_from_login(db_path, "carol@x.com", "C. Carr (Contractor)")
        carol2 = users.get_user(db_path, "carol@x.com")
        check("repeat login does NOT stomp an existing full name",
              carol2["full_name"] == "Carol Carr")
        check("but it does re-stamp last_login_at",
              carol2["last_login_at"] >= first_login)

        # A manually-added user with no name yet gets one filled in on first login.
        users.create_user(db_path, email="dave@x.com", actor="root@x.com")
        users.upsert_from_login(db_path, "dave@x.com", "Dave Dunn")
        dave = users.get_user(db_path, "dave@x.com")
        check("login fills in a missing name on a manually-added user",
              dave["full_name"] == "Dave Dunn")
        check("but does not change the source away from manual",
              dave["source"] == users.SOURCE_MANUAL)

        # create_user ran for ann, bob, dave; carol arrived via upsert_from_login, which
        # is deliberately NOT audited -- so the create count stays at three.
        check("upsert is not audited (login is not an admin action)",
              audit_actions(db_path).count("user.create") == 3)

        print("\n== Search ==")
        results = users.list_users(db_path, q="carol")
        check("search matches on name", [r["email"] for r in results] == ["carol@x.com"])
        check("search matches on email substring",
              {r["email"] for r in users.list_users(db_path, q="@x.com")} ==
              {"ann@x.com", "bob@x.com", "carol@x.com", "dave@x.com"})
        check("search matches on username",
              [r["email"] for r in users.list_users(db_path, q="aadams")] == ["ann@x.com"])
        check("a search matching nothing returns empty",
              users.list_users(db_path, q="zzzzz") == [])

        print("\n== Listing order ==")
        names = [u["full_name"] or u["email"] for u in users.list_users(db_path)]
        check("sorted by display name", names == sorted(names, key=str.lower))

        print("\n== Delete does not touch access ==")
        permissions.init_permissions_db(db_path)
        permissions.invalidate()
        gid = permissions.create_group(db_path, name="Techs",
                                       capabilities=[permissions.VIEW],
                                       members=["ann@x.com"], actor="root@x.com")
        users.delete_user(db_path, "ann@x.com", actor="root@x.com")
        check("user gone from directory", users.get_user(db_path, "ann@x.com") is None)
        check("delete is audited", "user.delete" in audit_actions(db_path))
        check("but their permission-group membership is untouched",
              "ann@x.com" in permissions.get_group(db_path, gid)["members"])
        check("and their effective access is unchanged",
              permissions.effective_permissions(db_path, "ann@x.com", set())["capabilities"]
              == {permissions.VIEW})

        try:
            users.delete_user(db_path, "ann@x.com")
            check("deleting an unknown user raises KeyError", False)
        except KeyError:
            check("deleting an unknown user raises KeyError", True)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
