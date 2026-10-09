"""Remote session recordings (roadmap #19): who recorded which PC, why, for how long, where the
video is, and who else may watch it.

**The video is recorded by the operator's browser, not by the agent.** The browser already
holds the decoded picture -- a WebRTC MediaStream, or the hub relay's canvas -- so it runs a
MediaRecorder on it and sends the hub webm chunks. *Rejected:* recording on the agent. It would
mean a second encoder on a PC that is already encoding the stream, and a second upload path
for something the console already has in hand.

**The badge on the PC is what makes a recording a recording.** The owner's decision is "never
silent": while a recording runs, the PC shows a "Recording Screen" badge, on the lock screen
too. So a recording starts in `starting`, the hub asks the agent's helper to raise the badge
(a `record` signal on the session's existing signaling channel), and only the helper's
acknowledgement moves it to `recording`. **No chunk is stored before that acknowledgement.**
An agent too old to know about the badge never answers, and its recording fails after
BADGE_WAIT_SECONDS rather than running unannounced. A badge that cannot be shown on a desktop
the session moves to (the lock screen) is reported, and the recording ends.

**It ends when the helper that showed the badge is gone.** A new helper (the supervisor
relaunched it after a sign-in, or it died) knows nothing of the badge, so its first offer ends
every live recording of the session -- see end_for_session's callers in remote_web.py.

**Ten minutes at a time.** A recording runs to a deadline; the browser shows a countdown (in
the console only, never in the video) and may extend it by another SEGMENT_SECONDS without a
new reason. *Rejected:* no limit -- a recording left running by accident fills the hub's disk
and is a privacy problem of its own.

**Stored on disk, per owner, kept until deleted.** `<root>/<owner>/recordings/<id>.webm`,
where `root` is the hub install's `data` directory (app.py), locked to SYSTEM and
Administrators like `.env` is. The database holds only the metadata. *Rejected:* a BLOB column
-- every backup of the database would copy every recording.

**Sharing.** The owner shares with permission groups, named people, or both. A group share
FOLLOWS the group: whoever is a member when they open the recording may watch it, and whoever
has left may not. Only the owner shares; *rejected:* re-sharing by someone a recording was
shared with -- the owner decides who sees a recording of a colleague's screen, and a chain of
re-shares would take that decision away from them.

Flask-free; recordings_web.py is the HTTP surface.
"""
import os
import re
import sqlite3
import threading
import time
import uuid

import fleet
import permissions

STATUS_STARTING = "starting"    # created; the hub has asked the PC to show the badge
STATUS_RECORDING = "recording"  # the PC confirmed the badge; chunks are accepted
STATUS_ENDED = "ended"          # stopped after recording something
STATUS_FAILED = "failed"        # never confirmed, so nothing was recorded
LIVE_STATUSES = (STATUS_STARTING, STATUS_RECORDING)

# End reasons, as codes the console translates (recordings.end_reason.<code>).
END_STOPPED = "stopped"
END_TIME_LIMIT = "time_limit"
END_SESSION_ENDED = "session_ended"
END_HELPER_RESTARTED = "helper_restarted"
END_BADGE_FAILED = "badge_failed"
END_BADGE_TIMEOUT = "badge_timeout"
END_STREAM_CHANGED = "stream_changed"
END_SIZE_LIMIT = "size_limit"
END_REASONS = (END_STOPPED, END_TIME_LIMIT, END_SESSION_ENDED, END_HELPER_RESTARTED,
               END_BADGE_FAILED, END_BADGE_TIMEOUT, END_STREAM_CHANGED, END_SIZE_LIMIT)

SEGMENT_SECONDS = 10 * 60
# Extending is offered in the last minute. A little wider here than in the browser so a click
# on the last second of the countdown is not refused for a clock that is a second off.
EXTEND_WINDOW_SECONDS = 90
# The browser's last chunk is cut when the recorder stops, which is after the deadline by
# however long one timeslice and one upload take.
DEADLINE_GRACE_SECONDS = 20
# How long the PC gets to raise the badge. The helper polls signaling every 800 ms; most of
# this is for a helper that is mid-desktop-switch.
BADGE_WAIT_SECONDS = 30

