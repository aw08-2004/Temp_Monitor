"""Flask HTTP surface for the remote file explorer -- a thin layer over files.py,
registered as a Blueprint from app.py.

**One gate, and it is `issue_commands` + machine scope -- including for browsing.** That is
a deliberate departure from the Processes card next door, which reads behind `view` on the
argument that a process list is inventory. A directory listing is not inventory: the folder
names on a machine are the names of somebody's documents, and the same door leads directly
to reading the bytes in them. What settles it is that this capability ALREADY grants exactly
this reach -- an operator with `issue_commands` can open a SYSTEM shell on the machine and
`type` any file on it -- so gating the explorer here adds no capability to anyone, while
gating it at `view` would hand a read of every user's files to everyone who can see a
temperature graph.

**Nothing here waits for the machine.** Every console call queues a command (or spools some
bytes) and answers immediately with an id; the browser polls for the answer, exactly as the
Terminal, Firmware and Processes surfaces do. An endpoint that blocked on an agent's next
poll would hold a hub worker thread for up to ten seconds per click, and this is a UI where
an operator clicks a great deal.

**The upload is two requests, and that is a CSRF control.** `POST .../files/upload` takes
multipart -- the one state-changing shape a cross-site HTML form can still produce -- so it
is inert by construction: it spools bytes to disk, returns an id, and touches no machine.
`POST .../files/push` is the JSON call that gives those bytes a destination and queues the
command, and JSON is what app.login_required's content-type rule covers. This mirrors
packages.upload_package_file exactly; see app.CSRF_UPLOAD_ENDPOINTS, which the upload route
below must be named in.

**The agent-facing half (`/api/agent/files/*`) sits behind agent bearer auth**, and every
one of those routes re-checks that the row it is about belongs to the CALLING machine. An
enrolled agent is trusted to act for itself and for nothing else: PC-3 must not be able to
answer PC-4's listing or read the bytes queued for PC-4's disk.
"""
import os

from flask import Blueprint, jsonify, request, send_file
from werkzeug.utils import secure_filename

import fleet
import files
import permissions
import permissions_web
import settings


