"""Flask HTTP surface for the fleet command channel -- a thin, security-conscious
layer over fleet.py. Registered as a Blueprint from app.py so the ~1100-line hub
module doesn't keep growing.

Two audiences, two auth schemes:

  * Agent-facing endpoints (/api/agent/*): authenticated by the per-agent bearer
    token issued at enrollment (Authorization: Bearer <agent_id>:<token>). These
    are the only new endpoints reachable without a browser session, so they are
    deliberately narrow -- enroll, heartbeat, pull commands, post a result.

  * Console-facing endpoints (/api/fleet/*): gated behind the same Google
    sign-in as the rest of the dashboard, via the login_required passed in from
    app.py. This is where an operator issues commands and reads status/audit.
    That session gate is the ONLY authorization on the command path -- commands
    carry no signature (see fleet.py's module docstring) -- so it is also the
    perimeter for running code as SYSTEM across the fleet.

Note for anyone extending the console endpoints: reading the body with
request.get_json(silent=True) is load-bearing beyond convenience. It requires
Content-Type: application/json, which is not CORS-safelisted, so a cross-origin
fetch preflights and fails (no ACAO here) and an HTML form -- the one cross-site
POST needing no preflight -- cannot produce that content type. That is what keeps
a CSRF against a signed-in operator from becoming fleet-wide RCE. Do not add
force=True, and do not accept a form-encoded fallback. (app.py additionally pins
SameSite=Lax on the session cookie.)
"""
import functools

from flask import Blueprint, jsonify, request, session

import fleet


def _bearer_agent(db_path):
    """Resolve (agent_id, machine) from the Authorization header, or (None, None).
    Token format is '<agent_id>:<token>' so a single header carries both the
    identity and the secret; only the secret's hash is ever stored server-side."""
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


def create_fleet_blueprint(db_path, enrollment_secret, login_required):
    """Build the fleet Blueprint. `login_required` is app.py's session gate, passed
    in to avoid a circular import and to keep one source of truth for auth."""
    bp = Blueprint("fleet", __name__)

    def agent_auth(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            agent_id, machine = _bearer_agent(db_path)
            if agent_id is None:
                return jsonify({"error": "agent authentication required"}), 401
            return view(agent_id, machine, *args, **kwargs)
        return wrapped

    # ---------------- Agent-facing ----------------
    @bp.route("/api/agent/enroll", methods=["POST"])
    def agent_enroll():
        data = request.get_json(silent=True) or {}
        machine = data.get("machine")
        secret = data.get("enrollment_secret")
        try:
            agent_id, token = fleet.enroll_agent(db_path, machine, secret, enrollment_secret)
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        # Token is returned exactly once; the agent must persist it locally.
        return jsonify({"agent_id": agent_id, "token": token}), 200

    @bp.route("/api/agent/heartbeat", methods=["POST"])
    @agent_auth
    def agent_heartbeat(agent_id, machine):
        # authenticate_agent already refreshed last_seen; nothing else to do.
        return jsonify({"status": "ok", "machine": machine}), 200

    @bp.route("/api/agent/commands", methods=["GET"])
    @agent_auth
    def agent_commands(agent_id, machine):
        """Agent pulls (and thereby claims) any pending commands for its machine.
        Outbound-only: the agent polls this, no inbound port is ever opened."""
        commands = fleet.claim_commands(db_path, agent_id, machine)
        return jsonify({"commands": commands}), 200

    @bp.route("/api/agent/commands/<command_id>/output", methods=["POST"])
    @agent_auth
    def agent_command_output(agent_id, machine, command_id):
        """Streamed output from a command still running on this agent.

        The agent posts {seq, chunk} as lines arrive so the console terminal shows
        progress rather than waiting for the whole run. Idempotent per (command, seq):
        a retry of a POST that actually landed is a no-op, so the agent must reuse the
        same seq. `truncated: true` tells the agent to stop streaming this command --
        the full text still reaches command_results at completion.
        """
        data = request.get_json(silent=True) or {}
        try:
            truncated = fleet.append_command_output(
                db_path, command_id, agent_id, data.get("seq"), data.get("chunk"))
        except KeyError:
            return jsonify({"error": "unknown command"}), 404
        except PermissionError as e:
            # Includes "already completed" -- the run is over, don't reopen it.
            return jsonify({"error": str(e)}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"status": "ok", "truncated": truncated}), 200

    @bp.route("/api/agent/commands/<command_id>/result", methods=["POST"])
    @agent_auth
    def agent_command_result(agent_id, machine, command_id):
        data = request.get_json(silent=True) or {}
        success = bool(data.get("success"))
        output = data.get("output")
        try:
            fleet.complete_command(db_path, command_id, agent_id, success, output)
        except KeyError:
            return jsonify({"error": "unknown command"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        return jsonify({"status": "recorded"}), 200

    # ---------------- Console-facing ----------------
    @bp.route("/api/fleet/status", methods=["GET"])
    @login_required
    def fleet_status():
        return jsonify(fleet.list_agent_status(db_path)), 200

    @bp.route("/api/fleet/commands", methods=["GET"])
    @login_required
    def fleet_list_commands():
        machine = (request.args.get("machine") or "").strip() or None
        return jsonify(fleet.list_commands(db_path, machine)), 200

    @bp.route("/api/fleet/commands/<command_id>", methods=["GET"])
    @login_required
    def fleet_get_command(command_id):
        command = fleet.get_command(db_path, command_id)
        if command is None:
            return jsonify({"error": "unknown command"}), 404
        return jsonify(command), 200

    @bp.route("/api/fleet/commands/<command_id>/output", methods=["GET"])
    @login_required
    def fleet_get_command_output(command_id):
        """Live scrollback for the terminal. `after_seq` is the client's cursor; pass
        back the `next_seq` from the previous response to fetch only what's new.
        Status and result ride along so one poll tick is one request."""
        try:
            after_seq = int(request.args.get("after_seq", -1))
        except (TypeError, ValueError):
            return jsonify({"error": "after_seq must be an integer"}), 400
        try:
            return jsonify(fleet.get_command_output(db_path, command_id, after_seq)), 200
        except KeyError:
            return jsonify({"error": "unknown command"}), 404

    @bp.route("/api/fleet/commands", methods=["POST"])
    @login_required
    def fleet_issue_command():
        data = request.get_json(silent=True) or {}
        issued_by = (session.get("user") or {}).get("email", "unknown")
        try:
            command_id = fleet.create_command(
                db_path,
                machine=data.get("machine"),
                command_type=data.get("type"),
                params=data.get("params") or {},
                issued_by=issued_by,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"command_id": command_id}), 201

    return bp
