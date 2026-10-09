"""Session recordings, the model (recordings.py, roadmap #19).

The silent failures this file exists to catch:

  * **Video stored with no badge on the PC.** A chunk accepted while the recording is still
    `starting` -- before the helper confirmed its "Recording Screen" badge -- is exactly the
    silent recording the owner ruled out, and nothing on screen would show it happened.
  * **A badge report from the wrong PC.** Only the recording's own machine may confirm it; an
    agent that could confirm another PC's badge could start that PC's recording unannounced.
  * **A corrupted file that still looks fine.** A webm file is one stream: a chunk stored out
    of order, or a retried chunk stored twice, leaves a file whose size looks right and whose
    video stops playing partway.
  * **A recording that outlives its limit or its session.** The deadline, the session's end
    and an unconfirmed badge each end a recording; if reconcile misses one, the hub keeps
    accepting video nobody is watching.
  * **A group share that does not follow the group.** The owner's decision: whoever is in the
    group NOW may watch. A snapshot would keep showing it to someone who has left.
  * **Re-sharing, or a share that points nowhere.** Unknown groups are refused, the owner is
    never stored as a share, and nothing but the owner's own call changes the list.
  * **An owner folder that climbs out of the recordings root.** It is built from an email.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import permissions
import recordings

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


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    except Exception:
        return False
    return False


def setup():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    fleet.init_fleet_db(db)
    permissions.init_permissions_db(db)
    recordings.init_recordings_db(db)
    root = tempfile.mkdtemp(prefix="recordings-test-")
    return db, root


def new(db, session="s1", machine="PC-01", owner="ann@x.com", now=1000):
    return recordings.create(db, session_id=session, machine=machine, owner=owner,
                             reason="Ticket 4711: user reports a frozen app",
                             mime="video/webm;codecs=vp9", now=now)


def test_create():
    print("\n-- starting a recording --")
    db, _root = setup()
    check("a reason is required",
          raises(ValueError, recordings.create, db, session_id="s", machine="PC",
                 owner="a@x.com", reason="   ", mime="video/webm"))
    check("a reason has a length limit",
          raises(ValueError, recordings.create, db, session_id="s", machine="PC",
                 owner="a@x.com", reason="x" * 501, mime="video/webm"))
    check("only webm video is accepted",
          raises(ValueError, recordings.create, db, session_id="s", machine="PC",
                 owner="a@x.com", reason="r", mime="text/html"))
    rec = new(db)
    check("a new recording starts in `starting`", rec["status"] == recordings.STATUS_STARTING)
    check("...owned by the normalised email", rec["owner"] == "ann@x.com")
    check("one session cannot be recorded twice at once",
          raises(ValueError, new, db))
    # Two Start requests racing both get past the read; the database must refuse the second.
    real = recordings.live_for_session
    recordings.live_for_session = lambda *_a, **_k: []
    try:
        raced = raises(ValueError, new, db)
    finally:
        recordings.live_for_session = real
    check("...even when a racing request got past the check",
          raced and len(recordings.live_for_session(db, "s1")) == 1)
    audit = fleet.list_audit(db, action="recording_start")["entries"]
    check("the start is audited with who, which PC and why",
          audit and audit[0]["actor"] == "ann@x.com" and audit[0]["target"] == "PC-01"
          and audit[0]["detail"]["reason"].startswith("Ticket 4711"))


def test_badge_gate():
    print("\n-- nothing is stored before the PC shows the badge --")
    db, root = setup()
    rec = new(db)
    check("a chunk before the badge is refused",
          raises(recordings.RecordingClosed, recordings.append, db, root, rec["id"], 0, b"x"))
    check("...and nothing reached the disk",
          not os.path.exists(recordings.file_path(root, rec)))
    after = recordings.confirm_badge(db, rec["id"], "PC-99", "shown", now=1001)
    check("another PC's badge report is ignored", after is None
          and recordings.get(db, rec["id"])["status"] == recordings.STATUS_STARTING)
    after = recordings.confirm_badge(db, rec["id"], "PC-01", "shown", now=1002)
    check("the recording's own PC confirms it", after["status"] == recordings.STATUS_RECORDING)
    check("...which starts the ten-minute clock",
          after["deadline"] == 1002 + recordings.SEGMENT_SECONDS)

    rec2 = new(db, session="s2")
    recordings.confirm_badge(db, rec2["id"], "PC-01", "failed", now=1003)
    check("a badge that could not be shown fails an unconfirmed recording",
          recordings.get(db, rec2["id"])["status"] == recordings.STATUS_FAILED)
    recordings.confirm_badge(db, rec["id"], "PC-01", "failed", now=1004)
    got = recordings.get(db, rec["id"])
    check("...and ends a running one (the lock screen it could not follow)",
          got["status"] == recordings.STATUS_ENDED
          and got["end_reason"] == recordings.END_BADGE_FAILED)


def test_chunks():
    print("\n-- chunks are one stream, in order, once each --")
    db, root = setup()
    rec = new(db)
    recordings.confirm_badge(db, rec["id"], "PC-01", "shown", now=1000)
    recordings.append(db, root, rec["id"], 0, b"AAAA", now=1001)
    check("a gap is refused",
          raises(ValueError, recordings.append, db, root, rec["id"], 2, b"CC", now=1002))
    recordings.append(db, root, rec["id"], 1, b"BB", now=1002)
    recordings.append(db, root, rec["id"], 1, b"BB", now=1003)      # a retry that had landed
    path = recordings.file_path(root, rec)
    with open(path, "rb") as f:
        data = f.read()
    check("a retried chunk is not written twice", data == b"AAAABB")
    got = recordings.get(db, rec["id"])
    check("size and next chunk are tracked", got["size_bytes"] == 6 and got["next_seq"] == 2)
    check("an oversized chunk is refused",
          raises(ValueError, recordings.append, db, root, rec["id"], 2,
                 b"x" * (recordings.MAX_CHUNK_BYTES + 1), now=1004))


def test_deadline_and_extend():
    print("\n-- ten minutes at a time --")
    db, root = setup()
    rec = new(db)
    recordings.confirm_badge(db, rec["id"], "PC-01", "shown", now=1000)
    deadline = 1000 + recordings.SEGMENT_SECONDS
    check("an extension long before the end is refused",
          raises(ValueError, recordings.extend, db, rec["id"], "ann@x.com", now=1100))
    got = recordings.extend(db, rec["id"], "ann@x.com", now=deadline - 30)
    check("an extension in the last minute adds ten minutes",
          got["deadline"] == deadline + recordings.SEGMENT_SECONDS and got["extensions"] == 1)
    check("...and is audited",
          len(fleet.list_audit(db, action="recording_extend")["entries"]) == 1)
    late = got["deadline"] + recordings.DEADLINE_GRACE_SECONDS + 1
    check("a chunk after the deadline is refused",
          raises(recordings.RecordingClosed, recordings.append, db, root, rec["id"], 0, b"x",
                 now=late))
    got = recordings.get(db, rec["id"])
    check("...and ends the recording at its time limit",
          got["status"] == recordings.STATUS_ENDED
          and got["end_reason"] == recordings.END_TIME_LIMIT)


def test_reconcile():
    print("\n-- recordings that stopped being live are ended --")
    db, _root = setup()
    unconfirmed = new(db, session="a", now=1000)
    overdue = new(db, session="b", now=1000)
    recordings.confirm_badge(db, overdue["id"], "PC-01", "shown", now=1000)
    orphan = new(db, session="dead", now=1000)
    recordings.confirm_badge(db, orphan["id"], "PC-01", "shown", now=1000)
    healthy = new(db, session="c", now=1000)
    recordings.confirm_badge(db, healthy["id"], "PC-01", "shown", now=1000)

    now = 1000 + recordings.SEGMENT_SECONDS + recordings.DEADLINE_GRACE_SECONDS + 1
    # Keep `healthy` inside its time by extending it first.
    recordings.extend(db, healthy["id"], "ann@x.com", now=1000 + recordings.SEGMENT_SECONDS - 10)
    ended = recordings.reconcile(db, lambda sid: sid != "dead", now=now)
    reasons = {r["id"]: r["end_reason"] for r in ended}
    check("an unconfirmed badge times out",
          reasons.get(unconfirmed["id"]) == recordings.END_BADGE_TIMEOUT
          and recordings.get(db, unconfirmed["id"])["status"] == recordings.STATUS_FAILED)
    check("a passed deadline ends it", reasons.get(overdue["id"]) == recordings.END_TIME_LIMIT)
    check("a session that ended takes its recording with it",
          reasons.get(orphan["id"]) == recordings.END_SESSION_ENDED)
    check("a healthy recording is left alone", healthy["id"] not in reasons
          and recordings.get(db, healthy["id"])["status"] == recordings.STATUS_RECORDING)
    check("reconcile is idempotent", recordings.reconcile(db, lambda sid: True, now=now) == [])
    check("every ending is audited once",
          len(fleet.list_audit(db, action="recording_end")["entries"]) == 3)

    rec = new(db, session="e", now=now)
    check("end_for_session ends a session's live recordings",
          recordings.end_for_session(db, "e", recordings.END_HELPER_RESTARTED) == [rec["id"]])
    check("...once", recordings.end_for_session(db, "e", recordings.END_HELPER_RESTARTED) == [])


def test_sharing():
    print("\n-- who may watch, and who decides --")
    db, root = setup()
    gid = permissions.create_group(db, "Helpdesk leads", capabilities=[permissions.VIEW],
                                   members=["lead@x.com"])
    rec = new(db)
    check("the owner may watch", recordings.can_view(db, rec, "ANN@x.com", []))
    check("nobody else may by default", not recordings.can_view(db, rec, "bob@x.com", []))
    check("an unknown group is refused rather than stored",
          raises(ValueError, recordings.set_shares, db, rec, groups=["nope"], users=[],
                 actor="ann@x.com"))
    check("a malformed address is refused",
          raises(ValueError, recordings.set_shares, db, rec, groups=[], users=["not an email"],
                 actor="ann@x.com"))
    added, _ = recordings.set_shares(db, rec, groups=[gid], users=["Bob@X.com", "ann@x.com"],
                                     actor="ann@x.com")
    shares = recordings.shares(db, rec["id"])
    check("users are normalised and the owner is not stored as a share",
          shares["users"] == ["bob@x.com"] and shares["groups"] == [gid])
    check("a named person may watch", recordings.can_view(db, rec, "bob@x.com", []))
    lead_groups = [g["id"] for g in permissions.groups_for_email(db, "lead@x.com")]
    check("a current group member may watch", recordings.can_view(db, rec, "lead@x.com",
                                                                   lead_groups))
    permissions.update_group(db, gid, members=[])
    lead_groups = [g["id"] for g in permissions.groups_for_email(db, "lead@x.com")]
    check("...and stops being able to once removed from the group",
          not recordings.can_view(db, rec, "lead@x.com", lead_groups))
    check("shared recordings are listed for the person they are shared with",
          [r["id"] for r in recordings.list_shared_with(db, "bob@x.com", [])] == [rec["id"]])
    check("...but not as shared with the owner",
          recordings.list_shared_with(db, "ann@x.com", []) == [])
    audit = fleet.list_audit(db, action="recording_share")["entries"]
    check("a share change is audited with what was added",
          audit and "user:bob@x.com" in audit[0]["detail"]["added"])
    recordings.set_shares(db, rec, groups=[gid], users=["bob@x.com"], actor="ann@x.com")
    check("saving the same shares writes no second audit row",
          len(fleet.list_audit(db, action="recording_share")["entries"]) == 1)


def test_storage():
    print("\n-- where the files live, and deleting them --")
    db, root = setup()
    climbing = recordings.owner_folder("../../etc@x.com")
    check("an owner folder cannot climb out of the root",
          os.sep not in climbing and "/" not in climbing and not climbing.startswith("."))
    check("...and two different emails never share one",
          recordings.owner_folder("a+b@x.com") != recordings.owner_folder("a_b@x.com")
          and recordings.owner_folder("o'neil@x.com") != recordings.owner_folder("o_neil@x.com"))
    check("...while an ordinary address stays readable",
          recordings.owner_folder("Ann.Lee@X.com") == "ann.lee@x.com")
    check("...and is never empty", recordings.owner_folder("") == "_unknown")
    rec = new(db)
    path = recordings.file_path(root, rec)
    check("the file is under <root>/<owner>/recordings",
          path == os.path.join(os.path.realpath(root), "ann@x.com", "recordings",
                               rec["id"] + ".webm"))
    # The path is handed to send_file and os.remove (CodeQL py/path-injection on #116).
    check("an id create() could not have minted is refused",
          raises(ValueError, recordings.file_path, root, dict(rec, id="../../evil")))
    check("...and so is one that is merely the wrong shape",
          raises(ValueError, recordings.file_path, root, dict(rec, id="A" * 32)))
    check("is_recording_id accepts exactly a uuid4 hex",
          recordings.is_recording_id(rec["id"]) and not recordings.is_recording_id("x"))
    recordings.confirm_badge(db, rec["id"], "PC-01", "shown", now=1000)
    recordings.append(db, root, rec["id"], 0, b"VIDEO", now=1001)
    recordings.finish(db, rec["id"], recordings.END_STOPPED, now=1100)
    check("usage counts the owner's bytes",
          recordings.usage(db, "ann@x.com") == {"count": 1, "bytes": 5})
    check("the duration runs from the badge to the end",
          recordings.get(db, rec["id"])["duration_seconds"] == 100)
    if sys.platform == "win32":
        # A colleague's player holding the file open. Windows refuses the delete; the record
        # must survive it, or the video would be orphaned on disk where nobody can reach it.
        with open(path, "rb"):
            refused = raises(ValueError, recordings.delete, db, root,
                             recordings.get(db, rec["id"]), actor="ann@x.com")
        check("a delete while somebody is reading the file is refused",
              refused and recordings.get(db, rec["id"]) is not None and os.path.exists(path))
    recordings.delete(db, root, recordings.get(db, rec["id"]), actor="ann@x.com")
    check("delete removes the file", not os.path.exists(path))
    check("...and the record", recordings.get(db, rec["id"]) is None)
    check("...and is audited",
          len(fleet.list_audit(db, action="recording_delete")["entries"]) == 1)


def main():
    test_create()
    test_badge_gate()
    test_chunks()
    test_deadline_and_extend()
    test_reconcile()
    test_sharing()
    test_storage()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
