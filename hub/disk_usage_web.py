"""Flask HTTP surface for disk usage history (roadmap #27) -- a thin layer over disk_usage.py,
registered as a Blueprint from app.py.

**Every console route is `issue_commands` plus machine scope, the file browser's gate, and not
`view`.** The volume totals alone would be harmless at `view`; they are inventory, and the live
storage cards already show them there. But the same answers carry folder paths -- "C:\\Users\\
<name>\\Documents\\Applications 2026" is the name of somebody's documents -- and files_web.py
already settled what reading folder names on a machine costs: exactly the capability that can
browse them. One gate for the whole surface keeps the two from drifting apart. A totals-only
view for `view` users is a follow-up recorded in ROADMAP #27, not something to sneak in here.

**The agent half sits behind agent bearer auth** and records for the CALLING machine only:
`POST /api/agent/disk-usage` (the daily summary) and `POST /api/agent/disk-usage/history` (the
full-depth gzip delta, see disk_history.py). The machine name comes from the token, never from
the body or the query, so PC-3 cannot file a report about PC-4's disk.

**"Scan now" queues a command and returns.** Like every other console call that reaches a
machine, nothing here waits for it; the console watches the scan time change.
"""
from flask import Blueprint, jsonify, request

import auth_helpers
import disk_history
import disk_usage
import fleet
import permissions
import permissions_web
import refusals
import settings

#: The command the "Scan now" button queues. Registered in fleet.DISK_USAGE_COMMANDS.
COMMAND_TYPE = "scan_disk_usage"


def create_disk_usage_blueprint(db_path, history_root, login_required, access):
    """Build the disk-usage Blueprint. `history_root` is where disk_history keeps its
    per-machine files -- passed in so the tests can point it at a temp directory."""
    bp = Blueprint("disk_usage", __name__)

    def _volume():
        return disk_usage.clean_volume(request.args.get("volume"))

    def _days():
        keep = settings.get_int(db_path, "data.disk_usage_keep_days")
        try:
            return max(1, min(int(request.args.get("days", keep)), keep))
        except (TypeError, ValueError):
            return keep

    @bp.route("/api/disk-usage/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_summary(machine):
        """Every known volume with its latest point, forecast and last scan."""
        return jsonify(disk_usage.get_summary(db_path, str(machine).strip())), 200

    @bp.route("/api/disk-usage/machines/<machine>/history", methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_history(machine):
        """One volume's daily points plus its forecast."""
        volume = _volume()
        if volume is None:
            return jsonify({"error": "volume must be a drive letter, like C:"}), 400
        name = str(machine).strip()
        payload = disk_usage.get_history(db_path, name, volume, days=_days())
        # The days a past state can be shown for: the compare picker's list.
        payload["history_days"] = disk_history.scan_days(history_root, name, volume)
        return jsonify(payload), 200

    @bp.route("/api/disk-usage/machines/<machine>/changes", methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_changes(machine):
        """What changed on one volume between two scans; `day` picks which, newest by default."""
        volume = _volume()
        if volume is None:
            return jsonify({"error": "volume must be a drive letter, like C:"}), 400
        name = str(machine).strip()
        payload = disk_usage.get_changes(db_path, name, volume, day=request.args.get("day"))
        # The same day's file-level changes, from the full-depth history. Keyed by the day the
        # folder list is for, so the two tables on screen always describe the same scan.
        payload["file_changes"] = disk_history.file_changes(
            history_root, name, volume, day=disk_history.parse_day(payload["day"]))
        return jsonify(payload), 200

    @bp.route("/api/disk-usage/machines/<machine>/path-history", methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_path_history(machine):
        """Every recorded size of one folder or file, at any depth, as change points."""
        volume = _volume()
        if volume is None:
            return jsonify({"error": "volume must be a drive letter, like C:"}), 400
        return jsonify(disk_history.series(history_root, str(machine).strip(), volume,
                                           request.args.get("path") or volume)), 200

    @bp.route("/api/disk-usage/machines/<machine>/browse", methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_browse(machine):
        """One folder's contents and sizes as they were on a past day."""
        volume = _volume()
        if volume is None:
            return jsonify({"error": "volume must be a drive letter, like C:"}), 400
        day = disk_history.parse_day(request.args.get("day"))
        if day is None:
            return jsonify({"error": "day must be a date, like 2026-10-05"}), 400
        return jsonify(disk_history.browse(history_root, str(machine).strip(), volume,
                                           request.args.get("path") or volume, day)), 200

    @bp.route("/api/disk-usage/machines/<machine>/scan", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_scan(machine):
        """Ask the machine to scan at its next inventory pass instead of tomorrow.

        JSON only, restated here as files_web restates it: this is a state-changing POST, and
        the content-type rule is what keeps a cross-site form from making one.
        """
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        try:
            command_id = fleet.create_command(
                db_path,
                machine=str(machine).strip(),
                command_type=COMMAND_TYPE,
                params={},
                issued_by=permissions_web.current_actor(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            return refusals.refuse(e)
        return jsonify({"command_id": command_id}), 201

    # ================================================================
    # Agent-facing
    # ================================================================
    @bp.route("/api/agent/disk-usage", methods=["POST"])
    def agent_report():
        """One daily disk-usage report from the machine holding this agent's token."""
        agent_id, machine = auth_helpers.bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("volumes"), list):
            return jsonify({"error": "expected a disk usage report"}), 400
        stored = disk_usage.record_scan(db_path, machine, data)
        return jsonify({"status": "stored", "volumes": stored}), 200

    @bp.route("/api/agent/disk-usage/history", methods=["POST"])
    def agent_history():
        """One volume's full-depth delta (or full tree), gzip, streamed to the spool.

        409 means "I do not hold the scan this delta was taken against -- send the whole tree".
        That is the repair path for a lost upload, not an error, and the agent treats it so.
        """
        agent_id, machine = auth_helpers.bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        volume = disk_usage.clean_volume(request.args.get("volume"))
        try:
            scanned_at = int(request.args.get("scanned_at", ""))
            base = request.args.get("base")
            base = int(base) if base not in (None, "") else None
        except ValueError:
            return jsonify({"error": "scanned_at and base must be timestamps"}), 400
        if volume is None:
            return jsonify({"error": "volume must be a drive letter, like C:"}), 400
        full = request.args.get("full") in ("1", "true")
        try:
            outcome = disk_history.accept(history_root, machine, volume, scanned_at, base, full,
                                          request.stream, request.content_length)
        except (OSError, ValueError):
            # A fixed sentence, never the exception's text: what went wrong writing the hub's
            # own disk is not the agent's business (CodeQL, py/stack-trace-exposure).
            return jsonify({"error": "the hub could not store that upload"}), 400
        if outcome == "need_full":
            return jsonify({"error": "base scan not held; send the full tree",
                            "need_full": True}), 409
        if outcome == "no_length":
            return jsonify({"error": "a Content-Length is required"}), 411
        if outcome == "too_large":
            return jsonify({"error": "that upload is larger than this hub accepts"}), 413
        if outcome == "busy":
            # The agent treats anything but 200 and 409 as "try again later", which it does
            # five minutes on. Nothing is lost: its upload file stays until the hub takes it.
            return jsonify({"error": "uploads for this volume are still being applied"}), 429
        return jsonify({"status": outcome}), 200

    return bp
