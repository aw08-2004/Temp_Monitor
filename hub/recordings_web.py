"""Flask HTTP surface for session recordings (roadmap #19) -- a thin layer over recordings.py,
registered as a Blueprint from app.py.

**Two audiences, two gates.**

  * **Making a recording** (start, chunks, extend) is `remote_control` + the PC in scope:
    the owner's decision is that whoever may remote-view a PC may record it, and not a
    separate capability. Start additionally requires that the caller is the operator who
    opened the session -- the recording is of THEIR viewer's picture, and nobody else's
    browser holds it.
  * **Watching one** is ownership or a share, and nothing else. Recording permissions are not
    consulted: a recording shared with the helpdesk leads is theirs to watch even if they
    cannot remote into that PC themselves, which is the point of sharing it. Stopping is
    owner-only and never needs a capability -- an operator whose access was removed
    mid-recording must still be able to end it.

**Who sees what never depends on the URL.** Every recording route answers 404 for one the
caller may not see, owned or not, so the ids are not an oracle for which recordings exist.

**Chunks are a PUT, not a POST.** A chunk is binary, so it cannot meet the JSON content-type
rule login_required applies to POST -- and that rule is the second of two CSRF defences, not
the first. A PUT still needs the X-CSRF-Token header (common.js sends it on every non-GET),
and no HTML form can produce a PUT. *Rejected:* adding the route to app.CSRF_UPLOAD_ENDPOINTS,
whose own comment says what joining that list costs.

The rest of the CSRF note from fleet_web.py applies verbatim: JSON bodies are read with
request.get_json(silent=True).
"""
import time

from flask import Blueprint, jsonify, render_template, request, send_file

import fleet
import permissions
import permissions_web
import recordings
import refusals
import remote

# The end reasons a browser may give when it stops its own recording. Everything else (the
# session ended, the badge failed, the size cap) is the hub's to decide.
BROWSER_END_REASONS = (recordings.END_STOPPED, recordings.END_TIME_LIMIT,
                       recordings.END_STREAM_CHANGED)

# Recordings whose "drop the badge" signal has been sent, so a recording ended by several
# paths at once -- a 409'd chunk, the reconcile sweep and an explicit stop -- spends one of
# the session's signals on it, not three. Process-lifetime is enough: a restart forgets
# nothing that matters, because the helper drops its badge on its own when it exits.
# Cleared when it reaches _BADGES_DROPPED_CAP rather than growing for the life of the
# process; the cost of forgetting is at most one extra "drop" signal for a recording that
# has already ended, which the helper ignores.
_badges_dropped = set()
_BADGES_DROPPED_CAP = 10_000

# When each viewer last opened each recording's video, for the recording_view audit row.
# The video route cannot tell a player from a script saving the file -- both are GETs of
# the same bytes, ranged or not -- so every access is audited, but a player's stream of
# range requests is one row per viewer per recording per VIEW_AUDIT_WINDOW_SECONDS rather
# than one per request. Pruned of expired entries when it grows.
_last_view_audit = {}
VIEW_AUDIT_WINDOW_SECONDS = 30 * 60


