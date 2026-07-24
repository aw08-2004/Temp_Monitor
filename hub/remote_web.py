"""Flask HTTP surface for remote view/control (roadmap #2) -- a thin, security-conscious layer
over remote.py, registered as a Blueprint from app.py.

Two audiences, two auth schemes, exactly like fleet_web.py:

  * Agent-facing (/api/agent/remote/*): authenticated by the per-agent bearer token. An agent
    may only signal on a session whose machine it owns -- an enrolled agent for PC-2 cannot
    read or write PC-9's session, which would otherwise let it hijack another machine's stream.

  * Console-facing (/api/remote/*): gated behind the Google sign-in (login_required) AND the
    permission layer -- the `remote_control` capability plus the target machine being in the
    operator's scope. Starting a session runs a SYSTEM helper on the target and exposes its
    screen, so it is gated at least as tightly as issuing a command; every start/stop is
    audited (in remote.py).

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json -- not
CORS-safelisted, so a cross-origin fetch preflights and fails and an HTML form cannot produce
it. Do not add force=True and do not accept a form-encoded fallback.
"""
import functools
import os

from flask import Blueprint, jsonify, request, session

import fleet
import permissions
import remote
import settings

# The .env variable holding the TURN shared secret. Read from the environment (load_dotenv ran
# at hub startup), never from the settings table -- secrets are structurally barred from there.
TURN_SECRET_ENV = "REMOTE_TURN_SECRET"


def _bearer_agent(db_path):
    """Resolve (agent_id, machine) from the Authorization header, or (None, None). Same scheme
    as fleet_web: '<agent_id>:<token>', only the token's hash is stored server-side."""
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