MAX_REASON_CHARS = 500
MAX_CHUNK_BYTES = 16 * 1024 * 1024
# About eight hours at the browser's recording bitrate. A cap, not a target: past it the
# recording ends with END_SIZE_LIMIT instead of filling the disk.
MAX_RECORDING_BYTES = 8 * 1024 * 1024 * 1024
MAX_SHARE_USERS = 100

SHARE_GROUP = "group"
SHARE_USER = "user"

FILE_EXTENSION = ".webm"
ALLOWED_MIME = re.compile(r"^video/webm(;\s*codecs=[a-z0-9.,]+)?$", re.IGNORECASE)

# One lock per recording for appends. The browser uploads one chunk at a time, so this only
# matters for a retried chunk racing its original -- which must not be written twice.
_append_locks = {}
_append_locks_guard = threading.Lock()


class RecordingClosed(Exception):
    """A chunk or an extension for a recording that is no longer recording. Carries the
    recording's status and end reason so the browser can say why."""

    def __init__(self, recording):
        super().__init__(f"recording is {recording['status']}")
        self.recording = recording


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_recordings_db(db_path):
    """Create the recording tables if absent. Idempotent."""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recordings (
                id           TEXT PRIMARY KEY,
                session_id   TEXT NOT NULL,
                machine      TEXT NOT NULL,
                owner        TEXT NOT NULL,
                reason       TEXT NOT NULL,
                status       TEXT NOT NULL,
                mime         TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                confirmed_at INTEGER,
                deadline     INTEGER,
                extensions   INTEGER NOT NULL DEFAULT 0,
                ended_at     INTEGER,
                end_reason   TEXT,
                size_bytes   INTEGER NOT NULL DEFAULT 0,
                next_seq     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recordings_owner ON recordings(owner)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recordings_session "
                     "ON recordings(session_id)")
        # One LIVE recording per session, enforced by the database rather than by create()'s
        # read-then-insert: two Start requests racing (a double click, a retried request)
        # could both read "none live" and both insert, and a viewer records one stream
        # (review of roadmap #19). Partial, so a session's ended recordings do not count.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_recordings_one_live "
                     "ON recordings(session_id) WHERE status IN ('starting', 'recording')")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recording_shares (
                recording_id TEXT NOT NULL,
                kind         TEXT NOT NULL,
                principal    TEXT NOT NULL,
                added_by     TEXT NOT NULL,
                added_at     INTEGER NOT NULL,
                PRIMARY KEY (recording_id, kind, principal)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recording_shares_principal "
                     "ON recording_shares(kind, principal)")


# ================================
# WHERE THE FILES LIVE
# ================================
_FOLDER_SAFE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789@._+-")


def owner_folder(owner):
    """The directory name for one owner: their email, lowercased, with anything outside a
    conservative set percent-encoded. The email is identity here
    (permissions.normalize_email), and an operator looking at the disk can tell whose folder
    is whose -- which a hash would hide.

    ENCODED, not replaced: replacing every odd character with `_` sent `a+b@x.com` and
    `a_b@x.com` to one folder (review of roadmap #19), so the folder no longer said whose.
    An encoding is one-to-one. A leading or trailing dot is encoded too, so the name is never
    `.` or `..` and never one Windows silently trims -- it cannot climb out of the root."""
    email = permissions.normalize_email(owner) or ""
    out = []
    for i, ch in enumerate(email):
        edge_dot = ch == "." and (i == 0 or i == len(email) - 1)
        if ch in _FOLDER_SAFE and not edge_dot:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out) or "_unknown"


_RECORDING_ID = re.compile(r"[0-9a-f]{32}")


def is_recording_id(value):
    """The shape every id create() mints (uuid4().hex). Routes check it before anything else,
    so a URL segment that could not be a recording never reaches the database or the disk."""
    return bool(_RECORDING_ID.fullmatch(str(value or "")))


