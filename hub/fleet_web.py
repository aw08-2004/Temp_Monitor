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

import backups
import bios
import firmware
import fleet
import live
import permissions
import permissions_web
import processes
import remote
import settings
import terminal
import wake


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


def create_fleet_blueprint(db_path, enrollment_secret, login_required, access,
                           on_command_result=None):
    """Build the fleet Blueprint. `login_required` (app.py's session gate) and `access`
    (the permission-group layer) are both passed in, to avoid a circular import and to
    keep one source of truth for each.

    `on_command_result(command_id, machine, success, result, output)` is an optional hook fired
    after a command result is recorded. The rules engine uses it to route a show_message
    answer to its follow-up actions -- passed in rather than imported for the same reason as
    the two above, and so this module keeps no opinion about what any command means."""
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

        It may also send `profiles`, `remote`, `bios` and `network` -- the slow local
        inventories, each on its own change-only cadence, none of them ever fatal -- and
        `processes`, which is neither slow nor change-only but is only sent at all while an
        operator has that machine's Processes card open (see the `processes_wanted` reply).

        `profiles` is the user profiles and resolved known folders on
        that machine. That is how the Backup Settings tab can show what `%Users%\\Desktop`
        ACTUALLY expands to on a given PC rather than echoing the pattern back. The agent
        sends it only when it changes (a new user signs in, OneDrive redirects a folder),
        so this is not per-heartbeat traffic. Never fatal: a malformed payload costs a
        stale preview, not a heartbeat.
        """
        # authenticate_agent already refreshed last_seen.
        data = request.get_json(silent=True) or {}
        payload = {"status": "ok", "machine": machine}
        current_version = settings.agent_config_version(db_path)
        if data.get("config_version") != current_version:
            payload["config"] = settings.agent_config(db_path)
            payload["config_version"] = current_version
        if data.get("profiles"):
            try:
                backups.record_profiles(db_path, machine, data["profiles"])
            except Exception as e:
                print(f"[backup] Could not record profiles for {machine}: {e}")
        # Logon sessions + display outputs, on the same change-only cadence and with the same
        # never-fatal handling: this feeds the remote session picker and the headless badge,
        # and neither is worth failing a heartbeat over.
        if data.get("remote"):
            try:
                remote.record_inventory(db_path, machine, data["remote"])
            except Exception as e:
                print(f"[remote] Could not record inventory for {machine}: {e}")
        # BIOS/firmware settings, same change-only cadence and same never-fatal handling. A
        # BIOS enumeration is slow (seconds, on some vendors) and the offline window is 90s --
        # which is exactly why it is scanned on the agent's inventory loop and merely CARRIED
        # here, never performed here. An unsupported machine reports that fact once and then
        # goes quiet, so a fleet of VMs costs one payload each, forever.
        if data.get("bios"):
            try:
                bios.record_inventory(db_path, machine, data["bios"])
            except Exception as e:
                print(f"[bios] Could not record inventory for {machine}: {e}")
            # ...and the same report is what CONFIRMS a firmware flash. The flash itself
            # completes during POST, long after the `update_bios` command was answered, so
            # the version this machine now reports is the only honest evidence it worked --
            # see firmware.confirm_from_inventory. Separate try/except from the ingest
            # above: a machine whose inventory failed to store may still have carried the
            # version that closes out a staged update, and neither is worth a heartbeat.
            try:
                firmware.confirm_from_inventory(db_path, machine,
                                                (data["bios"] or {}).get("bios_version"))
            except Exception as e:
                print(f"[firmware] Could not confirm updates for {machine}: {e}")
        # Network adapters (roadmap #10), same change-only cadence and same never-fatal
        # handling. This is what the hub groups machines into subnets by, so that a sleeping
        # PC can be woken through an awake peer on its own segment -- and it has to come
        # from the agent, because the only address the hub can otherwise see is the NAT'd
        # site edge that every machine at that office shares.
        if data.get("network"):
            try:
                wake.record_network(db_path, machine, data["network"])
            except Exception as e:
                print(f"[wake] Could not record network inventory for {machine}: {e}")
        # The process list (the machine's Processes card). The ONE payload here that is not
        # change-only, because a process list that has not changed is not a thing that
        # happens -- and unlike its neighbours it is not sent unless somebody is looking:
        # `processes_wanted` below is what turns the machine's sampling on, and it goes
        # false again a few seconds after the operator closes the card. Same never-fatal
        # handling as the rest; a dropped snapshot costs one refresh.
        if data.get("processes"):
            try:
                processes.record_snapshot(db_path, machine, data["processes"])
            except Exception as e:
                print(f"[processes] Could not record processes for {machine}: {e}")
        # Answered on EVERY heartbeat, including the ones carrying nothing: this is how an
        # agent learns to STOP sampling (and how a pre-1.71 agent learns to start). A machine
        # nobody is looking at reads `false` here and does no process work at all.
        try:
            payload["processes_wanted"] = processes.is_watched(db_path, machine)
        except Exception as e:
            print(f"[processes] Could not resolve the watch for {machine}: {e}")
        # The other demand-driven flag, answered on every heartbeat for the same reason:
        # this is how a machine learns to go BACK to its ordinary five-second telemetry
        # after somebody closes the machine page (see live.py). Never fatal, and its own
        # try/except -- a failure here must not cost the process flag above.
        try:
            payload["live_wanted"] = live.is_watched(db_path, machine)
            payload["live_interval_seconds"] = live.FAST_INTERVAL_SECONDS
        except Exception as e:
            print(f"[live] Could not resolve the watch for {machine}: {e}")
        return jsonify(payload), 200

    @bp.route("/api/agent/processes/wanted", methods=["GET"])
    @agent_auth
    def agent_processes_wanted(agent_id, machine):
        """Does anybody want this machine's process list RIGHT NOW?

        Superseded by /api/agent/watch above, which answers this and the live-telemetry
        watch in one request; agents from 3.28.0 call that instead. Kept because every agent
        already in the field calls this one, and it must keep working unchanged.

        The same question the heartbeat answers, asked on its own so it can be answered
        promptly. The heartbeat is a 10-second tick carrying config, inventory and liveness;
        making an operator wait out one of those, plus a sampling window, plus a console
        poll, put the first process list 10-15 seconds after the click that asked for it.
        Long enough that the card looked broken, which is why "waiting" had to be a rendered
        state at all.

        The agent has no inbound port -- nothing here can push -- so the only way to shorten
        that is to let it ASK more often than it heartbeats, and the only way that is
        affordable fleet-wide is for the asking to be this: bearer auth, one indexed lookup
        on a single-row-per-machine table, and a ~30-byte answer. No config, no inventory, no
        writes. An idle fleet costs one of these per machine every couple of seconds and
        nothing else; the moment one answers `true` that machine starts sampling and posts
        its list on a heartbeat of its own (see the agent's ProcessLoopAsync).

        Scoped to the caller's own machine by construction: `machine` comes from the bearer
        token, never from the request, so one enrolled agent cannot learn that an operator is
        looking at another.
        """
        try:
            wanted = processes.is_watched(db_path, machine)
        except Exception as e:
            # Never fatal, and false rather than true: a hub that cannot answer must not
            # leave a machine sampling every five seconds on the strength of an error.
            print(f"[processes] Could not resolve the watch for {machine}: {e}")
            wanted = False
        return jsonify({"wanted": wanted}), 200

    @bp.route("/api/agent/watch", methods=["GET"])
    @agent_auth
    def agent_watch(agent_id, machine):
        """Is anybody looking at this machine RIGHT NOW, and at what?

        The successor to /api/agent/processes/wanted below, and the reason it takes both
        questions at once: an agent that asked them separately would double the only request
        an unwatched machine ever makes for either feature. One bearer-authenticated call,
        two indexed lookups on single-row-per-machine tables, no writes, a ~60-byte answer.

          * `processes` -- start sampling the process list (the Processes card).
          * `live`      -- report telemetry every `live_interval_seconds` with a full sensor
                           block, instead of every five seconds with one every other time
                           (the machine page's charts). See live.py.

        Both are also answered on the heartbeat, which stays authoritative for a hub that
        predates this route -- but a heartbeat is a 10-second tick, and both of these exist
        to make something feel like it responded to a click rather than eventually caught up.
        The agent has no inbound port, so asking often is the only way to answer promptly.

        Scoped to the caller's own machine by construction: `machine` comes from the bearer
        token, never from the request, so one agent cannot learn who is looking at another.
        """
        # Each in its own try, and false on failure: a hub that cannot answer must not leave
        # a machine enumerating processes -- or shipping a sensor block every second -- on
        # the strength of an error.
        try:
            wants_processes = processes.is_watched(db_path, machine)
        except Exception as e:
            print(f"[processes] Could not resolve the watch for {machine}: {e}")
            wants_processes = False
        try:
            wants_live = live.is_watched(db_path, machine)
        except Exception as e:
            print(f"[live] Could not resolve the watch for {machine}: {e}")
            wants_live = False
        return jsonify({
            "processes": wants_processes,
            "live": wants_live,
            "live_interval_seconds": live.FAST_INTERVAL_SECONDS,
        }), 200

    @bp.route("/api/agent/commands", methods=["GET"])
    @agent_auth
    def agent_commands(agent_id, machine):
        """Agent pulls (and thereby claims) any pending commands for its machine.

        Outbound-only, still: the agent makes this request, no inbound port is ever opened.
        What `?wait=<seconds>` adds is that the hub may HOLD it open when the queue is empty,
        so a command issued a moment later goes down this connection at once rather than
        waiting out the agent's next poll. See fleet.py's COMMAND PUSH block.

        The requested wait is a CEILING FROM BOTH ENDS: min(what the agent asked for, what
        this hub is configured to hold). The agent's own HTTP timeout is sized against its
        request, so a hub configured to hold longer must never be able to hold an agent past
        the point where it gives up and retries -- that would spend a slot on a connection
        with nobody on the other end.

        `waited` tells the agent whether the request was actually held. That is what lets it
        come straight back round instead of sleeping again (the hold WAS the sleep), and --
        just as important -- what lets an agent that was refused a slot fall back to its old
        cadence rather than hammering the hub with unheld requests.
        """
        hold = settings.get_int(db_path, "fleet.command_push_hold_seconds")
        max_agents = settings.get_int(db_path, "fleet.command_push_max_agents")
        try:
            requested = float(request.args.get("wait") or 0)
        except (TypeError, ValueError):
            requested = 0.0
        # Negative or nonsense reads as "don't wait", which is the pre-push behaviour and the
        # right answer for a client that didn't ask for this.
        wait_seconds = max(0.0, min(requested, float(hold)))

        commands, waited = fleet.wait_for_commands(
            db_path, agent_id, machine, wait_seconds, max_waiting=max_agents)
        return jsonify({"commands": commands, "waited": waited}), 200

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
        # Post-result hook (the rules engine's message routing). Deliberately after the
        # result is committed and deliberately non-fatal: the agent has already done the
        # work, and answering it 500 because a follow-up action threw would make it retry a
        # result that landed -- which, for a message whose answer restarts the machine, would
        # restart it twice.
        if on_command_result is not None:
            try:
                on_command_result(command_id, machine, success, data.get("result"), output)
            except Exception as e:                    # noqa: BLE001
                print(f"[fleet] Command-result hook failed for {command_id}: {e}")
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
        another and the audit trail would be fiction. Resolves a device token as well as
        a session cookie (roadmap #11), so an action taken from the app is attributed to
        the operator who paired it rather than to nobody."""
        return permissions_web.current_actor()

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
        # Same shape of hole, sharper consequence: these are gated on remote_control at the
        # Remote tab, so accepting them here would let a holder of issue_commands ALONE
        # install a driver and a new trusted publisher on any machine in their scope.
        if data.get("type") in fleet.VIRTUAL_DISPLAY_COMMANDS | fleet.REMOTE_CONTROL_COMMANDS:
            return jsonify({"error": "Remote sessions and virtual displays are managed from "
                                     "the Remote tab, not the command channel."}), 400
        # shell_open names a pty session row that POST /api/fleet/pty creates. Issued by
        # hand it would point an agent at a session id that does not exist, or at another
        # operator's -- so the only way to get one is through the endpoint that makes the
        # session and binds it to the caller.
        if data.get("type") == "shell_open":
            return jsonify({"error": "Terminal sessions are opened from the Terminal tab, "
                                     "not the command channel."}), 400
        # wake_machine is about ANOTHER machine: the hub picks a relay on the sleeping PC's
        # subnet and puts that PC's MAC and broadcast address in the params. Hand-rolled it
        # would carry a request id nothing reconciles, and -- the part that matters -- an
        # arbitrary address pair aimed at a machine whose subnet the caller never checked,
        # so the packet lands on the wrong segment and the console never shows that it did.
        # `prepare_wake` is deliberately NOT refused: it carries nothing machine-specific,
        # it is favoritable, and running a favorite comes back through this endpoint.
        if data.get("type") == "wake_machine":
            return jsonify({"error": "PCs are woken from the Network tab, which picks a "
                                     "machine on the target's own subnet to send the "
                                     "packet."}), 400
        # Same capability, but a different door on purpose: processes_web validates the
        # (name, pid) pairing that protects against PID reuse and refuses the critical
        # Windows processes whose termination is a bugcheck rather than a closed program.
        # Accepting a hand-rolled copy here would make both of those guards optional.
        if data.get("type") in fleet.PROCESS_COMMANDS:
            return jsonify({"error": "Processes are ended and restarted from the machine's "
                                     "Processes card, not the command channel."}), 400
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

    # ---------------- Interactive terminal (ConPTY) ----------------
    # Six endpoints, three each way, all of them thin: terminal.py owns the rules. The split
    # between agent-facing and console-facing is the same as everywhere else in this file
    # -- bearer token vs. browser session -- but the ownership checks are TIGHTER on the
    # console side. `issue_commands` plus machine scope is what lets you OPEN a terminal;
    # it is deliberately not enough to touch one that is already open, because that
    # session carries another operator's keystrokes. See terminal.py's module docstring.

    def _own_session(view):
        """Resolve <session_id>, and refuse anything that is not the caller's own live
        session on a machine they can see. Unknown / not-yours / out-of-scope all answer
        404 alike, matching scoped_command: distinguishing them would leak which session
        ids exist."""
        @functools.wraps(view)
        def wrapped(session_id, *args, **kwargs):
            found = terminal.get_session(db_path, session_id)
            if (found is None
                    or found["operator"] != _current_email().strip().lower()
                    or not access.in_scope(found["machine"])):
                return jsonify({"error": "unknown terminal session"}), 404
            return view(found, *args, **kwargs)
        return wrapped

    @bp.route("/api/fleet/pty", methods=["GET"])
    @login_required
    @can_view
    def fleet_list_pty():
        """The caller's own OPEN terminals on a machine, newest first.

        This is what makes a terminal survive leaving the page: on return the console asks
        "do I still have a shell here?" and re-attaches instead of spawning a second one.
        Scoped to the caller by construction -- there is no operator parameter, because the
        answer must never be "here is somebody else's terminal".
        """
        machine = (request.args.get("machine") or "").strip()
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        found = terminal.list_sessions(
            db_path, machine=machine, operator=_current_email(), active_only=True)
        return jsonify({"sessions": [
            {"session_id": row["id"], "shell": row["shell"], "status": row["status"],
             "cols": row["cols"], "rows": row["rows"], "created_at": row["created_at"]}
            for row in found
        ]}), 200

    @bp.route("/api/fleet/pty", methods=["POST"])
    @login_required
    @access.require(permissions.ISSUE_COMMANDS)
    def fleet_open_pty():
        """Open a terminal: create the session row, then issue the shell_open command that
        tells the agent to attach a pseudoconsole to it.

        ORDER MATTERS -- the session must exist before the command referencing it does, or
        an agent that polls in between gets a session id the hub has never heard of.
        """
        data = request.get_json(silent=True) or {}
        machine = data.get("machine")
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        try:
            found = terminal.open_session(
                db_path,
                machine=machine,
                operator=_current_email(),
                shell=data.get("shell"),
                cols=data.get("cols"),
                rows=data.get("rows"),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        try:
            command_id = fleet.create_command(
                db_path,
                machine=machine,
                command_type="shell_open",
                params={
                    "session_id": found["id"],
                    "shell": found["shell"],
                    "cols": found["cols"],
                    "rows": found["rows"],
                },
                issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
            )
        except ValueError as e:
            # Don't leave a session nothing will ever attach to sitting in the operator's
            # per-machine quota.
            terminal.finish_session(db_path, found["id"], "could not queue shell_open")
            return jsonify({"error": str(e)}), 400

        terminal.attach_command(db_path, found["id"], command_id)
        return jsonify({
            "session_id": found["id"],
            "command_id": command_id,
            "shell": found["shell"],
            "cols": found["cols"],
            "rows": found["rows"],
        }), 201

    @bp.route("/api/fleet/pty/<session_id>/input", methods=["POST"])
    @login_required
    @access.require(permissions.ISSUE_COMMANDS)
    @_own_session
    def fleet_pty_input(found):
        """Keystrokes, or a resize. `data` is raw terminal bytes and is passed through
        untouched -- see terminal.push_input."""
        body = request.get_json(silent=True) or {}
        terminal.note_console_seen(db_path, found["id"])
        try:
            if "size" in body:
                terminal.push_input(db_path, found["id"], "resize", body.get("size") or {})
            if body.get("data"):
                terminal.push_input(db_path, found["id"], "data", body.get("data"))
        except KeyError:
            return jsonify({"error": "unknown terminal session"}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"status": "ok"}), 200

    @bp.route("/api/fleet/pty/<session_id>/output", methods=["GET"])
    @login_required
    @can_view
    @_own_session
    def fleet_pty_output(found):
        try:
            after_seq = int(request.args.get("after_seq", -1))
        except (TypeError, ValueError):
            after_seq = -1
        # Being polled IS the operator still watching. Without this the abandonment reaper
        # could not tell "reading the output of a long build" from "closed the browser".
        terminal.note_console_seen(db_path, found["id"])
        try:
            body = terminal.pull_output(db_path, found["id"], after_seq)
        except KeyError:
            return jsonify({"error": "unknown terminal session"}), 404
        # The console can't tell "the agent is still starting the shell" from "the agent
        # is offline and never will" without this: a shell_open that expired means the
        # machine never picked it up.
        if body["status"] == terminal.STATUS_OPEN and found.get("command_id"):
            command = fleet.get_command(db_path, found["command_id"])
            if command and command["status"] == fleet.STATUS_EXPIRED:
                terminal.finish_session(db_path, found["id"], "the agent never picked it up")
                body["status"] = terminal.STATUS_CLOSED
                body["close_reason"] = "the agent never picked it up"
        return jsonify(body), 200

    @bp.route("/api/fleet/pty/<session_id>/clear", methods=["POST"])
    @login_required
    @can_view
    @_own_session
    def fleet_pty_clear(found):
        """Forget this session's scrollback. The shell keeps running."""
        terminal.note_console_seen(db_path, found["id"])
        terminal.clear_replay(db_path, found["id"])
        return jsonify({"status": "cleared"}), 200

    @bp.route("/api/fleet/pty/<session_id>/close", methods=["POST"])
    @login_required
    @can_view
    @_own_session
    def fleet_pty_close(found):
        """Ask the agent to end the session. It stays 'closing' until the agent confirms,
        so the console can tell a shell that actually went away from one that is ignoring
        us (a wedged child holding the console)."""
        terminal.request_close(db_path, found["id"])
        return jsonify({"status": "closing"}), 200

    @bp.route("/api/agent/pty/<session_id>/input", methods=["GET"])
    @agent_auth
    def agent_pty_input(agent_id, machine, session_id):
        """The agent's fast poll for keystrokes. Scoped to the agent's OWN machine: an
        enrolled agent must not be able to read the keystrokes typed at another."""
        found = terminal.get_session(db_path, session_id)
        if found is None or found["machine"] != machine:
            return jsonify({"error": "unknown terminal session"}), 404
        try:
            after_seq = int(request.args.get("after_seq", -1))
        except (TypeError, ValueError):
            after_seq = -1
        return jsonify(terminal.pull_input(db_path, session_id, after_seq)), 200

    @bp.route("/api/agent/pty/<session_id>/output", methods=["POST"])
    @agent_auth
    def agent_pty_output(agent_id, machine, session_id):
        found = terminal.get_session(db_path, session_id)
        if found is None or found["machine"] != machine:
            return jsonify({"error": "unknown terminal session"}), 404
        data = request.get_json(silent=True) or {}
        try:
            oldest = terminal.push_output(db_path, session_id, data.get("seq"), data.get("chunk"))
        except PermissionError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"status": "ok", "oldest_seq": oldest}), 200

    @bp.route("/api/agent/pty/<session_id>/closed", methods=["POST"])
    @agent_auth
    def agent_pty_closed(agent_id, machine, session_id):
        """The shell ended (operator typed `exit`, it crashed, or the agent honoured a
        close). Terminal state -- the console stops polling on seeing it."""
        found = terminal.get_session(db_path, session_id)
        if found is None or found["machine"] != machine:
            return jsonify({"error": "unknown terminal session"}), 404
        data = request.get_json(silent=True) or {}
        terminal.finish_session(db_path, session_id, data.get("reason") or "the shell exited")
        return jsonify({"status": "closed"}), 200

    return bp