def create_remote_blueprint(db_path, login_required, access):
    bp = Blueprint("remote", __name__)
    can_view = access.require(permissions.VIEW)
    can_remote = access.require(permissions.REMOTE_CONTROL)

    def _current_email():
        return (session.get("user") or {}).get("email", "unknown")

    def _ice_servers(session_id):
        """Assemble the ICE server list for a session from settings + the .env TURN secret."""
        return remote.ice_servers(
            session_id,
            stun_urls=settings.get_list(db_path, "remote.stun_urls"),
            turn_urls=settings.get_list(db_path, "remote.turn_urls"),
            turn_secret=os.environ.get(TURN_SECRET_ENV, ""),
            turn_ttl=settings.get_int(db_path, "remote.turn_ttl_seconds"),
        )

    # ---------------- Agent-facing (bearer token) ----------------
    def agent_auth(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            agent_id, machine = _bearer_agent(db_path)
            if agent_id is None:
                return jsonify({"error": "agent authentication required"}), 401
            return view(agent_id, machine, *args, **kwargs)
        return wrapped

    def _agent_session_or_404(session_id, machine):
        """The session, but only if it belongs to the calling agent's machine. Unknown id and
        another machine's id both answer 404 -- an agent must not be able to probe for sessions
        it doesn't own."""
        sess = remote.get_session(db_path, session_id)
        if sess is None or sess["machine"] != machine:
            return None
        return sess

    @bp.route("/api/agent/remote/<session_id>/signal", methods=["POST"])
    @agent_auth
    def agent_signal(agent_id, machine, session_id):
        if _agent_session_or_404(session_id, machine) is None:
            return jsonify({"error": "unknown session"}), 404
        data = request.get_json(silent=True) or {}
        kind = data.get("kind")
        # The agent's helper has come up and produced its offer: advance the session so the
        # console UI can show "connecting" rather than a stuck "pending".
        if kind == "offer":
            remote.mark_status(db_path, session_id, remote.STATUS_CONNECTING)
        try:
            seq = remote.add_signal(db_path, session_id, remote.SENDER_AGENT, kind,
                                    data.get("payload"))
        except KeyError:
            return jsonify({"error": "unknown session"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"seq": seq}), 200

    @bp.route("/api/agent/remote/<session_id>/ended", methods=["POST"])
    @agent_auth
    def agent_ended(agent_id, machine, session_id):
        """The agent reports its side finished (consent denied, capture failed, or clean
        teardown), so the session ends now instead of waiting out the TTL sweep."""
        if _agent_session_or_404(session_id, machine) is None:
            return jsonify({"error": "unknown session"}), 404
        data = request.get_json(silent=True) or {}
        reason = str(data.get("reason") or "agent ended")[:200]
        remote.end_session(db_path, session_id, reason, actor=machine)
        return jsonify({"status": "ended"}), 200

    @bp.route("/api/agent/remote/<session_id>/poll", methods=["GET"])
    @agent_auth
    def agent_poll(agent_id, machine, session_id):
        sess = _agent_session_or_404(session_id, machine)
        if sess is None:
            return jsonify({"error": "unknown session"}), 404
        try:
            after_seq = int(request.args.get("after_seq", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "after_seq must be an integer"}), 400
        result = remote.get_signals(db_path, session_id, remote.SENDER_AGENT, after_seq)
        result["status"] = sess["status"]
        return jsonify(result), 200

    # ---------------- Console-facing (session + remote_control + scope) ----------------
    def scoped_session(view):
        """Resolve a /api/remote/session/<id> route's session and confirm the caller can reach
        its machine. Unknown id and out-of-scope id both answer 404, so this is not an oracle
        for which session ids exist on machines the caller cannot see."""
        @functools.wraps(view)
        def wrapped(session_id, *args, **kwargs):
            sess = remote.get_session(db_path, session_id)
            if sess is None or not access.in_scope(sess["machine"]):
                return jsonify({"error": "unknown session"}), 404
            return view(session_id, sess, *args, **kwargs)
        return wrapped

    @bp.route("/api/remote/<machine>/start", methods=["POST"])
    @login_required
    @can_remote
    def start_session(machine):
        if not settings.get_bool(db_path, "remote.enabled"):
            return jsonify({"error": "Remote control is disabled in Settings."}), 403
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403

        data = request.get_json(silent=True) or {}
        try:
            monitor = max(0, int(data.get("monitor", 0)))
        except (TypeError, ValueError):
            return jsonify({"error": "monitor must be an integer"}), 400

        consent_mode = settings.get(db_path, "remote.consent_mode") or "unattended"
        session_id = remote.create_session(
            db_path, machine, _current_email(), consent_mode,
            ttl_seconds=settings.get_int(db_path, "remote.session_ttl_seconds"),
        )
        ice = _ice_servers(session_id)

        # Queue the agent's start command. Its params are a one-shot snapshot (session id +
        # freshly minted TURN creds), which is exactly why start_remote_session is not
        # favoritable (see fleet.REMOTE_CONTROL_COMMANDS).
        try:
            fleet.create_command(
                db_path, machine=machine, command_type="start_remote_session",
                params={"session_id": session_id, "monitor": monitor,
                        "consent_mode": consent_mode, "ice_servers": ice},
                issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            remote.end_session(db_path, session_id, "failed to queue start command",
                               actor=_current_email())
            return jsonify({"error": str(e)}), 400

        return jsonify({"session_id": session_id, "ice_servers": ice,
                        "consent_mode": consent_mode}), 201

    @bp.route("/api/remote/session/<session_id>/signal", methods=["POST"])
    @login_required
    @can_remote
    @scoped_session
    def console_signal(session_id, sess):
        data = request.get_json(silent=True) or {}
        try:
            seq = remote.add_signal(db_path, session_id, remote.SENDER_CONSOLE,
                                    data.get("kind"), data.get("payload"))
        except KeyError:
            return jsonify({"error": "unknown session"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"seq": seq}), 200

    @bp.route("/api/remote/session/<session_id>/poll", methods=["GET"])
    @login_required
    @can_remote
    @scoped_session
    def console_poll(session_id, sess):
        try:
            after_seq = int(request.args.get("after_seq", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "after_seq must be an integer"}), 400
        result = remote.get_signals(db_path, session_id, remote.SENDER_CONSOLE, after_seq)
        result["status"] = remote.get_session(db_path, session_id)["status"]
        return jsonify(result), 200

    @bp.route("/api/remote/session/<session_id>/stop", methods=["POST"])
    @login_required
    @can_remote
    @scoped_session
    def stop_session(session_id, sess):
        remote.end_session(db_path, session_id, "operator stopped", actor=_current_email())
        return jsonify({"status": "ended"}), 200

    @bp.route("/api/remote/sessions", methods=["GET"])
    @login_required
    @can_view
    def list_sessions():
        """Active sessions for a machine, so the machine page can show 'currently being
        viewed'. Gated on view + scope like the other read endpoints."""
        machine = (request.args.get("machine") or "").strip() or None
        if machine and not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        return jsonify(remote.list_sessions(db_path, machine, active_only=True)), 200

    return bp
