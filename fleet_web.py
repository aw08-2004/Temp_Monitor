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
    app.py, AND behind the permission-group layer via the `access` object.
    Commands carry no signature (see fleet.py's module docstring), so that pair is
    the entire authorization for running code as SYSTEM: the `issue_commands`
    capability plus the target machine being in the operator's scope. Reads are
    gated too, on `view` -- otherwise an operator could watch the streamed output of
    a command run on a machine outside their scope, which is exactly what the
    terminal scrollback endpoints below serve.

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
import permissions
import settings


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


def create_fleet_blueprint(db_path, enrollment_secret, login_required, access):
    """Build the fleet Blueprint. `login_required` (app.py's session gate) and `access`
    (the permission-group layer) are both passed in, to avoid a circular import and to
    keep one source of truth for each."""
    bp = Blueprint("fleet", __name__)
    can_view = access.require(permissions.VIEW)

    def scoped_command(view):
        """Gate a /api/fleet/commands/<id> route on the caller being able to see the
        machine that command belongs to.

        The machine isn't in the URL -- only the command id is -- so this resolves the
        command first and checks its machine. An unknown id and an out-of-scope id
        both answer 404: distinguishing them would turn this into an oracle for which
        command ids exist on machines the caller cannot see.
        """
        @functools.wraps(view)
        def wrapped(command_id, *args, **kwargs):
            command = fleet.get_command(db_path, command_id)
            if command is None or not access.in_scope(command.get("machine")):
                return jsonify({"error": "unknown command"}), 404
            return view(command_id, *args, **kwargs)
        return wrapped

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
        """Liveness ping, and the hub -> agent configuration channel.

        Config rides here rather than on /api/report deliberately. /api/report is
        unauthenticated by design (it is the open telemetry ingress), so putting
        per-machine settings in its response would hand anyone who can reach the hub and
        guess a hostname a configuration oracle. This endpoint is already bearer
        authenticated, already per-machine, and already polled every ~10s.

        The agent sends the config_version it currently holds and the hub replies with
        config only when that differs, so the steady-state heartbeat stays two fields.
        """
        # authenticate_agent already refreshed last_seen.
        data = request.get_json(silent=True) or {}
        payload = {"status": "ok", "machine": machine}
        current_version = settings.agent_config_version(db_path)
        if data.get("config_version") != current_version:
            payload["config"] = settings.agent_config(db_path)
            payload["config_version"] = current_version
        return jsonify(payload), 200

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
        cwd = data.get("cwd")
        try:
            fleet.complete_command(db_path, command_id, agent_id, success, output, cwd)
        except KeyError:
            return jsonify({"error": "unknown command"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        return jsonify({"status": "recorded"}), 200

    # ---------------- Console-facing ----------------
    @bp.route("/api/fleet/status", methods=["GET"])
    @login_required
    @can_view
    def fleet_status():
        # fleet.py stays settings-free and takes the window as an argument; this HTTP
        # layer is where the operator's configured value gets injected.
        return jsonify(access.filter_rows(fleet.list_agent_status(
            db_path,
            offline_after=settings.get_int(db_path, "fleet.offline_after_seconds"),
        ))), 200

    @bp.route("/api/fleet/commands", methods=["GET"])
    @login_required
    @can_view
    def fleet_list_commands():
        machine = (request.args.get("machine") or "").strip() or None
        if machine and not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        return jsonify(access.filter_rows(fleet.list_commands(db_path, machine))), 200

    @bp.route("/api/fleet/commands/<command_id>", methods=["GET"])
    @login_required
    @can_view
    @scoped_command
    def fleet_get_command(command_id):
        command = fleet.get_command(db_path, command_id)
        if command is None:
            return jsonify({"error": "unknown command"}), 404
        return jsonify(command), 200

    @bp.route("/api/fleet/commands/<command_id>/output", methods=["GET"])
    @login_required
    @can_view
    @scoped_command
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

    def _current_email():
        """The signed-in operator. ALWAYS the source of ownership/attribution -- never
        take an email from the request body, or one operator could write rows as
        another and the audit trail would be fiction."""
        return (session.get("user") or {}).get("email", "unknown")

    # Favorites are reusable command templates owned by an operator, not machine
    # state, so they are scoped by ownership (and the `shared` flag) rather than by
    # machine. `view` is the floor for touching the fleet console at all; actually
    # RUNNING what a favorite contains goes through the gated issue endpoint below.
    @bp.route("/api/fleet/favorites", methods=["GET"])
    @login_required
    @can_view
    def fleet_list_favorites():
        return jsonify(fleet.list_favorites(db_path, _current_email())), 200

    @bp.route("/api/fleet/favorites", methods=["POST"])
    @login_required
    @can_view
    def fleet_create_favorite():
        data = request.get_json(silent=True) or {}
        try:
            favorite_id = fleet.create_favorite(
                db_path,
                email=_current_email(),
                name=data.get("name"),
                command_type=data.get("type"),
                params=data.get("params") or {},
                shared=bool(data.get("shared")),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"favorite_id": favorite_id}), 201

    @bp.route("/api/fleet/favorites/<favorite_id>", methods=["PUT"])
    @login_required
    @can_view
    def fleet_update_favorite(favorite_id):
        data = request.get_json(silent=True) or {}
        try:
            fleet.update_favorite(
                db_path, favorite_id, _current_email(),
                name=data.get("name"),
                command_type=data.get("type"),
                params=data.get("params"),
                shared=data.get("shared"),
            )
        except KeyError:
            return jsonify({"error": "unknown favorite"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"status": "updated"}), 200

    @bp.route("/api/fleet/favorites/<favorite_id>", methods=["DELETE"])
    @login_required
    @can_view
    def fleet_delete_favorite(favorite_id):
        try:
            fleet.delete_favorite(db_path, favorite_id, _current_email())
        except KeyError:
            return jsonify({"error": "unknown favorite"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        return jsonify({"status": "deleted"}), 200

    @bp.route("/api/fleet/commands", methods=["POST"])
    @login_required
    @access.require(permissions.ISSUE_COMMANDS)
    def fleet_issue_command():
        data = request.get_json(silent=True) or {}
        issued_by = _current_email()
        # The sharp end of the whole model: this queues code to run as SYSTEM. The
        # target arrives in the body rather than the URL, so the scope check is inline
        # rather than decorated -- and it runs BEFORE fleet.create_command, so nothing
        # is ever queued for a machine the caller cannot reach.
        if not access.in_scope(data.get("machine")):
            return jsonify({"error": "You do not have access to that machine."}), 403
        # Scheduler-owned types are refused here even though create_command would accept
        # them. They are gated on a DIFFERENT capability (deploy_packages), and a
        # hand-rolled one would carry a deployment id nothing reconciles -- so the deploy
        # would look queued in the console forever. Issue those through /api/packages.
        if data.get("type") in fleet.SCHEDULED_COMMANDS:
            return jsonify({"error": "Package deployments are issued from the Packages "
                                     "page, not the command channel."}), 400
        try:
            command_id = fleet.create_command(
                db_path,
                machine=data.get("machine"),
                command_type=data.get("type"),
                params=data.get("params") or {},
                issued_by=issued_by,
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"command_id": command_id}), 201

    return bp
