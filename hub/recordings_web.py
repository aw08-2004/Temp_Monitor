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
_badges_dropped = set()


def create_recordings_blueprint(db_path, login_required, access, root):
    """`root` is where recordings live on disk (app.RECORDINGS_ROOT): one folder per owner."""
    bp = Blueprint("recordings", __name__)

    def _me():
        return permissions.normalize_email(permissions_web.current_actor())

    def _my_groups():
        return [g["id"] for g in access.current().get("groups") or []]

    def _session_live(session_id):
        sess = remote.get_session(db_path, session_id)
        return (sess is not None
                and sess["status"] in (remote.STATUS_PENDING, remote.STATUS_CONNECTING,
                                       remote.STATUS_ACTIVE)
                and sess["expires_at"] > time.time())

    def _drop_badge(rec):
        """Ask the PC to take the badge down for a recording that has ended. Best effort: if
        the session is gone, the helper is gone, and its badge with it."""
        if rec is None or rec["id"] in _badges_dropped or rec["status"] in recordings.LIVE_STATUSES:
            return
        _badges_dropped.add(rec["id"])
        if not _session_live(rec["session_id"]):
            return
        try:
            remote.add_signal(db_path, rec["session_id"], remote.SENDER_CONSOLE, "record",
                              {"recording_id": rec["id"], "on": False})
        except (KeyError, PermissionError, ValueError):
            pass

    def _reconcile():
        for rec in recordings.reconcile(db_path, _session_live):
            _drop_badge(rec)

    def _public(rec, *, owned):
        out = {k: rec[k] for k in (
            "id", "machine", "owner", "reason", "status", "created_at", "confirmed_at",
            "deadline", "extensions", "ended_at", "end_reason", "size_bytes",
            "duration_seconds")}
        # Only the owner sees who else may watch it: they are the only one who can change it.
        if owned:
            out["shares"] = recordings.shares(db_path, rec["id"])
        return out

    def _owned_or_404(recording_id):
        rec = recordings.get(db_path, recording_id)
        if rec is None or rec["owner"] != _me():
            return None
        return rec

    def _not_found():
        return jsonify({"error": "unknown recording"}), 404

    def _may_record(machine):
        return access.can(permissions.REMOTE_CONTROL) and access.in_scope(machine)

    def _closed(exc):
        """A 409 carrying the recording, so the browser can tell the operator why it ended."""
        _drop_badge(exc.recording)
        return jsonify({"error": "This recording has ended.",
                        "recording": _public(exc.recording, owned=True)}), 409

    # ---------------- Making a recording (remote_control + scope) ----------------
    @bp.route("/api/remote/<machine>/recordings", methods=["POST"])
    @login_required
    @access.require_machine(permissions.REMOTE_CONTROL)
    def start_recording(machine):
        data = request.get_json(silent=True) or {}
        sess = remote.get_session(db_path, str(data.get("session_id") or ""))
        if sess is None or sess["machine"] != machine:
            return jsonify({"error": "unknown session"}), 404
        if not _session_live(sess["id"]):
            return jsonify({"error": "That session has ended."}), 409
        if permissions.normalize_email(sess["issued_by"]) != _me():
            return jsonify({"error": "Only the operator viewing this session can record "
                                     "it."}), 403
        _reconcile()
        try:
            rec = recordings.create(db_path, session_id=sess["id"], machine=machine,
                                    owner=_me(), reason=data.get("reason"),
                                    mime=data.get("mime"))
        except ValueError as e:
            return refusals.refuse(e)
        try:
            remote.add_signal(db_path, sess["id"], remote.SENDER_CONSOLE, "record",
                              {"recording_id": rec["id"], "on": True})
        except (KeyError, PermissionError, ValueError) as e:
            recordings.finish(db_path, rec["id"], recordings.END_BADGE_FAILED, actor=_me())
            return refusals.refuse(e, 409)
        return jsonify(_public(rec, owned=True)), 201

    @bp.route("/api/remote/recordings/<recording_id>", methods=["GET"])
    @login_required
    def recording_status(recording_id):
        """Polled by the recording browser: first for the PC's badge confirmation, then for
        an ending it did not cause. `server_time` lets it run the countdown against the hub's
        clock rather than its own."""
        _reconcile()
        rec = _owned_or_404(recording_id)
        if rec is None:
            return _not_found()
        return jsonify(dict(_public(rec, owned=True), server_time=int(time.time()))), 200

    @bp.route("/api/remote/recordings/<recording_id>/chunks/<int:seq>", methods=["PUT"])
    @login_required
    def recording_chunk(recording_id, seq):
        rec = _owned_or_404(recording_id)
        if rec is None:
            return _not_found()
        if not _may_record(rec["machine"]):
            return jsonify({"error": f"You do not have access to {rec['machine']!r}."}), 403
        if not _session_live(rec["session_id"]):
            recordings.finish(db_path, rec["id"], recordings.END_SESSION_ENDED)
            return _closed(recordings.RecordingClosed(recordings.get(db_path, rec["id"])))
        data = _read_capped(recordings.MAX_CHUNK_BYTES)
        if data is None:
            return jsonify({"error": "chunk too large"}), 413
        try:
            rec = recordings.append(db_path, root, rec["id"], seq, data)
        except recordings.RecordingClosed as e:
            return _closed(e)
        except KeyError:
            return _not_found()
        except ValueError as e:
            return refusals.refuse(e)
        return jsonify({"next_seq": rec["next_seq"], "size_bytes": rec["size_bytes"],
                        "deadline": rec["deadline"], "status": rec["status"],
                        "server_time": int(time.time())}), 200

    def _read_capped(limit):
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

    @bp.route("/api/remote/recordings/<recording_id>/extend", methods=["POST"])
    @login_required
    def extend_recording(recording_id):
        rec = _owned_or_404(recording_id)
        if rec is None:
            return _not_found()
        if not _may_record(rec["machine"]):
            return jsonify({"error": f"You do not have access to {rec['machine']!r}."}), 403
        try:
            rec = recordings.extend(db_path, rec["id"], actor=_me())
        except recordings.RecordingClosed as e:
            return _closed(e)
        except ValueError as e:
            return refusals.refuse(e, 409)
        return jsonify(dict(_public(rec, owned=True), server_time=int(time.time()))), 200

    @bp.route("/api/remote/recordings/<recording_id>/stop", methods=["POST"])
    @login_required
    def stop_recording(recording_id):
        rec = _owned_or_404(recording_id)
        if rec is None:
            return _not_found()
        reason = (request.get_json(silent=True) or {}).get("reason") or recordings.END_STOPPED
        if reason not in BROWSER_END_REASONS:
            reason = recordings.END_STOPPED
        recordings.finish(db_path, rec["id"], reason, actor=_me())
        rec = recordings.get(db_path, rec["id"])
        _drop_badge(rec)
        return jsonify(_public(rec, owned=True)), 200

    # ---------------- The library (owner or share) ----------------
    @bp.route("/recordings")
    @login_required
    def recordings_page():
        return render_template("recordings.html")

    @bp.route("/api/recordings", methods=["GET"])
    @login_required
    def list_recordings():
        _reconcile()
        me = _me()
        groups = _my_groups()
        return jsonify({
            "owned": [_public(r, owned=True) for r in recordings.list_owned(db_path, me)],
            "shared": [_public(r, owned=False)
                       for r in recordings.list_shared_with(db_path, me, groups)],
            "usage": recordings.usage(db_path, me),
            # Names for the share editor and for showing an owner's existing group shares.
            # Ids and names only -- membership is not anybody else's business.
            "groups": [{"id": g["id"], "name": g["name"]}
                       for g in permissions.list_groups(db_path)],
        }), 200

    @bp.route("/api/recordings/<recording_id>/video", methods=["GET"])
    @login_required
    def recording_video(recording_id):
        """The file, for the player (ranged, so it can seek) or as a download (`?download=1`).
        Only once it has ended: a file still being appended to cannot be played to the end
        or sought in, and is not yet the recording anyone was asked to keep."""
        rec = recordings.get(db_path, recording_id)
        if not recordings.can_view(db_path, rec, _me(), _my_groups()):
            return _not_found()
        if rec["status"] != recordings.STATUS_ENDED or not rec["size_bytes"]:
            return jsonify({"error": "This recording has no video to show yet."}), 409
        download = request.args.get("download") == "1"
        if download:
            fleet.audit(db_path, actor=_me(), action="recording_download",
                        level=fleet.LEVEL_SECURITY, target=rec["machine"],
                        detail={"recording_id": rec["id"], "owner": rec["owner"]})
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(rec["created_at"]))
        name = f"{rec['machine']}-{stamp}{recordings.FILE_EXTENSION}"
        try:
            resp = send_file(recordings.file_path(root, rec),
                             mimetype=rec["mime"].split(";")[0], conditional=True,
                             as_attachment=download, download_name=name)
        except FileNotFoundError:
            return jsonify({"error": "The video file is missing on the hub."}), 410
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    @bp.route("/api/recordings/<recording_id>/shares", methods=["PUT"])
    @login_required
    def update_shares(recording_id):
        """Owner only -- the no-re-sharing rule is this check."""
        rec = _owned_or_404(recording_id)
        if rec is None:
            return _not_found()
        data = request.get_json(silent=True) or {}
        groups, users = data.get("groups") or [], data.get("users") or []
        if not isinstance(groups, list) or not isinstance(users, list):
            return jsonify({"error": "groups and users must be lists"}), 400
        try:
            recordings.set_shares(db_path, rec, groups=groups, users=users, actor=_me())
        except ValueError as e:
            return refusals.refuse(e)
        return jsonify(_public(recordings.get(db_path, rec["id"]), owned=True)), 200

    @bp.route("/api/recordings/<recording_id>", methods=["DELETE"])
    @login_required
    def delete_recording(recording_id):
        rec = _owned_or_404(recording_id)
        if rec is None:
            return _not_found()
        try:
            recordings.delete(db_path, root, rec, actor=_me())
        except ValueError as e:
            return refusals.refuse(e, 409)
        _drop_badge(dict(rec, status=recordings.STATUS_ENDED))
        return jsonify({"status": "deleted"}), 200

    return bp
