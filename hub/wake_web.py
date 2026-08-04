"""Flask HTTP surface for Wake-on-LAN (roadmap #10) -- a thin layer over wake.py,
registered as a Blueprint from app.py.

**Two gates, and the split follows the one bios_web.py already draws:**

  * **Reading** a machine's adapters and its wakeability diagnosis is `view` + machine
    scope. Which NICs a PC has, and whether it can be woken at all, is inventory in exactly
    the sense its model and its disks are -- an operator who can see the machine can see it.
    Making "why won't this PC wake?" cost an admin would make the question cost more than
    walking to the desk, which is the same reasoning that keeps the Firmware tab readable.
  * **Waking**, **preparing** and **cancelling** are `issue_commands` + machine scope. There
    is deliberately no new capability: waking a PC is strictly less dangerous than the
    `shutdown` the same capability already covers, and the vocabulary is already large.

**The answers here are phrased as attempts, because that is what they are.** Nothing
acknowledges a magic packet, so this layer never says "woken" on the strength of having sent
one -- it reports which stage the request reached and lets the machine's own check-in supply
the ending. `no_relay` in particular is returned as a first-class outcome with the subnet
named, not as an error: at 3am every machine on a subnet being asleep is the expected state,
and "wake failed" would send an operator looking in entirely the wrong place.

**Diagnoses cross the wire as CODES, not sentences.** The console renders them through i18n
like every other server-supplied vocabulary (capability labels, detection kinds, path
tokens), so a hub answering a German operator does not have to be told which language to
build a sentence in. wake.diagnose is the single source of the list.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, request

import fleet
import permissions
import permissions_web
import settings
import wake

#: Every diagnosis code wake.diagnose can emit, so the console can be checked against it and
#: a code added without a translation fails a test instead of captioning a card with its own
#: key. Mirrors the i18n coverage assertions the settings REGISTRY already carries.
DIAGNOSIS_CODES = ("no_report", "no_wired_nic", "wireless_only", "no_address",
                   "wake_disabled", "fast_startup")


def create_wake_blueprint(db_path, login_required, access, machine_roster=None):
    """Build the wake Blueprint.

    `machine_roster` is app.py's fleet roster callable -- the same one the backup and
    firmware schedulers use, so "online" means one thing across the hub rather than this
    blueprint growing a second, subtly different definition. It must yield dicts carrying
    `machine`, `online` and `last_seen`; the last of those is what lets a wake be confirmed
    against the moment its packet went out rather than against mere online-ness.
    """
    bp = Blueprint("wake", __name__)
    can_view = access.require(permissions.VIEW)
    can_issue = access.require(permissions.ISSUE_COMMANDS)

    def _current_email():
        return permissions_web.current_actor()

    def _require_json():
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _roster(names=None):
        entries = [e for e in (machine_roster() if machine_roster else []) if e.get("machine")]
        if names is None:
            return entries
        wanted = set(names)
        return [e for e in entries if e["machine"] in wanted]

    def _online_names(entries=None):
        return {e["machine"] for e in (entries if entries is not None else _roster())
                if e.get("online")}

    def _ttl():
        return settings.get_int(db_path, "wake.request_ttl_seconds")

    def _tick(entries=None):
        """Run a scheduler pass right now, so a button press does not wait out the tick.

        Same shape as backups_web's `_dispatch_files`: the scheduler still owns the work and
        this is only an early nudge, so an operator watching the console sees the relay
        chosen immediately instead of up to an interval later.
        """
        try:
            return wake.tick(
                db_path, machines=entries if entries is not None else _roster(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"),
                confirm_timeout=settings.get_int(db_path, "wake.confirm_timeout_seconds"),
                allow_hub_broadcast=settings.get_bool(db_path, "wake.hub_broadcast"))
        except Exception as e:  # never let a dispatch failure lose the recorded request
            print(f"[wake] Immediate dispatch failed: {e}")
            return 0, 0, 0

    def _payload(machine, entries=None):
        """Everything the machine page's Network card renders, in one answer."""
        network = wake.get_network(db_path, machine)
        online = machine in _online_names(entries)
        return {
            "machine": machine,
            "online": online,
            "nics": network["nics"],
            "fast_startup": network["fast_startup"],
            "reported_at": network["reported_at"],
            "subnets": wake.target_subnets(db_path, machine),
            # A machine that is ONLINE is not diagnosed. Every code here is a reason a wake
            # would not arrive, and showing "Fast Startup is on" beside a machine that is
            # running right now reads as a fault rather than as advance notice.
            "diagnosis": [] if online else wake.diagnose(network),
            "wakeable": bool(wake.wakeable_nics(network)),
            "request": wake.open_request_for(db_path, machine),
            "history": wake.list_requests(db_path, machine, limit=10),
            "can_wake": access.can(permissions.ISSUE_COMMANDS),
        }

    # ---------------- Read ----------------
    @bp.route("/api/wake/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_network(machine):
        return jsonify(_payload(machine)), 200

    @bp.route("/api/wake/requests", methods=["GET"])
    @login_required
    @can_view
    def recent_requests():
        """Wakes across the whole fleet, narrowed to what this operator can see.

        Scope filtering happens here rather than in the query because wake.py deliberately
        knows nothing about permission groups -- the same division fleet.py and packages.py
        keep.
        """
        open_only = request.args.get("open") in ("1", "true", "yes")
        rows = wake.list_requests(db_path, limit=200, open_only=open_only)
        return jsonify({"requests": access.filter_rows(rows)}), 200

    # ---------------- Wake ----------------
    @bp.route("/api/wake/machines/<machine>", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def wake_machine(machine):
        """Wake one PC, or as soon as a peer on its subnet is awake to relay for it.

        Answers 202 in every non-error case, including `no_relay` and `unwakeable`: the
        request WAS recorded, and the outcome is a fact about the fleet rather than a fault
        in the call. A 4xx here would be read as "the console is broken" for what is
        actually "everything on that subnet is switched off".
        """
        bad = _require_json()
        if bad:
            return bad
        entries = _roster()
        online = machine in _online_names(entries)
        body = request.get_json(silent=True) or {}

        try:
            result = wake.request_wake(db_path, machine, requested_by=_current_email(),
                                       reason=str(body.get("reason") or "")[:200],
                                       online=online, ttl_seconds=_ttl())
        except wake.WakeRejected as e:
            return jsonify({"error": str(e)}), 400

        fleet.audit(db_path, actor=_current_email(), action="wake_request",
                    level=fleet.LEVEL_NOTICE, target=machine,
                    detail={"request_id": result["id"], "status": result["status"],
                            "online": online})
        if result["status"] == wake.STATUS_PENDING:
            _tick(entries)
        return jsonify(_payload(machine, entries)), 202

    @bp.route("/api/wake/fleet", methods=["POST"])
    @login_required
    @can_issue
    def wake_fleet():
        """Wake every OFFLINE machine in scope -- the pairing that makes this worth having.

        Machines that are already awake or cannot be woken still get a request row with
        their own outcome rather than being dropped, so the counts add up: silently skipping
        them is how somebody believes forty PCs were woken and thirty were not.

        An explicit `machines` list narrows it (the maintenance-window case); without one it
        is the whole visible fleet.
        """
        bad = _require_json()
        if bad:
            return bad
        body = request.get_json(silent=True) or {}
        entries = _roster()
        wanted = body.get("machines")
        names = [e["machine"] for e in entries if access.in_scope(e["machine"])]
        if isinstance(wanted, list):
            asked = {str(n).strip() for n in wanted if str(n).strip()}
            names = [n for n in names if n in asked]

        actor = _current_email()
        try:
            results = wake.request_many(db_path, names, requested_by=actor,
                                        reason=str(body.get("reason") or "")[:200],
                                        online=_online_names(entries), ttl_seconds=_ttl())
        except wake.WakeRejected as e:
            return jsonify({"error": str(e)}), 400

        counts = {}
        for row in results:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        fleet.audit(db_path, actor=actor, action="wake_request_fleet",
                    level=fleet.LEVEL_NOTICE,
                    detail={"requested": len(results), "counts": counts})
        if counts.get(wake.STATUS_PENDING):
            _tick(entries)
        return jsonify({
            "requested": len(results),
            "counts": counts,
            "requests": wake.list_requests(db_path, limit=len(results) or 1),
        }), 202

    @bp.route("/api/wake/requests/<request_id>/cancel", methods=["POST"])
    @login_required
    @can_issue
    def cancel_request(request_id):
        """Give up on a wake no relay has been handed yet.

        An unknown id and an out-of-scope one both answer 404, exactly as fleet_web's
        `scoped_command` does: distinguishing them would turn this into an oracle for which
        requests exist on machines the caller cannot see.
        """
        bad = _require_json()
        if bad:
            return bad
        existing = wake.get_request(db_path, request_id)
        if existing is None or not access.in_scope(existing["machine"]):
            return jsonify({"error": "unknown wake request"}), 404
        if not wake.cancel_request(db_path, request_id):
            # Deliberately specific rather than a generic 409: the operator needs to know
            # the packet is already gone, which is a different situation from a stale click.
            return jsonify({"error": "This wake has already been sent to a relay and "
                                     "cannot be recalled.",
                            "request": wake.get_request(db_path, request_id)}), 409
        fleet.audit(db_path, actor=_current_email(), action="wake_cancel",
                    level=fleet.LEVEL_NOTICE, target=existing["machine"],
                    detail={"request_id": request_id})
        return jsonify(_payload(existing["machine"])), 200

    # ---------------- Preconditions ----------------
    @bp.route("/api/wake/machines/<machine>/prepare", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def prepare_machine(machine):
        """Make a PC wakeable: turn its NICs' wake flags on and Fast Startup off.

        The remedy half of the diagnosis above, and the reason this feature is usable on a
        fleet rather than a desk. Most of a WoL rollout is preconditions rather than code,
        and a console that can only NAME the four reasons a machine will not wake leaves
        somebody visiting forty desks to fix them.

        Runs only on a machine that is awake -- it is a local configuration change, not a
        wake -- so it is queued like any other command and expires if the PC never answers.
        """
        bad = _require_json()
        if bad:
            return bad
        command_id = fleet.create_command(
            db_path, machine=machine, command_type="prepare_wake",
            # No machine-specific payload at all: what to enable is decided on the machine,
            # against the adapters it actually has. That is also what keeps this one
            # favoritable while `wake_machine` is not.
            params={}, issued_by=_current_email(),
            ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"))
        return jsonify({"status": "queued", "command_id": command_id}), 202

    return bp