class _RecordingRoutes:
    """The route handlers, as methods rather than closures inside the factory.

    They are methods because a closure's branches count toward the function that encloses
    it: with eleven handlers inside it, the factory read as one function far over any
    complexity limit, and the PR's quality gate refused it. The factory below only registers
    routes and applies gates; everything that decides anything is here, one method per
    question."""

    def __init__(self, db_path, access, root):
        self.db_path = db_path
        self.access = access
        self.root = root

    # ---------------- helpers ----------------
    @staticmethod
    def me():
        return permissions.normalize_email(permissions_web.current_actor())

    def my_groups(self):
        return [g["id"] for g in self.access.current().get("groups") or []]

    def session_live(self, session_id):
        return remote.is_session_live(self.db_path, session_id)

    def drop_badge(self, rec):
        """Ask the PC to take the badge down for a recording that has ended. Best effort: if
        the session is gone, the helper is gone, and its badge with it."""
        if rec is None or rec["id"] in _badges_dropped or rec["status"] in recordings.LIVE_STATUSES:
            return
        if len(_badges_dropped) >= _BADGES_DROPPED_CAP:
            _badges_dropped.clear()
        _badges_dropped.add(rec["id"])
        if not self.session_live(rec["session_id"]):
            return
        try:
            remote.add_signal(self.db_path, rec["session_id"], remote.SENDER_CONSOLE, "record",
                              {"recording_id": rec["id"], "on": False})
        except (KeyError, PermissionError, ValueError):
            pass

    def reconcile(self):
        for rec in recordings.reconcile(self.db_path, self.session_live):
            self.drop_badge(rec)

    def public(self, rec, *, owned):
        out = {k: rec[k] for k in (
            "id", "machine", "owner", "reason", "status", "created_at", "confirmed_at",
            "deadline", "extensions", "ended_at", "end_reason", "size_bytes",
            "duration_seconds")}
        # Only the owner sees who else may watch it: they are the only one who can change it.
        if owned:
            out["shares"] = recordings.shares(self.db_path, rec["id"])
        return out

    def owned_or_none(self, recording_id):
        if not recordings.is_recording_id(recording_id):
            return None
        rec = recordings.get(self.db_path, recording_id)
        if rec is None or rec["owner"] != self.me():
            return None
        return rec

    @staticmethod
    def not_found():
        return jsonify({"error": "unknown recording"}), 404

    def may_record(self, machine):
        return self.access.can(permissions.REMOTE_CONTROL) and self.access.in_scope(machine)

    @staticmethod
    def no_access(machine):
        return jsonify({"error": f"You do not have access to {machine!r}."}), 403

    def closed(self, exc):
        """A 409 carrying the recording, so the browser can tell the operator why it ended."""
        self.drop_badge(exc.recording)
        return jsonify({"error": "This recording has ended.",
                        "recording": self.public(exc.recording, owned=True)}), 409

    def with_clock(self, rec):
        """`server_time` lets the browser run its countdown against the hub's clock."""
        return jsonify(dict(self.public(rec, owned=True), server_time=int(time.time()))), 200

    @staticmethod
    def read_capped(limit):
        """The request body, or None if it is longer than `limit`, read without holding more
        than `limit + 1` bytes -- remote_web._read_capped's reasoning: a chunked request has
        no Content-Length, and the hub sets no MAX_CONTENT_LENGTH."""
        if (request.content_length or 0) > limit:
            return None
        chunks, size = [], 0
        while size <= limit:
            chunk = request.stream.read(min(64 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return None if size > limit else b"".join(chunks)

    # ---------------- making a recording ----------------
    def recordable_session(self, machine, session_id):
        """The session to record, or the refusal: it must be this PC's, still live, and the
        caller's own -- the recording is of THEIR viewer's picture."""
        sess = remote.get_session(self.db_path, str(session_id or ""))
        if sess is None or sess["machine"] != machine:
            return None, (jsonify({"error": "unknown session"}), 404)
        if not self.session_live(sess["id"]):
            return None, (jsonify({"error": "That session has ended."}), 409)
        if permissions.normalize_email(sess["issued_by"]) != self.me():
            return None, (jsonify({"error": "Only the operator viewing this session can "
                                            "record it."}), 403)
        return sess, None

    def start(self, machine):
        data = request.get_json(silent=True) or {}
        sess, refused = self.recordable_session(machine, data.get("session_id"))
        if refused:
            return refused
        self.reconcile()
        try:
            rec = recordings.create(self.db_path, session_id=sess["id"], machine=machine,
                                    owner=self.me(), reason=data.get("reason"),
                                    mime=data.get("mime"))
        except ValueError as e:
            return refusals.refuse(e)
        try:
            remote.add_signal(self.db_path, sess["id"], remote.SENDER_CONSOLE, "record",
                              {"recording_id": rec["id"], "on": True})
        except (KeyError, PermissionError, ValueError) as e:
            recordings.finish(self.db_path, rec["id"], recordings.END_BADGE_FAILED,
                              actor=self.me())
            return refusals.refuse(e, 409)
        return jsonify(self.public(rec, owned=True)), 201

    def status(self, recording_id):
        """Polled by the recording browser: first for the PC's badge confirmation, then for
        an ending it did not cause."""
        self.reconcile()
        rec = self.owned_or_none(recording_id)
        return self.not_found() if rec is None else self.with_clock(rec)

    def chunk(self, recording_id, seq):
        rec = self.owned_or_none(recording_id)
        if rec is None:
            return self.not_found()
        if not self.may_record(rec["machine"]):
            return self.no_access(rec["machine"])
        if not self.session_live(rec["session_id"]):
            recordings.finish(self.db_path, rec["id"], recordings.END_SESSION_ENDED)
            return self.closed(recordings.RecordingClosed(recordings.get(self.db_path, rec["id"])))
        data = self.read_capped(recordings.MAX_CHUNK_BYTES)
        if data is None:
            return jsonify({"error": "chunk too large"}), 413
        try:
            rec = recordings.append(self.db_path, self.root, rec["id"], seq, data)
        except recordings.RecordingClosed as e:
            return self.closed(e)
        except KeyError:
            return self.not_found()
        except ValueError as e:
            return refusals.refuse(e)
        return jsonify({"next_seq": rec["next_seq"], "size_bytes": rec["size_bytes"],
                        "deadline": rec["deadline"], "status": rec["status"],
                        "server_time": int(time.time())}), 200

    def extend(self, recording_id):
        # Reconciled first, as status() is: a session that ended since the last sweep must
        # end its recording here, not have it extended.
        self.reconcile()
        rec = self.owned_or_none(recording_id)
        if rec is None:
            return self.not_found()
        if not self.may_record(rec["machine"]):
            return self.no_access(rec["machine"])
        try:
            rec = recordings.extend(self.db_path, rec["id"], actor=self.me())
        except recordings.RecordingClosed as e:
            return self.closed(e)
        except ValueError as e:
            return refusals.refuse(e, 409)
        return self.with_clock(rec)

    def stop(self, recording_id):
        rec = self.owned_or_none(recording_id)
        if rec is None:
            return self.not_found()
        reason = (request.get_json(silent=True) or {}).get("reason")
        if reason not in BROWSER_END_REASONS:
            reason = recordings.END_STOPPED
        recordings.finish(self.db_path, rec["id"], reason, actor=self.me())
        rec = recordings.get(self.db_path, rec["id"])
        self.drop_badge(rec)
        return jsonify(self.public(rec, owned=True)), 200

    # ---------------- the library ----------------
    def library(self):
        self.reconcile()
        me = self.me()
        owned = recordings.list_owned(self.db_path, me)
        # Group names are for the share editor, so only someone with something to share gets
        # them: an owner, or someone who can record. A person who has only been SHARED a
        # recording gets none -- every group's name is not something they otherwise see.
        # Ids and names only, even then -- membership is not anybody else's business.
        sharer = bool(owned) or self.access.can(permissions.REMOTE_CONTROL)
        groups = permissions.list_groups(self.db_path) if sharer else []
        return jsonify({
            "owned": [self.public(r, owned=True) for r in owned],
            "shared": [self.public(r, owned=False)
                       for r in recordings.list_shared_with(self.db_path, me, self.my_groups())],
            "usage": recordings.usage(self.db_path, me),
            "groups": [{"id": g["id"], "name": g["name"]} for g in groups],
        }), 200

    def audit_access(self, rec, download, now=None):
        """recording_download for every Download; recording_view for any other access, once
        per viewer per recording per window (see VIEW_AUDIT_WINDOW_SECONDS)."""
        now = time.time() if now is None else now
        me = self.me()
        if not download:
            key = (me, rec["id"])
            if now - _last_view_audit.get(key, 0) < VIEW_AUDIT_WINDOW_SECONDS:
                return
            if len(_last_view_audit) > 10_000:
                for stale in [k for k, t in _last_view_audit.items()
                              if now - t >= VIEW_AUDIT_WINDOW_SECONDS]:
                    del _last_view_audit[stale]
            _last_view_audit[key] = now
        fleet.audit(self.db_path, actor=me,
                    action="recording_download" if download else "recording_view",
                    level=fleet.LEVEL_SECURITY, target=rec["machine"],
                    detail={"recording_id": rec["id"], "owner": rec["owner"]})

    def viewable_or_none(self, recording_id):
        if not recordings.is_recording_id(recording_id):
            return None
        rec = recordings.get(self.db_path, recording_id)
        return rec if recordings.can_view(self.db_path, rec, self.me(), self.my_groups()) else None

    def video(self, recording_id):
        """The file, for the player (ranged, so it can seek) or as a download (`?download=1`).
        Only once it has ended: a file still being appended to cannot be played to the end
        or sought in, and is not yet the recording anyone was asked to keep.

        Every access is audited, because this route cannot tell playing from copying: a plain
        or ranged GET returns the same bytes a Download does, so auditing only `?download=1`
        audited the button rather than the copy (review of roadmap #19). `?download=1` writes
        recording_download every time; anything else writes recording_view at most once per
        viewer per recording per VIEW_AUDIT_WINDOW_SECONDS, so a player's range requests are
        one row, not hundreds."""
        rec = self.viewable_or_none(recording_id)
        if rec is None:
            return self.not_found()
        if rec["status"] != recordings.STATUS_ENDED or not rec["size_bytes"]:
            return jsonify({"error": "This recording has no video to show yet."}), 409
        try:
            path = recordings.file_path(self.root, rec)
        except ValueError:
            return self.not_found()
        download = request.args.get("download") == "1"
        self.audit_access(rec, download)
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(rec["created_at"]))
        try:
            resp = send_file(path, mimetype=rec["mime"].split(";")[0], conditional=True,
                             as_attachment=download,
                             download_name=f"{rec['machine']}-{stamp}{recordings.FILE_EXTENSION}")
        except FileNotFoundError:
            return jsonify({"error": "The video file is missing on the hub."}), 410
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    def update_shares(self, recording_id):
        """Owner only -- the no-re-sharing rule is this check."""
        rec = self.owned_or_none(recording_id)
        if rec is None:
            return self.not_found()
        data = request.get_json(silent=True) or {}
        groups, users = data.get("groups") or [], data.get("users") or []
        if not isinstance(groups, list) or not isinstance(users, list):
            return jsonify({"error": "groups and users must be lists"}), 400
        try:
            recordings.set_shares(self.db_path, rec, groups=groups, users=users, actor=self.me())
        except ValueError as e:
            return refusals.refuse(e)
        return jsonify(self.public(recordings.get(self.db_path, rec["id"]), owned=True)), 200

    def delete(self, recording_id):
        rec = self.owned_or_none(recording_id)
        if rec is None:
            return self.not_found()
        try:
            recordings.delete(self.db_path, self.root, rec, actor=self.me())
        except ValueError as e:
            return refusals.refuse(e, 409)
        finally:
            # In a finally because delete() ends a live recording BEFORE it tries the file.
            # A delete refused over a file somebody is reading has still ended the recording,
            # and reconcile() never revisits an ended one -- so a badge left up here would
            # stay on the PC until the session went (review of roadmap #19).
            self.drop_badge(dict(rec, status=recordings.STATUS_ENDED))
        return jsonify({"status": "deleted"}), 200


def create_recordings_blueprint(db_path, login_required, access, root):
    """`root` is where recordings live on disk (app.RECORDINGS_ROOT): one folder per owner.

    Registration and gates only -- the handlers are _RecordingRoutes'. The endpoint names
    are the old closures' names, which templates resolve with url_for."""
    bp = Blueprint("recordings", __name__)
    routes = _RecordingRoutes(db_path, access, root)

    # ---------------- Making a recording (remote_control + scope) ----------------
    @bp.route("/api/remote/<machine>/recordings", methods=["POST"])
    @login_required
    @access.require_machine(permissions.REMOTE_CONTROL)
    def start_recording(machine):
        return routes.start(machine)

    @bp.route("/api/remote/recordings/<recording_id>", methods=["GET"])
    @login_required
    def recording_status(recording_id):
        return routes.status(recording_id)

    @bp.route("/api/remote/recordings/<recording_id>/chunks/<int:seq>", methods=["PUT"])
    @login_required
    def recording_chunk(recording_id, seq):
        return routes.chunk(recording_id, seq)

    @bp.route("/api/remote/recordings/<recording_id>/extend", methods=["POST"])
    @login_required
    def extend_recording(recording_id):
        return routes.extend(recording_id)

    @bp.route("/api/remote/recordings/<recording_id>/stop", methods=["POST"])
    @login_required
    def stop_recording(recording_id):
        return routes.stop(recording_id)

    # ---------------- The library (owner or share) ----------------
    @bp.route("/recordings", methods=["GET"])
    @login_required
    def recordings_page():
        return render_template("recordings.html")

    @bp.route("/api/recordings", methods=["GET"])
    @login_required
    def list_recordings():
        return routes.library()

    @bp.route("/api/recordings/<recording_id>/video", methods=["GET"])
    @login_required
    def recording_video(recording_id):
        return routes.video(recording_id)

    @bp.route("/api/recordings/<recording_id>/shares", methods=["PUT"])
    @login_required
    def update_shares(recording_id):
        return routes.update_shares(recording_id)

    @bp.route("/api/recordings/<recording_id>", methods=["DELETE"])
    @login_required
    def delete_recording(recording_id):
        return routes.delete(recording_id)

    return bp