def file_path(root, recording):
    """Where one recording's video lives -- and proof that it is inside `root`.

    Both halves are checked rather than trusted: the id must be one create() could have
    minted, and the resolved path must still be under the root once `..` and links are
    resolved. owner_folder() already cannot produce a climbing name, so the containment check
    is the second lock on the same door, kept because this path is handed to send_file and
    os.remove (CodeQL py/path-injection). Raises ValueError rather than returning a
    path outside."""
    recording_id = str(recording["id"])
    if not is_recording_id(recording_id):
        raise ValueError("not a recording id")
    base = os.path.realpath(root)
    path = os.path.realpath(os.path.join(base, owner_folder(recording["owner"]), "recordings",
                                         recording_id + FILE_EXTENSION))
    if not path.startswith(base + os.sep):
        raise ValueError("recording path escapes the recordings folder")
    return path


# ================================
# LIFECYCLE
# ================================
def _row(row):
    if row is None:
        return None
    rec = dict(row)
    rec["duration_seconds"] = _duration(rec)
    return rec


def _duration(rec):
    if not rec.get("confirmed_at"):
        return 0
    end = rec.get("ended_at") or int(time.time())
    return max(0, int(end) - int(rec["confirmed_at"]))


def get(db_path, recording_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM recordings WHERE id = ?",
                           (str(recording_id),)).fetchone()
    return _row(row)


def live_for_session(db_path, session_id):
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM recordings WHERE session_id = ? AND status IN (?, ?)",
            (str(session_id), *LIVE_STATUSES)).fetchall()
    return [_row(r) for r in rows]


def create(db_path, *, session_id, machine, owner, reason, mime, now=None):
    """A new recording in `starting`. The caller has already established that `owner` is the
    operator viewing `session_id` and may remote-control `machine`, and sends the agent its
    `record` signal next.

    The reason is required and kept verbatim (trimmed): it is the one line an auditor reads
    to decide whether recording this screen was justified."""
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to start a recording.")
    if len(reason) > MAX_REASON_CHARS:
        raise ValueError(f"The reason must be at most {MAX_REASON_CHARS} characters.")
    mime = str(mime or "").strip()
    if not ALLOWED_MIME.match(mime):
        raise ValueError("Recordings must be WebM video.")
    already = "This session is already being recorded."
    if live_for_session(db_path, session_id):
        raise ValueError(already)
    now = int(now if now is not None else time.time())
    recording_id = uuid.uuid4().hex
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO recordings(id, session_id, machine, owner, reason, status, mime, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (recording_id, str(session_id), str(machine),
                 permissions.normalize_email(owner), reason, STATUS_STARTING, mime, now))
    except sqlite3.IntegrityError:
        # The race the check above cannot see: idx_recordings_one_live refused the second.
        raise ValueError(already)
    fleet.audit(db_path, actor=owner, action="recording_start", level=fleet.LEVEL_SECURITY,
                target=str(machine),
                detail={"recording_id": recording_id, "session_id": str(session_id),
                        "reason": reason})
    return get(db_path, recording_id)


def confirm_badge(db_path, recording_id, machine, state, now=None):
    """The PC's helper reporting on its badge: `shown`, `hidden` or `failed`. Only the agent
    of the recording's own machine is heard -- the caller passes the machine its bearer token
    belongs to. Returns the recording after the report, or None if it is not this machine's.

    `shown` moves a starting recording to recording and starts its clock. `failed` ends it,
    whichever state it is in: a recording whose badge is not on screen is exactly what the
    owner ruled out."""
    now = int(now if now is not None else time.time())
    rec = get(db_path, recording_id)
    if rec is None or rec["machine"] != machine:
        return None
    if state == "shown" and rec["status"] == STATUS_STARTING:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE recordings SET status = ?, confirmed_at = ?, deadline = ? "
                "WHERE id = ? AND status = ?",
                (STATUS_RECORDING, now, now + SEGMENT_SECONDS, rec["id"], STATUS_STARTING))
    elif state == "failed":
        finish(db_path, rec["id"], END_BADGE_FAILED, actor=machine, now=now)
    return get(db_path, rec["id"])


