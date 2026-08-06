"""Flask HTTP surface for the machine Processes card -- a thin layer over processes.py,
registered as a Blueprint from app.py.

**Two gates, drawn where bios_web.py and wake_web.py draw theirs:**

  * **Reading** the process list is `view` + machine scope. What is running on a PC is
    inventory in the same sense its disks and its sensors are, and an operator who can open
    the machine page can already read every temperature, every volume and the whole LHM
    sensor tree. Making "what is pinning this CPU at 100%?" cost an admin would make the
    question cost more than walking to the desk -- which is the same reasoning that keeps
    the Firmware and Network tabs readable.
  * **Ending or restarting** a process is `issue_commands` + machine scope. No new
    capability: this is strictly less dangerous than the `restart` and `shutdown` that gate
    already covers, and than the SYSTEM shell it also covers.

**The read endpoint has a side effect, and that is the design.** Polling it renews the
watch that tells the machine to keep sampling (processes.note_watch). Nothing else
subscribes and nothing unsubscribes: a browser tab that is closed, crashed, or suspended by
a sleeping laptop never sends a farewell, so the watch has to lapse on its own. See
processes.py for why sampling is not simply always on.

**Nothing here waits for the machine.** Both actions queue a command and answer with its id;
the console polls `/api/fleet/commands/<id>` for the outcome, exactly as the Terminal and
Firmware tabs do. An endpoint that blocked on an agent's next poll would hold a hub worker
thread for up to ten seconds per click.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, request

import fleet
import permissions
import permissions_web
import processes
import settings


def create_processes_blueprint(db_path, login_required, access):
    """Build the processes Blueprint. `login_required` (app.py's session gate) and `access`
    (the permission-group layer) are passed in for the same reason every other blueprint
    takes them: one source of truth for each, and no circular import."""
    bp = Blueprint("processes", __name__)

    def _current_email():
        return permissions_web.current_actor()

    def _require_json():
        """Refuse anything that is not application/json, like wake_web does.

        app.py's login_required already applies this rule, but it is restated at the route
        because that is the control which stops a cross-site form POST from ending processes
        on a signed-in operator's fleet -- and a control that lives only in a decorator
        somebody else owns is one refactor away from being absent."""
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _queue(machine, command_type, params):
        """Queue one process command and answer with its id. Shared by both verbs because
        the only thing that differs between them is the params, and the failure handling --
        which is the part worth having exactly once -- is identical."""
        try:
            command_id = fleet.create_command(
                db_path,
                machine=machine,
                command_type=command_type,
                params=params,
                issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"command_id": command_id}), 201

    @bp.route("/api/machines/<machine>/processes", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_processes(machine):
        """This machine's last process report, and a renewed watch.

        `poll_interval` and `watch_ttl` are served rather than hardcoded in the browser so
        the cadence stays one decision: the console's poll rate, the watch window and the
        agent's sampling interval are a single design and drifting them apart would show up
        as a card that goes blank for a few seconds every minute.
        """
        machine_name = str(machine).strip()
        processes.note_watch(db_path, machine_name, watcher=_current_email())
        payload = processes.get_snapshot(db_path, machine_name)
        payload["poll_interval"] = processes.POLL_INTERVAL_SECONDS
        payload["watch_ttl"] = processes.WATCH_TTL_SECONDS
        return jsonify(payload), 200

    @bp.route("/api/machines/<machine>/processes/kill", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_kill_process(machine):
        """End one process, or every instance of one name the operator selected.

        `pids` + `name` travel together on purpose -- see processes.validate_kill. The agent
        re-checks the pairing against the live process before it kills anything, so a
        snapshot that went stale between render and click ends nothing rather than ending
        whatever inherited the id.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        try:
            params = processes.validate_kill(
                data.get("name"), data.get("pids"), tree=data.get("tree"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return _queue(str(machine).strip(), "kill_process", params)

    @bp.route("/api/machines/<machine>/processes/restart", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def machine_restart_process(machine):
        """End one process and start it again where it was.

        What that means is decided ON THE MACHINE, because only the machine knows: a process
        hosting a Windows service is restarted as that service, and anything else is relaunched
        from its own image in the Windows session it was running in, as the user who was
        running it. See the agent's RestartProcessExecutor.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        try:
            params = processes.validate_restart(data.get("name"), data.get("pid"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return _queue(str(machine).strip(), "restart_process", params)

    return bp
