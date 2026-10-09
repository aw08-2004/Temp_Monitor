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
import time

from flask import Blueprint, Response, jsonify, request, session

import auth_helpers
import fleet
import permissions
import permissions_web
import recordings
import refusals
import remote
import remote_relay
import settings
# For the peer plane's gate only (roadmap #15). sharing_web does not import this module
# at load time -- its one use of remote.py is a deferred import inside a function -- so this
# direction is the one that stays acyclic.
import sharing_web

# The .env variable holding the TURN shared secret. Read from the environment (load_dotenv ran
# at hub startup), never from the settings table -- secrets are structurally barred from there.
TURN_SECRET_ENV = "REMOTE_TURN_SECRET"

# The refusal for relay traffic on a session that has ended or expired.
SESSION_NOT_ACTIVE = "session is not active"

# Backwards-compatible alias so callers using the old private name keep working.
_bearer_agent = auth_helpers.bearer_agent


def create_remote_blueprint(db_path, login_required, access, env_path=None):
    bp = Blueprint("remote", __name__)
    can_view = access.require(permissions.VIEW)
    can_remote = access.require(permissions.REMOTE_CONTROL)
    # TURN configuration is fleet-wide plumbing, not a per-machine control, so it sits behind
    # manage_settings like the rest of Settings rather than remote_control. env_path is the same
    # .env load_dotenv read at boot; without it the secret is read-only from the UI (still shown,
    # just not settable) so a misconfigured deploy degrades instead of 500ing.
    can_manage_settings = access.require(permissions.MANAGE_SETTINGS)

    def _current_email():
        return permissions_web.current_actor()

    def _ice_servers(session_id, peer_ip=None):
        """Assemble the ICE server list for ONE PEER of a session, from settings + the .env TURN
        secret.

        `peer_ip` is that peer's own source address, which is why the two sides no longer get an
        identical list: a relay URL spelled with a private IP literal is reachable from inside
        the hub's network and from nowhere else (remote.select_urls_for_peer). ProxyFix is installed
        in app.py, so request.remote_addr is the real client behind the TLS terminator.
        """
        return remote.ice_servers(
            session_id,
            stun_urls=settings.get_list(db_path, "remote.stun_urls"),
            turn_urls=settings.get_list(db_path, "remote.turn_urls"),
            turn_secret=os.environ.get(TURN_SECRET_ENV, ""),
            turn_ttl=settings.get_int(db_path, "remote.turn_ttl_seconds"),
            peer_ip=peer_ip,
        )

    def _stream_params(data):
        """Validate the viewer's stream choices into agent command params.

        Everything here arrives from a browser, so it is validated rather than clamped
        silently: an operator who typed 500 fps should be told, not quietly given 60. The
        agent clamps again on its own side -- an older hub, a replayed command, or a future
        client must not be able to hand the capture loop an fps of 0.

        `session` is the WINDOWS logon session to inject into, not the remote session id.
        "auto" (the default) means the agent picks -- which is still the right answer for a
        machine with one obvious session, and the only possible answer for an agent too old to
        report its sessions.
        """
        def _int(key, default, low, high):
            raw = data.get(key, default)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be an integer")
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            return value

        target = data.get("session", "auto")
        if target in (None, "", "auto"):
            target_session = -1
        else:
            try:
                target_session = int(target)
            except (TypeError, ValueError):
                raise ValueError("session must be an integer or 'auto'")
            # Session 0 is the non-interactive services session; it has no desktop at all.
            if target_session <= 0:
                raise ValueError("session must be a positive Windows session id, or 'auto'")

        codec = str(data.get("codec", "h264")).lower()
        if codec not in ("h264", "vp8"):
            raise ValueError("codec must be 'h264' or 'vp8'")
        encoder = str(data.get("encoder", "auto")).lower()
        if encoder not in ("auto", "hardware", "software"):
            raise ValueError("encoder must be 'auto', 'hardware' or 'software'")

        return {
            "monitor": _int("monitor", 0, 0, 15),
            "target_session": target_session,
            "fps": _int("fps", 15, 1, 60),
            "bitrate_kbps": _int("bitrate_kbps", 4000, 100, 50000),
            "scale": _int("scale", 100, 25, 100),
            "codec": codec,
            "encoder": encoder,
        }

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
            # An offer is a NEW helper, and a new helper has no badge up: the one that had it
            # was relaunched (a sign-in, a sign-out) or died. So a recording running through
            # the old one ends here -- the "it stops if the helper dies" half of roadmap #19.
            # The session's first offer finds nothing to end, since nothing records before
            # the picture arrives.
            recordings.end_for_session(db_path, session_id, recordings.END_HELPER_RESTARTED,
                                       actor=machine)
        elif kind == "recording":
            # The helper's answer to a `record` signal: whether the badge is on screen.
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            recordings.confirm_badge(db_path, str(payload.get("recording_id") or ""),
                                     machine, str(payload.get("badge") or ""))
        try:
            seq = remote.add_signal(db_path, session_id, remote.SENDER_AGENT, kind,
                                    data.get("payload"))
        except KeyError:
            return jsonify({"error": "unknown session"}), 404
        except PermissionError as e:
            return refusals.refuse(e, 409)
        except ValueError as e:
            return refusals.refuse(e)
        return jsonify({"seq": seq}), 200

    @bp.route("/api/agent/remote/<session_id>/ice", methods=["GET"])
    @agent_auth
    def agent_ice(agent_id, machine, session_id):
        """The ICE servers for THIS agent, minted now and chosen from the agent's own source
        address.

        The list in the start command was built when the console pressed Start, from the
        console's vantage -- and the two peers are frequently not on the same side of the hub's
        network. Handing the agent a relay URL spelled with the hub's LAN address when the agent
        is out on the internet costs it an allocation that can only time out; handing it only the
        public hostname when it is sitting on the hub's own LAN makes it hairpin off the router
        for a relay one switch away. Fetching from here lets the hub answer from what it can see
        rather than from what the other peer could see.

        Also re-mints the credential, so a helper the supervisor relaunched late in a long
        session does not start out holding one that has already expired.
        """
        if _agent_session_or_404(session_id, machine) is None:
            return jsonify({"error": "unknown session"}), 404
        return jsonify({"ice_servers": _ice_servers(session_id, request.remote_addr)}), 200

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
        recordings.end_for_session(db_path, session_id, recordings.END_SESSION_ENDED,
                                   actor=machine)
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

    # ---------------- Hub relay (remote_relay.py) ----------------
    # The fallback path for a session WebRTC could not connect. Same two planes as the
    # signaling above -- the agent by bearer token on its own machine's session, the console by
    # remote_control + scope -- and the relay only exists once the CONSOLE asked for it (the
    # `relay` signal in console_signal), so an agent cannot push video into a session nobody
    # switched over.
    def _relay_session_live(sess):
        """True while the session may still carry relay traffic. A session that ended or
        expired has its relay closed here as well as in remote.end_session, because the TTL
        sweep and a racing request can each get there first."""
        if sess is None:
            return False
        if sess["status"] in (remote.STATUS_PENDING, remote.STATUS_CONNECTING,
                              remote.STATUS_ACTIVE) and sess["expires_at"] > time.time():
            return True
        remote_relay.close_relay(sess["id"])
        return False

    @bp.route("/api/agent/remote/<session_id>/relay/down", methods=["POST"])
    @agent_auth
    def agent_relay_down(agent_id, machine, session_id):
        """Encoded frames and status records from the agent, in remote_relay's framing. The
        body is read raw: it is binary, and bearer auth (not a browser cookie) is what makes this
        route safe from a cross-site POST, so the JSON content-type rule does not apply."""
        sess = _agent_session_or_404(session_id, machine)
        if sess is None:
            return jsonify({"error": "unknown session"}), 404
        if not _relay_session_live(sess):
            return jsonify({"error": SESSION_NOT_ACTIVE}), 409
        body = _read_capped(remote_relay.MAX_UPLOAD_BYTES)
        if body is None:
            return jsonify({"error": "upload too large"}), 413
        try:
            records = remote_relay.parse_records(body)
        except ValueError as e:
            return refusals.refuse(e)
        if remote_relay.push_down(session_id, records) is None:
            return jsonify({"error": "relay is not open for this session"}), 409
        return jsonify({"stored": len(records)}), 200

    def _read_capped(limit):
        """The request body, or None if it is longer than `limit` -- read without ever holding
        more than `limit + 1` bytes.

        The Content-Length check alone is not a bound: a chunked POST has no Content-Length,
        and request.get_data() would buffer the whole body before parse_records ever saw it.
        This is the one route that takes a raw binary body, and the hub sets no
        MAX_CONTENT_LENGTH, so the cap has to be enforced here, on the stream (found in review
        of #115)."""
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

    @bp.route("/api/agent/remote/<session_id>/relay/up", methods=["GET"])
    @agent_auth
    def agent_relay_up(agent_id, machine, session_id):
        """Long-poll for the console's input and live-settings messages -- the relayed copy of
        what the WebRTC control channel would have carried, as the same JSON strings."""
        sess = _agent_session_or_404(session_id, machine)
        if sess is None:
            return jsonify({"error": "unknown session"}), 404
        if not _relay_session_live(sess):
            return jsonify({"messages": [], "next": 0, "state": "closed"}), 200
        messages, cursor, state = remote_relay.read_up(
            session_id, request.args.get("after", 0))
        return jsonify({"messages": messages, "next": cursor, "state": state}), 200

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
            stream = _stream_params(data)
        except ValueError as e:
            return refusals.refuse(e)

        consent_mode = settings.get(db_path, "remote.consent_mode") or "unattended"
        session_id = remote.create_session(
            db_path, machine, _current_email(), consent_mode,
            ttl_seconds=settings.get_int(db_path, "remote.session_ttl_seconds"),
        )
        ice = _ice_servers(session_id, request.remote_addr)

        # Queue the agent's start command. Its params are a one-shot snapshot (session id +
        # freshly minted TURN creds), which is exactly why start_remote_session is not
        # favoritable (see fleet.REMOTE_CONTROL_COMMANDS).
        #
        # The agent's copy is minted from the CONSOLE's vantage, which is the wrong one for it --
        # it re-fetches its own from /api/agent/remote/<id>/ice before it builds its peer. This
        # copy stays because an agent too old to know about that endpoint still needs something,
        # and because it is what makes a session start at all when the ICE fetch fails.
        try:
            fleet.create_command(
                db_path, machine=machine, command_type="start_remote_session",
                params={"session_id": session_id, "consent_mode": consent_mode,
                        "ice_servers": ice, **stream},
                issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            remote.end_session(db_path, session_id, "failed to queue start command",
                               actor=_current_email())
            return refusals.refuse(e)

        return jsonify({"session_id": session_id, "ice_servers": ice,
                        "consent_mode": consent_mode, **stream}), 201

    @bp.route("/api/remote/<machine>/inventory", methods=["GET"])
    @login_required
    @can_remote
    def machine_inventory(machine):
        """Logon sessions and display outputs, as last reported on the agent's heartbeat.

        This is what turns the session switcher from a guess into a choice, and what lets the
        machine page say "no display outputs" before an operator opens a session and finds a
        black screen. Read-only, so it answers from the last heartbeat rather than making the
        operator wait on a round trip -- /refresh is there for when that is not good enough.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        inventory = remote.get_inventory(db_path, machine)
        inventory["payload_available"] = remote.get_virtual_display_payload(db_path) is not None
        return jsonify(inventory), 200

    @bp.route("/api/remote/<machine>/inventory/refresh", methods=["POST"])
    @login_required
    @can_remote
    def refresh_inventory(machine):
        """Queue a re-report. The agent's inventory rides the heartbeat on a change-detected,
        self-throttled cadence -- right for the steady state, wrong for the moment an operator
        is staring at the picker and somebody has just signed in."""
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        try:
            command_id = fleet.create_command(
                db_path, machine=machine, command_type="refresh_remote_inventory",
                params={}, issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            return refusals.refuse(e)
        return jsonify({"command_id": command_id}), 202

    @bp.route("/api/remote/session/<session_id>/signal", methods=["POST"])
    @login_required
    @can_remote
    @scoped_session
    def console_signal(session_id, sess):
        data = request.get_json(silent=True) or {}
        relay_created = False
        if data.get("kind") == "relay":
            refused, relay_created = _open_relay(session_id, sess)
            if refused is not None:
                return refused
        try:
            seq = remote.add_signal(db_path, session_id, remote.SENDER_CONSOLE,
                                    data.get("kind"), data.get("payload"))
        except (KeyError, PermissionError, ValueError) as e:
            # A relay this request opened for a signal the agent will now never hear would sit
            # open, unused, until the idle sweep. Close it; a retry opens it again.
            if relay_created:
                remote_relay.close_relay(session_id)
            if isinstance(e, KeyError):
                return jsonify({"error": "unknown session"}), 404
            if isinstance(e, PermissionError):
                return refusals.refuse(e, 409)
            return refusals.refuse(e)
        if relay_created:
            # Audited only once the switch is real -- the signal is queued, so the agent will
            # act on it -- and only by the request that created the relay, so a console that
            # retries a timed-out request, or two retries racing, write one row between them.
            # From here on the hub sees the picture in the clear (remote_relay's docstring).
            payload = data.get("payload")
            reason = payload.get("reason") if isinstance(payload, dict) else ""
            fleet.audit(db_path, actor=_current_email(), action="remote_session_relay",
                        level=fleet.LEVEL_SECURITY, target=sess["machine"],
                        detail={"session_id": session_id, "reason": str(reason or "")[:120]})
        return jsonify({"seq": seq}), 200

    def _open_relay(session_id, sess):
        """Open the relay for a console's `relay` signal. Returns (refusal, created): a refusal
        response or None, and whether THIS call created the relay.

        Opened BEFORE the signal is queued, not after, and that order is deliberate. The other
        way round, the agent can poll the signal, switch, and upload before the relay exists --
        and an agent that is refused an upload ends the session, which would turn the fallback
        into the failure. The cost of this order, a relay open for a signal that then failed, is
        undone by the caller (it closes what it created).

        Refused outright when the fallback is switched off: the agent would otherwise drop its
        peer for a path that answers 409."""
        if not settings.get_bool(db_path, "remote.relay_fallback"):
            return (jsonify({"error": "Relaying through the hub is turned off in Settings."}),
                    403), False
        if not _relay_session_live(sess):
            return (jsonify({"error": SESSION_NOT_ACTIVE}), 409), False
        _, created = remote_relay.open_relay(session_id, sess["machine"])
        return None, created

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

    @bp.route("/api/remote/session/<session_id>/relay/down", methods=["GET"])
    @login_required
    @can_remote
    @scoped_session
    def console_relay_down(session_id, sess):
        """Long-poll the agent's frames. Binary, in remote_relay's framing; the cursor and the
        relay's state ride in headers so the body stays the frames and nothing else."""
        if not _relay_session_live(sess):
            records, cursor, state = [], 0, "closed"
        else:
            records, cursor, state = remote_relay.read_down(
                session_id, request.args.get("after", 0))
        resp = Response(remote_relay.frame_records(records),
                        mimetype="application/octet-stream")
        resp.headers["X-Relay-Next"] = str(cursor)
        resp.headers["X-Relay-State"] = state
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/api/remote/session/<session_id>/relay/up", methods=["POST"])
    @login_required
    @can_remote
    @scoped_session
    def console_relay_up(session_id, sess):
        """Input and live-settings messages for the agent. JSON body, for the CSRF reason in
        this module's docstring."""
        if not _relay_session_live(sess):
            return jsonify({"error": SESSION_NOT_ACTIVE}), 409
        data = request.get_json(silent=True) or {}
        messages = data.get("messages")
        if not isinstance(messages, list) or len(messages) > 200:
            return jsonify({"error": "messages must be a list of at most 200"}), 400
        try:
            queued = remote_relay.push_up(session_id, messages)
        except remote_relay.RelayBacklogFull:
            # The agent stopped reading. 429 rather than a silent drop; the viewer retries,
            # and a session whose agent is gone ends through the down stream's state.
            return jsonify({"error": "The machine is not keeping up with input."}), 429
        except ValueError as e:
            return refusals.refuse(e)
        if queued is None:
            return jsonify({"error": "relay is not open for this session"}), 409
        return jsonify({"queued": queued}), 200

    @bp.route("/api/remote/session/<session_id>/stop", methods=["POST"])
    @login_required
    @can_remote
    @scoped_session
    def stop_session(session_id, sess):
        remote.end_session(db_path, session_id, "operator stopped", actor=_current_email())
        recordings.end_for_session(db_path, session_id, recordings.END_SESSION_ENDED,
                                   actor=_current_email())
        return jsonify({"status": "ended"}), 200

    @bp.route("/api/remote/sessions", methods=["GET"])
    @login_required
    @can_view
    def list_sessions():
        """Active sessions for a machine, so the machine page can show 'currently being
        viewed'. Gated on view + scope like the other read endpoints.

        The no-machine form answers for the WHOLE fleet, so its rows are narrowed to the
        caller's scope before they go out -- they carry both a machine name and the email of
        the operator on it, and an HR tech must no more be able to enumerate Hospital
        hostnames here than on /api/machines (which filters for the same reason)."""
        machine = (request.args.get("machine") or "").strip() or None
        if machine and not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        sessions = remote.list_sessions(db_path, machine, active_only=True)
        return jsonify(access.filter_rows(sessions)), 200

    # ---------------- Peer hubs (roadmap #15) ----------------
    # A third plane beside the console and the agent, and it lives here rather than in
    # sharing_web.py because everything a session start needs -- the ICE list, the minted
    # TURN credentials, the stream-parameter clamps -- is in this closure. What sharing.py
    # owns is the AUTHORIZATION, and that arrives as `peer_gate`.
    #
    # Note what these routes do NOT do: they do not talk to the borrowing hub, and they do
    # not tell the agent that one exists. A peer-started session is queued on this hub's own
    # command queue to this hub's own agent, exactly like a console-started one, and the
    # signaling that follows is relayed through this hub in both directions. That is the
    # whole design -- see sharing.py's module docstring.
    #
    # Virtual display is deliberately absent from this plane. Installing it puts a
    # third-party driver into the DriverStore and its publisher into the machine's
    # certificate store, which is an expansion of what the machine trusts and is not
    # something a share hands over. A borrowed headless machine has to be prepared by its
    # own operator first.
    peer_remote = sharing_web.peer_gate(db_path, access, permissions.REMOTE_CONTROL)

    def peer_session(view):
        """Resolve <session_id> to a session on this share's machine THAT THIS PEER STARTED.

        The second half is the one that matters. Scoping only to the machine would let a
        borrowing hub poll the signaling of a session a LOCAL operator has open on it --
        that is somebody else's screen and, at setup time, somebody else's TURN credentials.
        Unknown, not-yours and wrong-machine all answer 404 alike, matching `scoped_session`
        above: distinguishing them would leak which session ids exist.
        """
        @functools.wraps(view)
        def wrapped(peer, state, session_id, *args, **kwargs):
            sess = remote.get_session(db_path, session_id)
            if (sess is None
                    or sess["machine"] != state["machine"]
                    or not sharing_web.peer_owns(sess, peer)):
                return jsonify({"error": "unknown session"}), 404
            return view(peer, state, sess, *args, **kwargs)
        return wrapped

    @bp.route("/api/peer/shares/<share_id>/remote", methods=["POST"])
    @peer_remote
    def peer_start_session(peer, state):
        """Start a session on a borrowed machine. The peer plane's copy of start_session.

        The consent mode is THIS hub's setting, not a parameter: whether the person at the
        desk is asked before their screen is shared is a policy of the fleet the machine
        belongs to, and it would be exactly the wrong thing to let the borrowing hub choose.
        """
        if not settings.get_bool(db_path, "remote.enabled"):
            return jsonify({"error": "Remote control is disabled on the owning hub."}), 403
        data = request.get_json(silent=True) or {}
        try:
            stream = _stream_params(data)
        except ValueError as e:
            return refusals.refuse(e)

        consent_mode = settings.get(db_path, "remote.consent_mode") or "unattended"
        issued_by = sharing_web.peer_actor(peer, data)
        session_id = remote.create_session(
            db_path, state["machine"], issued_by, consent_mode,
            ttl_seconds=settings.get_int(db_path, "remote.session_ttl_seconds"),
        )
        # request.remote_addr is the BORROWING HUB's address, not the operator's browser --
        # the media never flows to that hub, so a LAN-vs-public choice made from it would be
        # wrong. Passing None makes select_urls_for_peer offer the public set, which is the
        # only one that can be right for a peer whose location this hub cannot know.
        ice = _ice_servers(session_id, None)
        try:
            fleet.create_command(
                db_path, machine=state["machine"], command_type="start_remote_session",
                params={"session_id": session_id, "consent_mode": consent_mode,
                        "ice_servers": ice, **stream},
                issued_by=issued_by,
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            remote.end_session(db_path, session_id, "failed to queue start command",
                               actor=issued_by)
            return refusals.refuse(e)

        fleet.audit(db_path, actor=issued_by, action="share.action",
                    level=fleet.LEVEL_SECURITY, target=state["machine"],
                    detail={"share_id": state["share_id"], "peer_id": peer["peer_id"],
                            "peer_label": peer.get("label") or "",
                            "type": "start_remote_session", "session_id": session_id,
                            "claimed_operator": permissions.normalize_email(
                                data.get("operator")) or None})
        return jsonify({"session_id": session_id, "ice_servers": ice,
                        "consent_mode": consent_mode, **stream}), 201

    @bp.route("/api/peer/shares/<share_id>/remote/<session_id>/signal", methods=["POST"])
    @peer_remote
    @peer_session
    def peer_signal(peer, state, sess):
        data = request.get_json(silent=True) or {}
        if data.get("kind") == "relay":
            # No relay for a borrowed machine: its frames would cross this hub AND the
            # borrowing one, and the share was granted for a session whose media goes peer to
            # peer. Refused here rather than queued for the agent, which would otherwise drop a
            # peer for a relay nobody opened.
            return jsonify({"error": "Relaying through the hub is not available for a shared "
                                     "machine."}), 403
        try:
            seq = remote.add_signal(db_path, sess["id"], remote.SENDER_CONSOLE,
                                    data.get("kind"), data.get("payload"))
        except KeyError:
            return jsonify({"error": "unknown session"}), 404
        except PermissionError as e:
            return refusals.refuse(e, 409)
        except ValueError as e:
            return refusals.refuse(e)
        return jsonify({"seq": seq}), 200

    @bp.route("/api/peer/shares/<share_id>/remote/<session_id>/poll", methods=["GET"])
    @peer_remote
    @peer_session
    def peer_poll(peer, state, sess):
        try:
            after_seq = int(request.args.get("after_seq", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "after_seq must be an integer"}), 400
        result = remote.get_signals(db_path, sess["id"], remote.SENDER_CONSOLE, after_seq)
        result["status"] = remote.get_session(db_path, sess["id"])["status"]
        return jsonify(result), 200

    @bp.route("/api/peer/shares/<share_id>/remote/<session_id>/stop", methods=["POST"])
    @peer_remote
    @peer_session
    def peer_stop(peer, state, sess):
        remote.end_session(db_path, sess["id"], "peer operator stopped",
                           actor=sharing_web.peer_actor(peer))
        return jsonify({"status": "ended"}), 200

    # ---------------- Virtual display (remote_control + scope) ----------------
    # These exist to make a headless machine viewable at all, so they belong to the same
    # capability as opening a session rather than inventing a new one. fleet_web refuses them
    # on the generic command channel, so this is the only way in.

    @bp.route("/api/remote/<machine>/virtual-display", methods=["POST"])
    @login_required
    @can_remote
    def virtual_display(machine):
        """Install or uninstall the virtual display on one machine.

        The install carries a snapshot of the payload pin -- the digest and the hub URL the
        agent should fetch from -- taken now. The bytes themselves live in the existing package
        blob store and travel over the existing authenticated, digest-verified agent download
        path, so this adds no new download channel and nothing to the agent's signed update
        manifest.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        data = request.get_json(silent=True) or {}
        mode = str(data.get("mode", "install")).lower()
        if mode not in ("install", "uninstall"):
            return jsonify({"error": "mode must be 'install' or 'uninstall'"}), 400

        if mode == "uninstall":
            command_id, error = _queue(machine, "uninstall_virtual_display", {})
            if error:
                return jsonify({"error": error}), 400
            fleet.audit(db_path, _current_email(), "virtual_display_uninstall", machine)
            return jsonify({"command_id": command_id}), 202

        payload = remote.get_virtual_display_payload(db_path)
        if payload is None:
            return jsonify({
                "error": "No virtual display driver has been uploaded yet. Upload the driver "
                         "package on the Packages page, then pin it in Settings > Remote."
            }), 409

        try:
            params = _virtual_display_settings(data)
        except ValueError as e:
            return refusals.refuse(e)
        params.update({
            "payload_url": _agent_package_url(payload["sha256"]),
            "payload_sha256": payload["sha256"],
            "version": payload["version"],
        })
        if data.get("allow_arm64"):
            params["allow_arm64"] = True

        command_id, error = _queue(machine, "install_virtual_display", params)
        if error:
            return jsonify({"error": error}), 400
        fleet.audit(db_path, _current_email(), "virtual_display_install", machine,
                    f"version={payload['version']} sha256={payload['sha256'][:16]}")
        return jsonify({"command_id": command_id, "version": payload["version"]}), 202

    @bp.route("/api/remote/<machine>/virtual-display/mode", methods=["POST"])
    @login_required
    @can_remote
    def virtual_display_mode(machine):
        """Change how many virtual monitors exist and at what resolutions.

        `monitors: 0` is the graceful stand-down for a machine that has since had a real
        monitor plugged in -- the driver stays installed and stops adding a phantom display,
        with no uninstall and no reboot.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        try:
            params = _virtual_display_settings(request.get_json(silent=True) or {})
        except ValueError as e:
            return refusals.refuse(e)
        command_id, error = _queue(machine, "set_virtual_display_mode", params)
        if error:
            return jsonify({"error": error}), 400
        fleet.audit(db_path, _current_email(), "virtual_display_mode", machine,
                    f"monitors={params['monitors']}")
        return jsonify({"command_id": command_id}), 202

    def _queue(machine, command_type, params):
        try:
            return fleet.create_command(
                db_path, machine=machine, command_type=command_type, params=params,
                issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            ), None
        except ValueError as e:
            # refusals.message, not str(e): the caller renders this into a hand-built 400, which
            # refusals.refuse() never sees, so it has to ask for the same filtered sentence.
            return None, refusals.message(e)

    def _agent_package_url(sha256):
        """The hub URL an agent fetches a blob from. Relative to the hub's own base so it works
        behind the TLS terminator, which the app itself only ever sees as http."""
        base = (os.environ.get("HUB_URL") or "").rstrip("/")
        return f"{base}/api/agent/packages/{sha256}"

    def _virtual_display_settings(data):
        """Validate monitor count and resolutions. A bad entry is rejected rather than dropped:
        an operator who asked for 3840x2160 and silently got 1920x1080 would have no way to
        tell, and would reasonably conclude the driver was broken."""
        try:
            monitors = int(data.get("monitors", 1))
        except (TypeError, ValueError):
            raise ValueError("monitors must be an integer")
        if not 0 <= monitors <= 8:
            raise ValueError("monitors must be between 0 and 8")

        resolutions = []
        for entry in list(data.get("resolutions") or [])[:32]:
            if not isinstance(entry, dict):
                raise ValueError("each resolution must be an object")
            try:
                width, height = int(entry.get("width")), int(entry.get("height"))
                hz = int(entry.get("hz", 60))
            except (TypeError, ValueError):
                raise ValueError("resolution width, height and hz must be integers")
            if not (640 <= width <= 7680 and 480 <= height <= 4320):
                raise ValueError(f"resolution {width}x{height} is out of range")
            if not 24 <= hz <= 240:
                raise ValueError(f"refresh rate {hz} is out of range")
            resolutions.append({"width": width, "height": height, "hz": hz})

        if not resolutions:
            resolutions = [{"width": 1920, "height": 1080, "hz": 60}]
        return {"monitors": monitors, "resolutions": resolutions}

    @bp.route("/api/remote/virtual-display/payload", methods=["GET", "POST"])
    @login_required
    @can_manage_settings
    def virtual_display_payload():
        """Read or set which uploaded package blob is the virtual display driver.

        Deliberately manage_settings rather than remote_control: pinning the payload decides
        what code the whole fleet will be told to install, which is a fleet-wide configuration
        decision, while installing it on one machine is a per-machine operational one.
        """
        if request.method == "GET":
            return jsonify({"payload": remote.get_virtual_display_payload(db_path)}), 200

        data = request.get_json(silent=True) or {}
        sha256 = str(data.get("sha256") or "").strip().lower()
        version = str(data.get("version") or "").strip()
        filename = str(data.get("filename") or "").strip()
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            return jsonify({"error": "sha256 must be a 64-character hex digest"}), 400
        if not version:
            return jsonify({"error": "version is required"}), 400

        remote.set_virtual_display_payload(db_path, version, sha256, filename, _current_email())
        fleet.audit(db_path, _current_email(), "virtual_display_payload_set", None,
                    f"version={version} sha256={sha256[:16]}")
        return jsonify({"payload": remote.get_virtual_display_payload(db_path)}), 200

    # ---------------- TURN configuration (manage_settings) ----------------
    @bp.route("/api/remote/turn/status", methods=["GET"])
    @login_required
    @can_manage_settings
    def turn_status():
        """What the Remote settings tab needs to diagnose 'ice_servers=0' without shell access:
        whether the secret is set, how many STUN/TURN URLs are configured, and -- the number
        that actually predicts a working session -- how many ICE servers a session would hand a
        peer right now."""
        secret = os.environ.get(TURN_SECRET_ENV, "")
        stun = settings.get_list(db_path, "remote.stun_urls")
        turn = settings.get_list(db_path, "remote.turn_urls")
        preview = remote.ice_servers(
            "preview", stun_urls=stun, turn_urls=turn, turn_secret=secret,
            turn_ttl=settings.get_int(db_path, "remote.turn_ttl_seconds"),
        )
        # How the URLs split by vantage, because "3 TURN URLs configured" hides the failure this
        # page exists to catch: a relay reachable only from inside the hub's network, or only
        # from outside it. A deployment with zero of either is not necessarily wrong (a LAN-only
        # fleet needs no public URL) but it does bound which sessions can ever connect.
        lan = [u for u in turn if remote.is_lan_address(remote.ice_url_host(u))]
        return jsonify({
            "enabled": bool(settings.get_bool(db_path, "remote.enabled")),
            "secret_set": bool(secret),
            "can_write_secret": bool(env_path),
            "stun_count": len(stun),
            "turn_count": len(turn),
            "turn_lan_count": len(lan),
            "turn_wan_count": len(turn) - len(lan),
            "turn_tcp_count": len([u for u in turn if "transport=tcp" in u.lower()]),
            "ice_count": len(preview),
        }), 200

    @bp.route("/api/remote/turn/secret", methods=["POST"])
    @login_required
    @can_manage_settings
    def turn_secret():
        """Set or rotate REMOTE_TURN_SECRET from the console. An explicit value is stored as-is
        (use this to match an existing coturn's static-auth-secret); an empty value mints a fresh
        random one (rotation). The secret is written to .env AND to the live process environment,
        so minting picks it up immediately with no restart -- but coturn still validates against
        its own copy, so the response returns the value once for the operator to sync to coturn.
        """
        if not env_path:
            return jsonify({"error": "The hub cannot write .env in this deployment; set "
                                     "REMOTE_TURN_SECRET on the host instead."}), 400
        data = request.get_json(silent=True) or {}
        provided = str(data.get("secret") or "").strip()
        value = provided or remote.generate_turn_secret()
        try:
            remote.set_env_var(env_path, TURN_SECRET_ENV, value)
        except ValueError:
            # A line break or control character in the pasted secret. envfile refuses it before
            # writing anything, because a second line in .env is a change to ALLOWED_EMAILS made
            # through the relay-secret box. A fixed sentence rather than the exception's text,
            # so no internal wording reaches the browser.
            return jsonify({"error": "The TURN secret cannot contain line breaks or other "
                                     "control characters."}), 400
        except OSError as e:
            # The OSError names the absolute path of .env on the host; that belongs in the
            # log for whoever has a shell there, not in a browser.
            print(f"[remote] could not write the TURN secret to .env: {e}")
            return jsonify({"error": "Could not write .env on the hub -- see the hub log."}), 500
        os.environ[TURN_SECRET_ENV] = value
        # Never put the secret itself in the audit detail -- only that it changed and how.
        fleet.audit(db_path, actor=_current_email(), action="remote_turn_secret_set",
                    level=fleet.LEVEL_SECURITY,
                    target="hub", detail={"rotated": not provided})
        return jsonify({"secret_set": True, "secret": value}), 200

    return bp