def _lock_for(recording_id):
    with _append_locks_guard:
        return _append_locks.setdefault(recording_id, threading.Lock())


def append(db_path, root, recording_id, seq, data, now=None):
    """Store chunk `seq` of a recording. Returns the recording after the write.

    Chunks are appended in order -- a webm file is one stream -- so a gap is refused and a
    chunk already stored (a retry whose first attempt landed) is accepted without being
    written twice. Raises RecordingClosed once the recording is not recording, which is how
    the browser learns about an ending it did not cause (the session ended, the badge went,
    the deadline passed)."""
    now = int(now if now is not None else time.time())
    if len(data) > MAX_CHUNK_BYTES:
        raise ValueError("chunk too large")
    with _lock_for(recording_id):
        rec = get(db_path, recording_id)
        if rec is None:
            raise KeyError("unknown recording")
        if rec["status"] == STATUS_RECORDING and now > rec["deadline"] + DEADLINE_GRACE_SECONDS:
            finish(db_path, rec["id"], END_TIME_LIMIT, now=now)
            rec = get(db_path, recording_id)
        if rec["status"] != STATUS_RECORDING:
            raise RecordingClosed(rec)
        seq = int(seq)
        if seq < rec["next_seq"]:
            return rec
        if seq > rec["next_seq"]:
            raise ValueError(f"expected chunk {rec['next_seq']}, got {seq}")
        if rec["size_bytes"] + len(data) > MAX_RECORDING_BYTES:
            finish(db_path, rec["id"], END_SIZE_LIMIT, now=now)
            raise RecordingClosed(get(db_path, recording_id))
        path = file_path(root, rec)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as f:
            f.write(data)
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE recordings SET size_bytes = size_bytes + ?, next_seq = next_seq + 1 "
                "WHERE id = ?", (len(data), rec["id"]))
    return get(db_path, recording_id)


def extend(db_path, recording_id, actor, now=None):
    """Another SEGMENT_SECONDS. Only in the last EXTEND_WINDOW_SECONDS, which is when the
    browser offers it: an extension asked for at the start would be a way round the limit
    rather than an answer to the countdown. No reason -- the one given at start covers the
    whole recording."""
    now = int(now if now is not None else time.time())
    rec = get(db_path, recording_id)
    if rec is None:
        raise KeyError("unknown recording")
    if rec["status"] != STATUS_RECORDING or now > rec["deadline"] + DEADLINE_GRACE_SECONDS:
        raise RecordingClosed(rec)
    if rec["deadline"] - now > EXTEND_WINDOW_SECONDS:
        raise ValueError("A recording can only be extended in its last minute.")
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE recordings SET deadline = deadline + ?, extensions = extensions + 1 "
            "WHERE id = ? AND status = ?", (SEGMENT_SECONDS, rec["id"], STATUS_RECORDING))
    fleet.audit(db_path, actor=actor, action="recording_extend", level=fleet.LEVEL_SECURITY,
                target=rec["machine"],
                detail={"recording_id": rec["id"], "extensions": rec["extensions"] + 1})
    return get(db_path, recording_id)


def finish(db_path, recording_id, reason, actor="hub", now=None):
    """End a live recording. Idempotent: returns False, and audits nothing, for one that had
    already ended. One that never got its badge confirmed ends as `failed`, because nothing
    was recorded."""
    now = int(now if now is not None else time.time())
    rec = get(db_path, recording_id)
    if rec is None or rec["status"] not in LIVE_STATUSES:
        return False
    status = STATUS_ENDED if rec["status"] == STATUS_RECORDING else STATUS_FAILED
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE recordings SET status = ?, ended_at = ?, end_reason = ? "
            "WHERE id = ? AND status IN (?, ?)",
            (status, now, str(reason), rec["id"], *LIVE_STATUSES))
        if (cur.rowcount or 0) != 1:
            return False
    ended = get(db_path, rec["id"])
    fleet.audit(db_path, actor=actor, action="recording_end", level=fleet.LEVEL_SECURITY,
                target=rec["machine"],
                detail={"recording_id": rec["id"], "reason": str(reason), "status": status,
                        "duration_seconds": ended["duration_seconds"],
                        "size_bytes": ended["size_bytes"]})
    return True


