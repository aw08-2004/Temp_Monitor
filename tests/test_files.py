"""Unit tests for files.py -- the file explorer's rules and its two kinds of row.

**The path rules are the point of this file.** Everything else here is bookkeeping that
would fail loudly the first time somebody used it; a path rule that is quietly wrong
produces a command that runs as SYSTEM against a file nobody named. So the assertions that
matter are the refusals: a relative path, a `..` component, a bare drive being deleted, a
folder moved inside itself.

Note what is deliberately NOT asserted: there is no test that some directory is off-limits,
because no directory is. An operator with this capability already has a SYSTEM shell on the
machine through the Terminal tab, so a blocklist here would only be a map. See files.py.

Run from the repo root so `import files` resolves.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import files

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


def refuses(fn, *args, **kwargs):
    """True if `fn` raised ValueError. The refusals are most of this file, and asserting
    them by hand is six lines each."""
    try:
        fn(*args, **kwargs)
    except ValueError:
        return True
    return False


# ============================== paths ==============================

def test_paths_are_absolute_and_literal():
    print("\n== A path is absolute, or it is not a path ==")
    check("a drive root normalizes with its separator",
          files.validate_path("C:") == "C:\\" and files.validate_path("C:\\") == "C:\\")
    check("forward slashes are accepted and spoken back in Windows' dialect",
          files.validate_path("c:/Users/bob/") == "C:\\Users\\bob")
    check("a UNC share is a path", files.validate_path("//srv/share/x") == "\\\\srv\\share\\x")
    check("...but a bare server is not", refuses(files.validate_path, "\\\\srv"))
    check("a relative path is refused", refuses(files.validate_path, "Users\\bob"))
    check("...including one that leans on the current drive",
          refuses(files.validate_path, "C:Users"))
    check("an empty path is refused", refuses(files.validate_path, "  "))

    # The '..' rule is not about escaping a root -- there is no root to escape. It is about
    # the audit row and the breadcrumb naming the same folder the command acts on.
    check("'..' is refused rather than resolved",
          refuses(files.validate_path, "C:\\Users\\bob\\..\\alice"))
    check("a NUL is refused -- it truncates the string in every API downstream",
          refuses(files.validate_path, "C:\\Users\\bob\x00.txt"))
    check("an absurdly long path is refused",
          refuses(files.validate_path, "C:\\" + "a" * files.MAX_PATH_CHARS))


def test_parents():
    print("\n== Walking up ==")
    check("a folder's parent is its folder",
          files.parent_path("C:\\Users\\bob") == "C:\\Users")
    check("...and one level up is the drive root",
          files.parent_path("C:\\Users") == "C:\\")
    check("a drive root has no parent", files.parent_path("C:\\") is None)
    check("nor does a share root", files.parent_path("\\\\srv\\share") is None)
    check("join builds a path the same way validate reads one",
          files.join_path("C:\\", "x.txt") == "C:\\x.txt"
          and files.join_path("C:\\a", "x.txt") == "C:\\a\\x.txt")


def test_names():
    print("\n== A name is one component ==")
    check("an ordinary name passes", files.validate_name("report v2.docx") == "report v2.docx")
    check("a separator is refused", refuses(files.validate_name, "a\\b"))
    check("a reserved device name is refused -- it creates a file nobody can open",
          refuses(files.validate_name, "con.txt") and refuses(files.validate_name, "LPT1"))
    check("a trailing dot is refused -- Windows drops it and renames the file silently",
          refuses(files.validate_name, "report."))
    check("'..' is not a name", refuses(files.validate_name, ".."))


# ============================== operations ==============================

def test_operations_are_validated_as_their_own_shapes():
    print("\n== Each verb is checked as the shape it actually is ==")
    check("copy takes sources and a destination",
          files.validate_operation("copy", paths=["C:\\a\\x"], destination="C:\\b")
          == {"op": "copy", "paths": ["C:\\a\\x"], "destination": "C:\\b"})
    check("rename takes one source and a name",
          files.validate_operation("rename", paths=["C:\\a\\x"], new_name="y")
          == {"op": "rename", "paths": ["C:\\a\\x"], "new_name": "y"})
    check("new_folder takes a destination and a name, and no sources",
          files.validate_operation("new_folder", destination="C:\\a", new_name="sub")
          == {"op": "new_folder", "destination": "C:\\a", "new_name": "sub"})

    check("an unknown verb is refused", refuses(files.validate_operation, "chmod"))
    check("delete with a destination is refused -- somebody built that by hand",
          refuses(files.validate_operation, "delete", paths=["C:\\a"], destination="C:\\b"))
    check("rename of twelve things is refused rather than given a meaning",
          refuses(files.validate_operation, "rename", paths=["C:\\a", "C:\\b"], new_name="y"))
    check("an empty selection is refused", refuses(files.validate_operation, "delete", paths=[]))
    check("more items than the cap is refused",
          refuses(files.validate_operation, "delete",
                  paths=[f"C:\\a\\{n}" for n in range(files.MAX_OPERATION_PATHS + 1)]))
    check("duplicates in a selection collapse",
          files.validate_operation("delete", paths=["C:\\a", "C:\\a"])["paths"] == ["C:\\a"])


def test_the_two_operations_that_destroy_data():
    print("\n== The two shapes that lose files rather than failing ==")
    # Deleting, copying or moving a whole volume is not a file operation, and every way it
    # could be meant has a better tool.
    check("a drive cannot be deleted", refuses(files.validate_operation, "delete", paths=["C:\\"]))
    check("nor can a share root",
          refuses(files.validate_operation, "delete", paths=["\\\\srv\\share"]))
    # The one case that destroys data instead of failing: the copy walks into the copy it is
    # making until the disk is full.
    check("a folder cannot be moved inside itself",
          refuses(files.validate_operation, "move", paths=["C:\\a"], destination="C:\\a\\b"))
    check("...and not into itself either",
          refuses(files.validate_operation, "copy", paths=["C:\\a"], destination="C:\\a"))
    check("a sibling destination is fine",
          files.validate_operation("move", paths=["C:\\a"], destination="C:\\ab")["destination"]
          == "C:\\ab")


# ============================== listings ==============================

def test_a_listing_is_asked_once_and_answered_once(db_path):
    print("\n== A listing is a question, asked once ==")
    request_id, clean = files.create_listing(db_path, "PC-1", "c:/Users/bob")
    # The DRIVE is uppercased and the separators become Windows'; the components are left
    # exactly as they were typed. A filename's case is the filename's business, and
    # "helpfully" normalizing it would show an operator a name that is not the one on disk.
    check("the path is normalized when the question is asked", clean == "C:\\Users\\bob")
    pending = files.get_listing(db_path, "PC-1" and request_id, machine="PC-1")
    check("...and starts pending, not empty", pending["status"] == files.PENDING)

    stored = files.record_listing(db_path, request_id, "PC-1", {
        "path": "C:\\Users\\bob",
        "entries": [
            {"name": "Documents", "directory": True, "hidden": False},
            {"name": "notes.txt", "directory": False, "size": 42, "modified": 1700000000},
            {"name": "", "directory": False},          # unusable -- dropped
            "not an object",                            # ditto
        ],
    })
    check("the machine's answer is stored", stored is True)

    payload = files.get_listing(db_path, request_id, machine="PC-1")
    check("...and reads back ready", payload["status"] == files.READY)
    check("entries that cannot be clicked are dropped rather than rendered",
          [e["name"] for e in payload["entries"]] == ["Documents", "notes.txt"])
    check("a folder carries no size -- a directory entry's size is not what is in it",
          payload["entries"][1]["size"] == 42)
    check("the parent is derived, so an older agent still gets an 'up' button",
          payload["parent"] == "C:\\Users")

    check("answering twice is refused -- a retry must not replace what is being read",
          files.record_listing(db_path, request_id, "PC-1", {"entries": []}) is False)


def test_a_listing_belongs_to_one_machine(db_path):
    print("\n== An id is not an authorisation ==")
    request_id, _ = files.create_listing(db_path, "PC-1", "C:\\")
    check("another machine's agent cannot answer it",
          files.record_listing(db_path, request_id, "PC-2", {"entries": [
              {"name": "x", "directory": False}]}) is False)
    check("...nor fail it", files.fail_listing(db_path, request_id, "PC-2", "denied") is False)
    check("...and a console scoped to another machine cannot read it",
          files.get_listing(db_path, request_id, machine="PC-2") is None)

    check("the machine it belongs to CAN refuse it",
          files.fail_listing(db_path, request_id, "PC-1", "Access is denied") is True)
    payload = files.get_listing(db_path, request_id, machine="PC-1")
    # A refusal is a RESULT: "Access is denied" is the answer to "what is in this folder",
    # and rendering it as a broken request sends an operator hunting the wrong fault.
    check("...and the refusal is readable as the answer",
          payload["status"] == files.FAILED and payload["error"] == "Access is denied")


def test_a_listing_is_capped_and_says_so(db_path):
    print("\n== A listing that is not everything says so ==")
    request_id, _ = files.create_listing(db_path, "PC-1", "C:\\Windows\\System32")
    over = files.MAX_ENTRIES + 25
    files.record_listing(db_path, request_id, "PC-1", {
        "entries": [{"name": f"f{n}", "directory": False} for n in range(over)],
        "truncated": 100,
    })
    payload = files.get_listing(db_path, request_id, machine="PC-1")
    check("the cap holds", len(payload["entries"]) == files.MAX_ENTRIES)
    # What the machine dropped plus what we dropped, as ONE number: "you are not seeing
    # everything" is one fact to an operator.
    check("...and what was dropped at both ends is one number",
          payload["truncated"] == 100 + 25)


def test_a_path_from_a_machine_is_re_checked(db_path):
    print("\n== A path arriving FROM a machine is remote text ==")
    request_id, _ = files.create_listing(db_path, "PC-1", "C:\\Users")
    files.record_listing(db_path, request_id, "PC-1", {
        "path": "..\\..\\somewhere",                     # nonsense from the far end
        "entries": [{"name": "x", "directory": True}],
        "drives": [{"path": "not a path"}, {"path": "D:\\", "label": "Data",
                                            "total_bytes": 100, "free_bytes": 40}],
    })
    payload = files.get_listing(db_path, request_id, machine="PC-1")
    check("a path we cannot make sense of falls back to the one the operator clicked",
          payload["path"] == "C:\\Users")
    check("a drive we cannot make sense of is dropped, not rendered",
          [d["path"] for d in payload["drives"]] == ["D:\\"])


def test_listings_are_pruned(db_path):
    print("\n== A table that gains a row per click does not keep them forever ==")
    request_id, _ = files.create_listing(db_path, "PC-1", "C:\\", now=1000)
    check("a fresh row survives a prune",
          files.prune_listings(db_path, now=1000 + files.LISTING_TTL_SECONDS - 1) == 0)
    check("...and a stale one does not",
          files.prune_listings(db_path, now=1000 + files.LISTING_TTL_SECONDS + 1) == 1)
    check("...and is then simply unknown",
          files.get_listing(db_path, request_id, machine="PC-1") is None)


# ============================== transfers ==============================

def test_a_download_and_an_upload_are_one_table(db_path, spool_dir):
    print("\n== Bytes in flight, both directions ==")
    pull = files.create_transfer(db_path, "PC-1", files.PULL, "C:\\logs\\app.log",
                                 "app.log", issued_by="tech@x.com")
    row = files.get_transfer(db_path, pull)
    check("a download starts pending -- the bytes do not exist yet",
          row["status"] == files.PENDING and row["spool"] is None)

    spool = files.new_spool_name(pull)
    with open(files.spool_path(spool_dir, spool), "wb") as handle:
        handle.write(b"hello")
    check("...and goes ready when they land",
          files.mark_transfer_ready(db_path, pull, spool, 5) is True)
    check("a second landing is refused -- a retry must not replace what is being read",
          files.mark_transfer_ready(db_path, pull, spool, 5) is False)
    check("the size is what arrived", files.get_transfer(db_path, pull)["size_bytes"] == 5)

    push = files.create_transfer(db_path, "PC-1", files.PUSH, "C:\\", "driver.msi",
                                 issued_by="tech@x.com", status=files.PENDING)
    # The two-step upload: the multipart POST that spooled these bytes was inert, and THIS
    # is the JSON step that gives them a destination. See files_web.
    check("an upload is armed with the destination it was aimed at",
          files.arm_push(db_path, push, "C:\\Temp\\drivers", "driver.msi") is True)
    row = files.get_transfer(db_path, push)
    check("...and carries it", row["path"] == "C:\\Temp\\drivers" and row["status"] == files.READY)
    check("arming twice is refused -- bytes an agent is fetching cannot be re-aimed",
          files.arm_push(db_path, push, "C:\\Other", "driver.msi") is False)

    check("a transfer belongs to one machine, like a listing",
          files.get_transfer(db_path, pull, machine="PC-2") is None)


def test_expiry_takes_the_bytes_with_it(db_path, spool_dir):
    print("\n== Nothing else on the hub deletes a spool file ==")
    transfer = files.create_transfer(db_path, "PC-1", files.PULL, "C:\\x.bin", "x.bin",
                                     now=1000)
    spool = files.new_spool_name(transfer)
    path = files.spool_path(spool_dir, spool)
    with open(path, "wb") as handle:
        handle.write(b"0" * 16)
    files.mark_transfer_ready(db_path, transfer, spool, 16)

    check("a live transfer survives", files.prune_transfers(db_path, spool_dir, now=1001) == 0)
    removed = files.prune_transfers(db_path, spool_dir,
                                    now=1000 + files.TRANSFER_TTL_SECONDS + 1)
    check("an expired one is dropped", removed == 1)
    # A row without its file renders as an expired download; a file without its row is disk
    # nothing will ever reclaim. So the file goes first and the row second.
    check("...and its bytes go with it", not os.path.exists(path))


def test_a_spool_name_is_never_a_path(db_path, spool_dir):
    print("\n== The spool name is written by us, and checked anyway ==")
    check("a bare name joins", files.spool_path(spool_dir, "abc.bin").startswith(spool_dir))
    check("a traversal is refused", refuses(files.spool_path, spool_dir, "..\\..\\.env"))
    check("a separator is refused", refuses(files.spool_path, spool_dir, "sub/abc.bin"))
    check("an empty name is refused", refuses(files.spool_path, spool_dir, ""))


def test_deleting_a_machine_leaves_nothing(db_path, spool_dir):
    print("\n== A decommissioned PC leaves no listings and no bytes ==")
    request_id, _ = files.create_listing(db_path, "PC-9", "C:\\Users\\bob")
    transfer = files.create_transfer(db_path, "PC-9", files.PULL, "C:\\x", "x")
    spool = files.new_spool_name(transfer)
    path = files.spool_path(spool_dir, spool)
    with open(path, "wb") as handle:
        handle.write(b"x")
    files.mark_transfer_ready(db_path, transfer, spool, 1)

    files.forget_machine(db_path, "PC-9", spool_dir=spool_dir)
    check("its listings are gone -- a row naming what was in a user's folders is residue",
          files.get_listing(db_path, request_id, machine="PC-9") is None)
    check("its transfers are gone", files.get_transfer(db_path, transfer) is None)
    check("...and so are the bytes", not os.path.exists(path))


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    spool_dir = tempfile.mkdtemp(prefix="filespool")
    try:
        files.init_files_db(db_path)
        test_paths_are_absolute_and_literal()
        test_parents()
        test_names()
        test_operations_are_validated_as_their_own_shapes()
        test_the_two_operations_that_destroy_data()
        test_a_listing_is_asked_once_and_answered_once(db_path)
        test_a_listing_belongs_to_one_machine(db_path)
        test_a_listing_is_capped_and_says_so(db_path)
        test_a_path_from_a_machine_is_re_checked(db_path)
        test_listings_are_pruned(db_path)
        test_a_download_and_an_upload_are_one_table(db_path, spool_dir)
        test_expiry_takes_the_bytes_with_it(db_path, spool_dir)
        test_a_spool_name_is_never_a_path(db_path, spool_dir)
        test_deleting_a_machine_leaves_nothing(db_path, spool_dir)
        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
        for name in os.listdir(spool_dir):
            try:
                os.remove(os.path.join(spool_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(spool_dir)
        except OSError:
            pass


if __name__ == "__main__":
    main()
