"""Flask HTTP surface for network discovery -- a thin layer over discovery.py,
registered as a Blueprint from app.py.

**Two gates, and the split is the one wake_web.py already draws over the same table.**

  * **Reading** a machine's sweepable subnets and its last scan is `view` + machine scope.
    What the machine can see from where it sits is inventory in the same sense its adapters
    are, and #10 already shows those adapters to anyone with `view`.
  * **Running** a sweep is `issue_commands` + machine scope. No new capability, for the same
    reason wake_web gives: an operator who can already open a SYSTEM shell on that machine
    can run `arp -a` on it by hand, so a separate capability here would guard a door that is
    standing open next to it. What the gate is really doing is naming an accountable actor
    for the audit row, which is why every sweep writes one.

**The result endpoint is agent-facing and re-checks the machine**, exactly as
`/api/agent/files/*` does: an enrolled agent may answer for itself and for nothing else.
PC-3 posting a host list against PC-4's scan would attribute a whole fabricated network to a
site PC-3 has never been on, and the console would have no way to tell.

**There is no fleet-wide "sweep everything" verb here, and that is deliberate.** The
fleet-wide button is the shape #10 has for wakes, and it is right there, so its absence
wants a reason: a wake is one UDP frame per target and a sweep is a few hundred ARP probes
per subnet, run concurrently across every site at once. A single click that does that to a
whole company is the thing the roadmap entry is uneasy about ("a discovery sweep is a
scanner pointed at a colleague's network"). One machine, one subnet, one operator's name
against it.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, request

import auth_helpers
import discovery
import fleet
import permissions
import permissions_web
import refusals
import settings

_bearer_agent = auth_helpers.bearer_agent

#: Every classification discovery.classify can emit, so the console can be checked against
#: it and a class added without a translation fails a test rather than captioning a row with
#: its own key. Mirrors wake_web.DIAGNOSIS_CODES, for the same reason.
CLASSIFICATIONS = discovery.CLASSIFICATIONS


def create_discovery_blueprint(db_path, login_required, access, machine_roster=None):
    """Build the discovery Blueprint.

    `machine_roster` is app.py's fleet roster callable -- the same one wake_web takes, so
    "online" means one thing across the hub rather than this blueprint growing a second,
    subtly different definition of it.
    """
    bp = Blueprint("discovery", __name__)
    can_view = access.require(permissions.VIEW)

    def _current_email():
        """Attribute a sweep to the authenticated actor, never to request data."""
        return permissions_web.current_actor()

    def _require_json():
        """Reject bodies Flask would otherwise silently treat as an empty request."""
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _online(machine):
        """Use app.py's live roster as the single definition of agent availability."""
        entries = machine_roster() if machine_roster else []
        return any(e.get("machine") == machine and e.get("online") for e in entries)

    def _reconcile():
        """Retire sweeps whose command died or whose report never arrived.

        Run on read rather than from a scheduler thread, which is the one place this
        departs from wake_web. A wake has work to do while nobody is watching -- it waits
        across ticks for a peer to come online, and the pairing with maintenance windows
        happens at 3am. A sweep has none: it is started by an operator who is looking at the
        card, the card polls while it runs, and a scan that nobody ever looks at again has
        no consequence for being marked failed late. So the hub gets no ninth daemon thread,
        and the reconciliation happens exactly when its answer is about to be read.

        It is one indexed query over `status = 'scanning'`, which is empty almost always.
        """
        try:
            discovery.reconcile_once(db_path)
        except Exception as e:  # a stale row must never take the card down with it
            print(f"[discovery] Reconcile pass failed: {e}")

    def _payload(machine):
        """Everything the Network tab's Discovery card renders, in one answer."""
        _reconcile()
        scan = discovery.latest_scan(db_path, machine)
        return {
            "machine": machine,
            "subnets": discovery.sweepable_subnets(db_path, machine),
            "online": _online(machine),
            "scan": discovery.open_scan_for(db_path, machine) or scan,
            "history": discovery.list_scans(db_path, machine, limit=10),
            "can_scan": access.can(permissions.ISSUE_COMMANDS),
        }

    # ---------------- Read ----------------
    @bp.route("/api/discovery/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_discovery(machine):
        """Return the scoped machine card payload for the Network tab."""
        return jsonify(_payload(machine)), 200

    @bp.route("/api/discovery/scans/<scan_id>", methods=["GET"])
    @login_required
    @can_view
    def scan_detail(scan_id):
        """One scan and its hosts.

        An unknown id and an out-of-scope one both answer 404, exactly as wake_web's cancel
        does: telling them apart would turn this into an oracle for which machines exist on
        subnets the caller cannot see.
        """
        scan = discovery.get_scan(db_path, scan_id)
        if scan is None or not access.in_scope(scan["machine"]):
            return jsonify({"error": "unknown scan"}), 404
        scan["hosts"] = discovery.scan_hosts(db_path, scan["id"])
        scan["counts"] = discovery.count_by_class(scan["hosts"])
        return jsonify(scan), 200

    @bp.route("/api/discovery/scans", methods=["GET"])
    @login_required
    @can_view
    def recent_scans():
        """Sweeps across the whole fleet, narrowed to what this operator can see.

        Scope filtering happens here rather than in the query because discovery.py
        deliberately knows nothing about permission groups -- the same division wake.py and
        packages.py keep.
        """
        _reconcile()
        rows = discovery.list_scans(db_path, limit=100)
        return jsonify({"scans": access.filter_rows(rows)}), 200

    # ---------------- Sweep ----------------
    @bp.route("/api/discovery/machines/<machine>/scan", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def start_scan(machine):
        """Sweep one subnet from this machine.

        The scan row is written BEFORE the command is queued, and the command is what can
        fail: a command that will not queue (an agent that has disclaimed the type, a
        machine that has gone away between the click and here) leaves a scan row carrying
        the refusal, which is what the operator needs to read. The other order would lose
        the reason along with the row.
        """
        bad = _require_json()
        if bad:
            return bad
        body = request.get_json(silent=True) or {}
        subnet = str(body.get("subnet") or "").strip()

        try:
            scan = discovery.request_scan(
                db_path, machine, subnet, requested_by=_current_email(),
                online=_online(machine),
                ttl_seconds=settings.get_int(db_path, "discovery.scan_ttl_seconds"))
        except discovery.DiscoveryRejected as e:
            return refusals.refuse(e)

        try:
            command_id = fleet.create_command(
                db_path, machine=machine, command_type=discovery.COMMAND_TYPE,
                params={"scan_id": scan["id"], "subnet": scan["subnet"]},
                issued_by=_current_email(),
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"))
        except Exception as e:
            discovery.fail_scan(db_path, scan["id"], machine, str(e))
            return jsonify(_payload(machine)), 202
        discovery.attach_command(db_path, scan["id"], command_id)

        # LEVEL_NOTICE rather than LEVEL_INFO: "who pointed a scanner at which network, and
        # when" is the question this feature has to be able to answer about itself.
        fleet.audit(db_path, actor=_current_email(), action="network_sweep",
                    level=fleet.LEVEL_NOTICE, target=machine,
                    detail={"scan_id": scan["id"], "subnet": scan["subnet"],
                            "command_id": command_id})
        return jsonify(_payload(machine)), 202

    # ================================================================
    # Agent-facing
    # ================================================================
    @bp.route("/api/agent/discovery/scan/<scan_id>", methods=["POST"])
    def agent_report_scan(scan_id):
        """One subnet's hosts, reported by the machine that swept it.

        Reported here rather than as the command's output, for the reason
        ListDirectoryExecutor gives: a full /22 is a thousand rows, the command channel's
        output is a terminal transcript, and keeping them apart means "the machine answered"
        and "the answer is stored" stay two facts the console can tell apart. A sweep whose
        POST was lost to a dropped connection must not look like a subnet with nothing on
        it -- which is precisely the finding this feature would then get wrong.
        """
        agent_id, machine = _bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        data = request.get_json(silent=True) or {}
        error = data.get("error")
        if error:
            if not discovery.fail_scan(db_path, scan_id, machine, error):
                return jsonify({"error": "unknown scan"}), 404
            return jsonify({"status": "recorded"}), 200
        if not discovery.record_results(db_path, scan_id, machine, data):
            return jsonify({"error": "unknown scan"}), 404
        return jsonify({"status": "stored"}), 200

    return bp