def end_for_session(db_path, session_id, reason, actor="hub", now=None):
    """End every live recording of a session. Returns the ids ended."""
    ended = []
    for rec in live_for_session(db_path, session_id):
        if finish(db_path, rec["id"], reason, actor=actor, now=now):
            ended.append(rec["id"])
    return ended


def reconcile(db_path, session_is_live, now=None):
    """End the live recordings that have quietly stopped being live: a badge never confirmed,
    a deadline passed with no browser left to say so, or a session that ended by a path that
    did not call end_for_session (the TTL sweep). `session_is_live(session_id)` is passed in
    so this module needs nothing from remote.py. Returns the recordings it ended, so the
    caller can take their badges down."""
    now = int(now if now is not None else time.time())
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM recordings WHERE status IN (?, ?)",
                            LIVE_STATUSES).fetchall()
    ended = []
    for rec in (_row(r) for r in rows):
        reason = None
        if not session_is_live(rec["session_id"]):
            reason = END_SESSION_ENDED
        elif (rec["status"] == STATUS_STARTING
              and now - rec["created_at"] > BADGE_WAIT_SECONDS):
            reason = END_BADGE_TIMEOUT
        elif (rec["status"] == STATUS_RECORDING
              and now > rec["deadline"] + DEADLINE_GRACE_SECONDS):
            reason = END_TIME_LIMIT
        if reason and finish(db_path, rec["id"], reason, now=now):
            ended.append(get(db_path, rec["id"]))
    return ended


# ================================
# WHO MAY WATCH
# ================================
def can_view(db_path, rec, email, group_ids):
    """The owner, a person it is shared with by email, or a CURRENT member of a group it is
    shared with. `group_ids` is the viewer's groups as of this request (permissions_web's
    effective permissions), which is what makes a group share follow the group."""
    if rec is None:
        return False
    email = permissions.normalize_email(email)
    if email and rec["owner"] == email:
        return True
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT kind, principal FROM recording_shares WHERE recording_id = ?",
            (rec["id"],)).fetchall()
    groups = {str(g) for g in group_ids or ()}
    for row in rows:
        if row["kind"] == SHARE_USER and email and row["principal"] == email:
            return True
        if row["kind"] == SHARE_GROUP and row["principal"] in groups:
            return True
    return False


def list_owned(db_path, owner):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM recordings WHERE owner = ? ORDER BY created_at DESC",
                            (permissions.normalize_email(owner),)).fetchall()
    return [_row(r) for r in rows]


def list_shared_with(db_path, email, group_ids):
    """Recordings shared with this person, directly or through a group they are in now.
    Their own are left out -- they are listed as owned."""
    email = permissions.normalize_email(email)
    groups = [str(g) for g in group_ids or ()]
    clauses = ["(s.kind = ? AND s.principal = ?)"]
    params = [SHARE_USER, email]
    if groups:
        clauses.append(f"(s.kind = ? AND s.principal IN ({','.join('?' * len(groups))}))")
        params.extend([SHARE_GROUP, *groups])
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT r.* FROM recordings r JOIN recording_shares s "
            f"ON s.recording_id = r.id WHERE r.owner != ? AND ({' OR '.join(clauses)}) "
            "ORDER BY r.created_at DESC", [email, *params]).fetchall()
    return [_row(r) for r in rows]


def has_shared_with(db_path, email, group_ids):
    """Cheap existence check, for showing the sidebar link to someone with no remote access
    who has been shared a recording."""
    return bool(list_shared_with(db_path, email, group_ids))