def _bearer_agent(db_path):
    """Same header contract as fleet_web._bearer_agent: 'Bearer <agent_id>:<token>'."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, None
    raw = header[len("Bearer "):].strip()
    agent_id, _, token = raw.partition(":")
    if not agent_id or not token:
        return None, None
    machine = fleet.authenticate_agent(db_path, agent_id, token)
    if machine is None:
        return None, None
    return agent_id, machine


def create_files_blueprint(db_path, spool_dir, login_required, access, hub_url=""):
    """Build the files Blueprint.

    `spool_dir` is where in-flight transfers are parked -- passed in rather than derived
    here so the test suite can re-point it, exactly like backups_web's `log_dir`. `hub_url`
    is the hub's PUBLIC origin, needed because the agent is handed an absolute URL to PUT to
    or GET from, and that URL must be the address the fleet reaches rather than whatever Host
    header happened to arrive.
    """
    bp = Blueprint("files", __name__)
    os.makedirs(spool_dir, exist_ok=True)

    def _current_email():
        return permissions_web.current_actor()

    def _require_json():
        """Refuse anything that is not application/json.

        app.py's login_required already applies this rule; it is restated at the route
        because that is the control which stops a cross-site form POST from deleting a
        signed-in operator's files, and a control that lives only in a decorator somebody
        else owns is one refactor away from being absent.
        """
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _queue(machine, command_type, params):
        """Queue one file command and answer with its id."""
        return fleet.create_command(
            db_path,
            machine=machine,
            command_type=command_type,
            params=params,
            issued_by=_current_email(),
            ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
        )

    def _agent_url(suffix):
        return f"{str(hub_url or '').rstrip('/')}/api/agent/files/{suffix}"

    # ================================================================
    # Console: browsing
    # ================================================================
    @bp.route("/api/machines/<machine>/files/list", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_list_directory(machine):
        """Ask this machine what is in one folder. Answers with a request id to poll.

        An empty or absent `path` means "the drives" -- the explorer's root view. That is a
        real answer rather than an error: an operator opening the tool has no path in mind
        yet, and a machine's volume list is the first thing they need.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        machine_name = str(machine).strip()
        raw_path = (data.get("path") or "").strip()

        if raw_path:
            try:
                request_id, clean = files.create_listing(db_path, machine_name, raw_path)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            params = {"request_id": request_id, "path": clean}
        else:
            # The drive list is still a listing row -- same polling, same expiry, same
            # audit line -- it just names no folder. See files.DRIVES_PATH.
            request_id, _ = files.create_listing(db_path, machine_name, None, drives=True)
            params = {"request_id": request_id, "drives": True}

        try:
            command_id = _queue(machine_name, "list_directory", params)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"request_id": request_id, "command_id": command_id,
                        "poll_interval": files.POLL_INTERVAL_SECONDS}), 201

    @bp.route("/api/machines/<machine>/files/list/<request_id>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_get_listing(machine, request_id):
        """The answer to one listing request, or `pending` while the machine is thinking.

        404 rather than an empty listing for an unknown id: a request that has been pruned
        and a request that was never made are the same fact to the console (ask again), and
        neither should render as an empty folder -- which is a claim about the disk.
        """
        payload = files.get_listing(db_path, request_id, machine=str(machine).strip())
        if payload is None:
            return jsonify({"error": "unknown listing request"}), 404
        payload["poll_interval"] = files.POLL_INTERVAL_SECONDS
        return jsonify(payload), 200

    # ================================================================
    # Console: operations
    # ================================================================
    @bp.route("/api/machines/<machine>/files/operation", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_file_operation(machine):
        """Copy, move, rename, delete, or make a folder. Answers with a command id.

        The console polls `/api/fleet/commands/<id>` for the outcome and re-lists the folder
        when it lands, rather than this endpoint trying to predict what the disk will look
        like afterwards. A copy that half-succeeded is a real state, and the only honest way
        to render it is to go and look.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        try:
            params = files.validate_operation(
                data.get("op"),
                paths=data.get("paths"),
                destination=data.get("destination"),
                new_name=data.get("new_name"),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            command_id = _queue(str(machine).strip(), "file_operation", params)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"command_id": command_id}), 201

    # ================================================================
    # Console: download (machine -> hub -> browser)
    # ================================================================
    @bp.route("/api/machines/<machine>/files/download", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_download_file(machine):
        """Fetch one file -- or one folder, zipped -- off the machine.

        Two hops, because the two ends are never both present at once: the agent PUTs the
        bytes to the hub, which spools them, and the browser collects them from the spool
        when it sees the transfer go ready. A direct stream would require the operator's
        browser and the machine to be awake at the same instant on the same network, which
        is exactly the assumption this whole product is built to avoid.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        machine_name = str(machine).strip()
        kind = str(data.get("kind") or files.KIND_FILE).strip().lower()
        if kind not in files.KINDS:
            return jsonify({"error": "kind must be file or folder"}), 400

        try:
            path = files.validate_path(data.get("path"))
            parent = files.parent_path(path)
            if parent is None:
                # A drive root has no name to save under, and "download C:\" is a request
                # for the machine's whole disk. Neither is what the button means.
                return jsonify({"error": "pick a file or a folder, not a drive"}), 400
            name = path.rsplit("\\", 1)[-1]
            # A folder arrives as a zip, so the download is named like one. Decided here
            # rather than in the browser because the agent is building the archive and the
            # console must not have to guess what it will be handed.
            download_name = f"{name}.zip" if kind == files.KIND_FOLDER else name
            transfer_id = files.create_transfer(
                db_path, machine_name, files.PULL, path, download_name,
                kind=kind, issued_by=_current_email())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        try:
            command_id = _queue(machine_name, "fetch_file", {
                "transfer_id": transfer_id,
                "path": path,
                "kind": kind,
                "url": _agent_url(f"transfer/{transfer_id}"),
                "max_bytes": files.MAX_TRANSFER_BYTES,
            })
        except ValueError as e:
            files.fail_transfer(db_path, transfer_id, str(e))
            return jsonify({"error": str(e)}), 400
        return jsonify({"transfer_id": transfer_id, "command_id": command_id,
                        "name": download_name,
                        "poll_interval": files.POLL_INTERVAL_SECONDS}), 201

    @bp.route("/api/machines/<machine>/files/transfers/<transfer_id>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_transfer_status(machine, transfer_id):
        """Where one transfer has got to. Polled by the console in both directions."""
        transfer = files.get_transfer(db_path, transfer_id, machine=str(machine).strip())
        if transfer is None:
            return jsonify({"error": "unknown transfer"}), 404
        # The spool NAME is hub-internal plumbing and never leaves the building.
        transfer.pop("spool", None)
        transfer["poll_interval"] = files.POLL_INTERVAL_SECONDS
        return jsonify(transfer), 200

    @bp.route("/api/machines/<machine>/files/transfers/<transfer_id>/content",
              methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_transfer_content(machine, transfer_id):
        """The downloaded bytes, as an attachment.

        Served with `as_attachment` and a generic octet-stream type on purpose. These bytes
        came off a managed machine and are entirely under the control of whoever put them
        there; served inline, an uploaded .html would run as script on the hub's own origin,
        against the session of the operator who downloaded it. The one thing this response
        must never do is let a remote machine's file execute in the console.
        """
        transfer = files.get_transfer(db_path, transfer_id, machine=str(machine).strip())
        if transfer is None:
            return jsonify({"error": "unknown transfer"}), 404
        if transfer["direction"] != files.PULL:
            return jsonify({"error": "that transfer is an upload"}), 400
        if transfer["status"] != files.READY or not transfer["spool"]:
            return jsonify({"error": "that download is not ready"}), 409
        try:
            path = files.spool_path(spool_dir, transfer["spool"])
        except ValueError:
            return jsonify({"error": "that download is no longer available"}), 410
        if not os.path.isfile(path):
            return jsonify({"error": "that download has expired"}), 410
        return send_file(path, as_attachment=True,
                         download_name=transfer["name"],
                         mimetype="application/octet-stream")

    # ================================================================
    # Console: upload (browser -> hub -> machine)
    # ================================================================
    @bp.route("/api/machines/<machine>/files/upload", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def upload_file_to_spool(machine):
        """Park one file on the hub. Creates no command and touches no machine.

        **This endpoint's inertness is load-bearing.** It accepts multipart, which is the
        one state-changing request shape a cross-site HTML form can still produce, so it is
        exempted from the hub's content-type CSRF rule by name (app.CSRF_UPLOAD_ENDPOINTS).
        That exemption is only safe because nothing happens here: the bytes go to a spool
        file, an id comes back, and the JSON call that aims them at a folder -- which IS
        covered by the rule -- is `/files/push` below. Adding any machine-facing effect here
        would quietly undo that.
        """
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "no file was uploaded"}), 400
        # secure_filename first, so a browser sending a path (some do, for a folder drop)
        # cannot get a separator this far; validate_name then applies the Windows rules,
        # because this name becomes a filename on the target machine.
        try:
            name = files.validate_name(secure_filename(upload.filename))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        machine_name = str(machine).strip()
        # The destination is not known yet and is deliberately not taken from this request:
        # /files/push supplies it, and its value there is what gets validated and audited.
        # A placeholder root keeps the row's NOT NULL contract honest until then.
        transfer_id = files.create_transfer(
            db_path, machine_name, files.PUSH, "C:\\", name,
            kind=files.KIND_FILE, issued_by=_current_email(),
            spool=None, status=files.PENDING)
        spool = files.new_spool_name(transfer_id)
        target = files.spool_path(spool_dir, spool)

        # Streamed to disk by werkzeug already; this just moves it into the spool and
        # measures it. The size check is AFTER the write rather than from Content-Length,
        # because a Content-Length is a claim and this is the fact.
        try:
            upload.save(target)
            size = os.path.getsize(target)
        except OSError as e:
            files.fail_transfer(db_path, transfer_id, str(e))
            return jsonify({"error": "the hub could not store that file"}), 500
        if size > files.MAX_TRANSFER_BYTES:
            files.discard_spool(spool_dir, spool)
            files.fail_transfer(db_path, transfer_id, "file is too large")
            return jsonify({"error": "that file is larger than this hub will carry"}), 413

        with files.get_conn(db_path) as conn:
            conn.execute("UPDATE file_transfers SET spool = ?, size_bytes = ? WHERE id = ?",
                         (spool, size, transfer_id))
        return jsonify({"transfer_id": transfer_id, "name": name, "size_bytes": size}), 201

    @bp.route("/api/machines/<machine>/files/push", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_push_file(machine):
        """Aim a spooled upload at a folder on the machine and tell it to come and get it.

        The half of the upload that carries meaning, and the half the CSRF rule covers --
        see the note on /files/upload above.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        machine_name = str(machine).strip()
        transfer = files.get_transfer(db_path, data.get("transfer_id"), machine=machine_name)
        if transfer is None or transfer["direction"] != files.PUSH:
            return jsonify({"error": "unknown upload"}), 404
        if transfer["status"] != files.PENDING or not transfer["spool"]:
            return jsonify({"error": "that upload has already been sent"}), 409

        try:
            # The name defaults to what was uploaded, and may be overridden -- "put this
            # there, but call it what it needs to be called" is one action to an operator,
            # and making it two would mean a rename command against a file that has not
            # arrived yet.
            name = files.validate_name(data.get("name") or transfer["name"])
            destination = files.validate_path(data.get("destination"))
            target = files.join_path(destination, name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if not files.arm_push(db_path, transfer["id"], destination, name):
            return jsonify({"error": "that upload has already been sent"}), 409

        try:
            command_id = _queue(machine_name, "push_file", {
                "transfer_id": transfer["id"],
                "destination": destination,
                "name": name,
                "path": target,
                "size_bytes": transfer["size_bytes"],
                "url": _agent_url(f"transfer/{transfer['id']}/content"),
                "overwrite": bool(data.get("overwrite")),
            })
        except ValueError as e:
            files.fail_transfer(db_path, transfer["id"], str(e))
            return jsonify({"error": str(e)}), 400
        return jsonify({"transfer_id": transfer["id"], "command_id": command_id,
                        "path": target,
                        "poll_interval": files.POLL_INTERVAL_SECONDS}), 201

    # ================================================================
    # Agent-facing
    # ================================================================
    @bp.route("/api/agent/files/listing/<request_id>", methods=["POST"])
    def agent_report_listing(request_id):
        """One folder's contents, reported by the machine that was asked.

        Reported here rather than as the command's output for the same reason a restore plan
        is fetched rather than carried: a listing of two thousand entries is ~200 KB, and the
        command channel's output is a terminal transcript that gets truncated at 16,000
        characters and written into the audit-adjacent record. The command still completes
        normally, with a one-line summary -- so "the machine answered" and "the answer is
        stored" stay separately visible.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        data = request.get_json(silent=True) or {}
        error = data.get("error")
        if error:
            if not files.fail_listing(db_path, request_id, machine, error):
                return jsonify({"error": "unknown listing request"}), 404
            return jsonify({"status": "recorded"}), 200
        if not files.record_listing(db_path, request_id, machine, data):
            return jsonify({"error": "unknown listing request"}), 404
        return jsonify({"status": "stored"}), 200

    @bp.route("/api/agent/files/transfer/<transfer_id>", methods=["PUT", "POST"])
    def agent_upload_transfer(transfer_id):
        """The bytes of one `fetch_file`, streamed from the machine into the hub's spool.

        Streamed straight to disk rather than buffered: this is capped at two gigabytes and
        reading one into memory would take the hub down. A Content-Length is required for
        the same reason backups' upload requires one -- a chunked body would have to be
        buffered here to discover its size, which is the thing this route exists to avoid.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        transfer = files.get_transfer(db_path, transfer_id, machine=machine)
        if transfer is None or transfer["direction"] != files.PULL:
            return jsonify({"error": "unknown transfer"}), 404
        if transfer["status"] != files.PENDING:
            return jsonify({"error": "that transfer is already finished"}), 409

        length = request.content_length
        if not length:
            return jsonify({"error": "a Content-Length is required"}), 411
        if length > files.MAX_TRANSFER_BYTES:
            files.fail_transfer(db_path, transfer["id"], "file is too large")
            return jsonify({"error": "that file is larger than this hub will carry"}), 413

        spool = files.new_spool_name(transfer["id"])
        target = files.spool_path(spool_dir, spool)
        written = 0
        try:
            with open(target, "wb") as handle:
                while True:
                    chunk = request.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    # The declared length is a claim; this is the enforcement. A body that
                    # keeps coming past its own Content-Length is either a broken client or
                    # an attempt to fill the hub's disk, and both stop here.
                    if written > length or written > files.MAX_TRANSFER_BYTES:
                        raise ValueError("body is longer than its Content-Length")
                    handle.write(chunk)
        except (OSError, ValueError) as e:
            files.discard_spool(spool_dir, spool)
            files.fail_transfer(db_path, transfer["id"], str(e))
            return jsonify({"error": "the hub could not store those bytes"}), 400

        if not files.mark_transfer_ready(db_path, transfer["id"], spool, written):
            # Lost a race with an expiry or a second PUT. The row is the authority, so the
            # bytes go rather than sitting on disk with nothing pointing at them.
            files.discard_spool(spool_dir, spool)
            return jsonify({"error": "that transfer is already finished"}), 409
        return jsonify({"status": "stored", "size_bytes": written}), 200

    @bp.route("/api/agent/files/transfer/<transfer_id>/content", methods=["GET"])
    def agent_download_transfer(transfer_id):
        """The bytes of one `push_file`, collected by the machine they are destined for."""
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        transfer = files.get_transfer(db_path, transfer_id, machine=machine)
        if transfer is None or transfer["direction"] != files.PUSH:
            return jsonify({"error": "unknown transfer"}), 404
        if transfer["status"] != files.READY or not transfer["spool"]:
            return jsonify({"error": "that upload is not ready"}), 409
        try:
            path = files.spool_path(spool_dir, transfer["spool"])
        except ValueError:
            return jsonify({"error": "that upload is no longer available"}), 410
        if not os.path.isfile(path):
            return jsonify({"error": "that upload has expired"}), 410
        return send_file(path, mimetype="application/octet-stream")

    @bp.route("/api/agent/files/transfer/<transfer_id>/result", methods=["POST"])
    def agent_transfer_result(transfer_id):
        """The machine's verdict on a transfer it was asked to make.

        Only ever used to report a FAILURE, and only for the directions where the hub cannot
        see one for itself: a `fetch_file` that could not read the file never PUTs anything,
        and a `push_file` that could not write it downloads the bytes and then fails. Without
        this the console would poll a pending transfer until it expired, which reads as a
        machine that is merely slow rather than one that has already answered.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        transfer = files.get_transfer(db_path, transfer_id, machine=machine)
        if transfer is None:
            return jsonify({"error": "unknown transfer"}), 404
        data = request.get_json(silent=True) or {}
        if data.get("error"):
            files.fail_transfer(db_path, transfer["id"], data.get("error"))
            # A failed push has bytes on the hub's disk that nothing will ever collect.
            if transfer["direction"] == files.PUSH:
                files.discard_spool(spool_dir, transfer["spool"])
            return jsonify({"status": "recorded"}), 200
        if transfer["direction"] == files.PUSH:
            # Delivered. The spool has served its purpose and the row stays, so the console
            # can still say what was sent where until it expires.
            files.discard_spool(spool_dir, transfer["spool"])
            with files.get_conn(db_path) as conn:
                conn.execute("UPDATE file_transfers SET spool = NULL WHERE id = ?",
                             (transfer["id"],))
        return jsonify({"status": "recorded"}), 200

    return bp
