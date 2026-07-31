"""Flask HTTP surface for the BIOS/firmware inventory (roadmap #9) -- a thin layer over
bios.py, registered as a Blueprint from app.py.

Console-facing only. The agent side of this feature has no endpoint of its own: the
inventory rides the existing authenticated heartbeat (see fleet_web.agent_heartbeat), which
is already per-machine, already bearer-authenticated and already polled -- adding a second
agent ingress for a payload that changes a few times a year would be one more thing to
authenticate and one more thing to get wrong.

Two gates, and the split is deliberate:

  * **Reading** the inventory is `view` + machine scope. What firmware a PC is set to is
    inventory data, exactly like its model or its disk layout, and an operator who can see a
    machine can see it. This is also why it is not behind `manage_settings`: needing an
    admin to answer "is Wake-on-LAN on?" would make the question cost more than walking to
    the desk.
  * **Forcing a re-read** is `issue_commands` + machine scope, because it queues a real
    command on a real machine. Cheap and read-only on the far end, but the perimeter for
    "make that PC do something" is the command channel's, not the viewer's.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, session

import bios
import fleet
import permissions
import settings


def create_bios_blueprint(db_path, login_required, access):
    bp = Blueprint("bios", __name__)
    can_view = access.require(permissions.VIEW)
    can_issue = access.require(permissions.ISSUE_COMMANDS)

    def _current_email():
        return (session.get("user") or {}).get("email", "unknown")

    @bp.route("/api/bios/<machine>", methods=["GET"])
    @login_required
    @can_view
    def machine_bios(machine):
        """The machine's firmware settings as of its last report.

        Answers from storage rather than from the machine: a BIOS enumeration takes seconds
        on some vendors, and a page that blocks on one would be a page nobody opens twice.
        `/refresh` is there for when the stored answer is not good enough.

        `support: null` means no agent has ever told us -- distinct from `unsupported`, which
        is a machine that has told us it has nothing to manage. The console must render those
        two differently or every fleet looks unmanageable the day before the agent release.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        return jsonify(bios.get_inventory(db_path, machine)), 200

    @bp.route("/api/bios/<machine>/refresh", methods=["POST"])
    @login_required
    @can_issue
    def refresh_bios(machine):
        """Queue a re-read. The agent's firmware inventory rides the heartbeat on a
        change-detected, self-throttled cadence -- right for the steady state, wrong for the
        operator who has just changed a setting in the BIOS by hand and wants to see it."""
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        try:
            command_id = fleet.create_command(
                db_path, machine=machine, command_type="refresh_bios_inventory",
                params={}, issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"command_id": command_id}), 202

    return bp