def usage(db_path, owner):
    """What one owner's recordings take up. The owner decided recordings are kept until
    deleted, so this is the number that tells them when to delete some."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes FROM recordings "
            "WHERE owner = ?", (permissions.normalize_email(owner),)).fetchone()
    return {"count": row["n"], "bytes": row["bytes"]}


def shares(db_path, recording_id):
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT kind, principal, added_by, added_at FROM recording_shares "
            "WHERE recording_id = ? ORDER BY kind, principal", (str(recording_id),)).fetchall()
    return {"groups": [r["principal"] for r in rows if r["kind"] == SHARE_GROUP],
            "users": [r["principal"] for r in rows if r["kind"] == SHARE_USER]}


def _share_groups(db_path, groups):
    """The group ids to share with, deduplicated. An unknown one is refused rather than
    stored, so a share cannot silently point at nothing."""
    group_ids = []
    for gid in groups or []:
        gid = str(gid or "").strip()
        if not gid:
            continue
        if permissions.get_group(db_path, gid) is None:
            raise ValueError("One of the groups no longer exists.")
        if gid not in group_ids:
            group_ids.append(gid)
    return group_ids


def _share_users(users, owner):
    """The people to share with, normalised and deduplicated. The owner is dropped: sharing
    with yourself grants nothing."""
    emails = []
    for raw in users or []:
        email = permissions.normalize_email(raw)
        if not email:
            continue
        if "@" not in email or len(email) > 254 or any(c.isspace() for c in email):
            raise ValueError(f"'{raw}' is not an email address.")
        if email != owner and email not in emails:
            emails.append(email)
    if len(emails) > MAX_SHARE_USERS:
        raise ValueError(f"A recording can be shared with at most {MAX_SHARE_USERS} people.")
    return emails


def set_shares(db_path, rec, *, groups, users, actor, now=None):
    """Replace a recording's shares. The caller has established that `actor` is the owner --
    nobody else may share (no re-sharing). Returns (added, removed) for the audit."""
    now = int(now if now is not None else time.time())
    group_ids = _share_groups(db_path, groups)
    emails = _share_users(users, rec["owner"])

    before = shares(db_path, rec["id"])
    wanted = {(SHARE_GROUP, g) for g in group_ids} | {(SHARE_USER, e) for e in emails}
    had = ({(SHARE_GROUP, g) for g in before["groups"]}
           | {(SHARE_USER, e) for e in before["users"]})
    added, removed = sorted(wanted - had), sorted(had - wanted)
    with get_conn(db_path) as conn:
        for kind, principal in removed:
            conn.execute("DELETE FROM recording_shares WHERE recording_id = ? AND kind = ? "
                         "AND principal = ?", (rec["id"], kind, principal))
        for kind, principal in added:
            conn.execute("INSERT INTO recording_shares(recording_id, kind, principal, "
                         "added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                         (rec["id"], kind, principal, permissions.normalize_email(actor), now))
    if added or removed:
        fleet.audit(db_path, actor=actor, action="recording_share", level=fleet.LEVEL_SECURITY,
                    target=rec["machine"],
                    detail={"recording_id": rec["id"],
                            "added": [f"{k}:{p}" for k, p in added],
                            "removed": [f"{k}:{p}" for k, p in removed]})
    return added, removed


def delete(db_path, root, rec, actor):
    """Remove a recording and its file. Owner only (the caller checks). A live recording is
    ended first, so a delete during recording cannot leave the browser appending to a file
    that has gone."""
    finish(db_path, rec["id"], END_STOPPED, actor=actor)
    path = file_path(root, rec)
    with _lock_for(rec["id"]):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except PermissionError:
            # Windows will not delete a file somebody is reading -- a colleague playing or
            # downloading it right now. Refused rather than half-done: removing the record
            # and leaving the file would orphan video nobody can see or delete.
            raise ValueError("Someone is watching or downloading this recording right now. "
                             "Try again in a moment.")
        with get_conn(db_path) as conn:
            conn.execute("DELETE FROM recording_shares WHERE recording_id = ?", (rec["id"],))
            conn.execute("DELETE FROM recordings WHERE id = ?", (rec["id"],))
    with _append_locks_guard:
        _append_locks.pop(rec["id"], None)
    fleet.audit(db_path, actor=actor, action="recording_delete", level=fleet.LEVEL_SECURITY,
                target=rec["machine"],
                detail={"recording_id": rec["id"], "size_bytes": rec["size_bytes"]})
