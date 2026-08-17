"""Tests hub/envfile.py: the KEY=value reader/writer, and the `.env` file ACL.

The ACL half is the reason this module exists. `.env` holds FLASK_SECRET_KEY,
AGENT_ENROLLMENT_SECRET, BACKUP_MASTER_KEY and the OAuth/LDAP secrets, and it lives under
C:\\Program Files -- which hands every file it contains an inherited
`BUILTIN\\Users:(OI)(CI)(IO)(GR,GE)`. Nothing but envfile.protect stands between that
inheritance and every local user on the hub server reading the whole secret store.

The ACL tests run as whatever account the suite runs as, which is ordinarily NOT SYSTEM or
an admin -- so after protect() this process can no longer READ the file it just wrote. That
is the correct outcome, and the assertions are written against the ACL itself rather than
against a read attempt, so they say what they mean either way.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

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


_TMPDIR = tempfile.mkdtemp(prefix="hub-envfile-test-")
_counter = [0]


def _fresh(body="FLASK_SECRET_KEY=hunter2\n"):
    _counter[0] += 1
    path = os.path.join(_TMPDIR, f"env{_counter[0]}")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return path


def _icacls(path):
    return subprocess.run(["icacls", path], capture_output=True, text=True).stdout


def _icacls_sids(path):
    """The ACL as raw SIDs. Names are localised -- BUILTIN\\Users prints as "Usuarios" on a
    Spanish install -- so anything asserting on a specific principal has to compare SIDs."""
    import win32security
    sd = win32security.GetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return ""
    return " ".join(win32security.ConvertSidToStringSid(dacl.GetAce(i)[2])
                    for i in range(dacl.GetAceCount()))


# ---------------------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------------------
def test_read_all():
    path = _fresh("A=1\n# comment\n\nexport B=2\n  C = 3 \n")
    values = envfile.read_all(path)
    check("reads plain KEY=value", values.get("A") == "1")
    check("reads `export KEY=value`", values.get("B") == "2")
    check("strips surrounding whitespace", values.get("C") == "3")
    check("comments are not keys", "# comment" not in values)
    check("a missing file reads as empty", envfile.read_all(os.path.join(_TMPDIR, "nope")) == {})


def test_read_all_tolerates_a_bom():
    path = os.path.join(_TMPDIR, "bom.env")
    with open(path, "w", encoding="utf-8-sig", newline="\n") as fh:
        fh.write("GOOGLE_CLIENT_ID=abc\n")
    # The trap the module docstring names: a BOM folded into the first key name leaves the
    # hub reading its own first setting as unset.
    check("a BOM does not corrupt the first key",
          envfile.read_all(path).get("GOOGLE_CLIENT_ID") == "abc")


def test_set_vars_preserves_everything_else():
    path = _fresh("# keep me\nA=1\nB=2\n")
    changed = envfile.set_vars(path, {"B": "9", "C": "3"})
    text = open(path, encoding="utf-8").read()
    check("comments survive a rewrite", "# keep me" in text)
    check("untouched keys survive", envfile.read_all(path)["A"] == "1")
    check("existing key is updated in place", envfile.read_all(path)["B"] == "9")
    check("new key is appended", envfile.read_all(path)["C"] == "3")
    check("returns only what actually changed", changed == {"B", "C"})
    check("no BOM is written", not open(path, "rb").read().startswith(b"\xef\xbb\xbf"))


def test_set_vars_none_deletes():
    path = _fresh("A=1\nB=2\n")
    envfile.set_vars(path, {"A": None})
    values = envfile.read_all(path)
    check("None removes the key entirely", "A" not in values)
    check("...and leaves the rest", values.get("B") == "2")


def test_set_vars_no_op_reports_nothing():
    path = _fresh("A=1\n")
    check("rewriting a key with its own value reports no change",
          envfile.set_vars(path, {"A": "1"}) == set())


# ---------------------------------------------------------------------------------------
# The ACL
# ---------------------------------------------------------------------------------------
def test_protect_removes_inherited_access():
    if sys.platform != "win32":
        check("protect is a Windows-only concern (skipped)", True)
        return
    path = _fresh()
    note = envfile.protect(path)
    acl = _icacls(path)
    check("protect reports what it did", bool(note) and "Restricted" in note)
    check("SYSTEM keeps full control", "NT AUTHORITY\\SYSTEM:(F)" in acl)
    check("nothing is inherited any more", "(I)" not in acl)
    # The property that matters, stated as itself rather than as an ACE count: the
    # everyone-on-this-box grant is what the exposure was.
    check("BUILTIN\\Users no longer appears at all",
          "S-1-5-32-545" not in _icacls_sids(path))
    check("at most three principals remain (SYSTEM, Administrators, the running account)",
          len([l for l in acl.splitlines() if ":(" in l]) <= 3)


def test_protect_keeps_the_account_the_hub_runs_as():
    """In production that account is SYSTEM and this is a no-op. In a dev checkout it is the
    developer -- and locking them out is unrecoverable without an elevated shell, because an
    unprivileged owner cannot even read the ACL back to undo it."""
    if sys.platform != "win32":
        check("running-account grant is Windows-only (skipped)", True)
        return
    path = _fresh("BACKUP_MASTER_KEY=irreplaceable\n")
    envfile.protect(path)
    try:
        check("the process that protected it can still read it",
              "irreplaceable" in open(path, encoding="utf-8").read())
        check("...and still rewrite it",
              envfile.set_vars(path, {"A": "1"}) == {"A"})
    except PermissionError:
        check("the process that protected it can still read it", False)


def test_protect_is_idempotent():
    if sys.platform != "win32":
        check("protect idempotency is Windows-only (skipped)", True)
        return
    path = _fresh()
    check("the first call reports a change", envfile.protect(path) is not None)
    # Runs on every hub boot, so a settled ACL must be silent -- otherwise the startup log
    # claims an exposure was just fixed on every single restart.
    check("the second call is a silent no-op", envfile.protect(path) is None)
    check("and the third", envfile.protect(path) is None)


def test_protect_never_raises():
    check("a missing file is not an error", envfile.protect(os.path.join(_TMPDIR, "gone")) is None)
    check("an empty path is not an error", envfile.protect("") is None)
    check("None is not an error", envfile.protect(None) is None)
    # Fails soft by contract: a hub that cannot re-ACL its config must still boot.
    note = envfile.protect(os.path.join(_TMPDIR, "no", "such", "dir", ".env"))
    check("an unreachable path returns rather than raising", note is None or isinstance(note, str))


if __name__ == "__main__":
    test_read_all()
    test_read_all_tolerates_a_bom()
    test_set_vars_preserves_everything_else()
    test_set_vars_none_deletes()
    test_set_vars_no_op_reports_nothing()
    test_protect_removes_inherited_access()
    test_protect_keeps_the_account_the_hub_runs_as()
    test_protect_is_idempotent()
    test_protect_never_raises()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
