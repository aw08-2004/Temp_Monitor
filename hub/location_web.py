"""Flask HTTP surface for on-demand device location (roadmap #23 phase B) -- a thin layer over
location.py, registered as a Blueprint from app.py.

**Two gates, and the split is not the one every other machine feature uses.**

  * **Reading** a device's last known position is `view` + machine scope, like its model, its
    disks and its NIC list. Where a device was when somebody last asked is inventory; it is not
    a privilege to see it, and making it one would mean an operator could not answer "did we
    already look?" without an admin.
  * **Asking** is **`locate_device`** + machine scope -- its own capability, and NOT the
    `issue_commands` that wake, the Processes card and the file explorer all reuse. Those three
    reuse it on the argument that each is less dangerous than the SYSTEM shell that gate already
    grants, which holds because all three act on a machine. This one acts on a person. "May
    reboot a PC" must not silently imply "may find out where an employee is."

That decision is load-bearing rather than decorative, so it is defended in three places: here,
in permissions.LOCATE_DEVICE, and in fleet_web.py -- which refuses a hand-rolled `locate_device`
through the generic command endpoint, because that endpoint's gate is `issue_commands` and
accepting one there would route around this capability for the whole helpdesk.

**`require_machine`, never a bare `require`.** Location is one of the two payloads in this
product where a scope leak is a privacy incident rather than an information leak. Every route
below that names a machine checks capability AND scope; the fleet listing filters through
`access.filter_rows`.

**Every ask is audited at security level, with the operator named**, and the device posts its
own notification naming them too. The person holding the device can always see they were
located; the trail can always answer who asked.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
from flask import Blueprint, jsonify, request

import capabilities
import fleet
import location
import permissions
import permissions_web
import refusals
import settings


def create_location_blueprint(db_path, login_required, access):
    """Build the location Blueprint. `login_required` (app.py's session gate) and `access`
    (the permission-group layer) are passed in, for the usual two reasons: no circular import,
    and one source of truth per gate."""
    bp = Blueprint("location", __name__)
    can_view = access.require(permissions.VIEW)

    def _payload(machine):
        """Everything the machine page's Location fold renders, in one answer."""
        return {
            "machine": machine,
            "latest": location.latest_fix(db_path, machine),
            "history": location.history(db_path, machine, limit=20),
            # Read off the command queue, not a request table -- see location.py's docstring
            # for why this feature has no lifecycle of its own.
            "pending": location.open_request(db_path, machine),
            # Presentation only; the POST below re-decides server-side. Sent so the page can
            # render a disabled button with a reason instead of a working one that 403s.
            "can_locate": access.can(permissions.LOCATE_DEVICE),
            # Whether the machine can answer at all (roadmap #23 F.1). A Windows PC reports no
            # capabilities and therefore reads as UNKNOWN here, not as incapable -- the
            # absent-report rule. `supports` is the other half of that asymmetry and is False
            # for a machine that has not said, which is right for a feature only an agent new
            # enough to report it can have.
            "supported": capabilities.supports(db_path, machine,
                                               capabilities.FEATURE_LOCATE),
        }

    # ---------------- Read ----------------
    @bp.route("/api/location/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_location(machine):
        return jsonify(_payload(str(machine).strip())), 200

    @bp.route("/api/location/fleet", methods=["GET"])
    @login_required
    @can_view
    def fleet_locations():
        """The newest fix for every machine this operator can see -- the fleet map's query.

        Scope filtering happens here rather than in SQL because location.py deliberately knows
        nothing about permission groups, the same division fleet.py and wake.py keep. It is
        applied to the RESULT of a single statement rather than by asking per machine: the map
        wants the whole fleet at once, and a query per machine is how a map page becomes the
        slowest thing in the console.
        """
        rows = location.latest_fixes(db_path)
        return jsonify({"fixes": access.filter_rows(rows)}), 200

    # ---------------- Ask ----------------
    @bp.route("/api/location/machines/<machine>", methods=["POST"])
    @login_required
    @access.require_machine(permissions.LOCATE_DEVICE)
    def locate_machine(machine):
        """Ask a device where it is.

        Answers **202 with the queued command**, never a position: the device has to be awake,
        claim the command and take a fix, which is seconds at best and never happens at all for
        a phone that is switched off. Blocking here would hold a request open for the length of
        the device's own timeout and still usually answer "no".

        A machine that has told us it cannot locate is refused by `fleet.create_command` with
        the reason attached -- that is F.1's capability check, and it is why an operator cannot
        queue a locate at a desktop PC and watch it expire.

        A second ask while one is already in flight returns the FIRST one rather than queueing
        another. Two locates racing at one device is not twice as much answer: the device would
        take two fixes seconds apart, file two rows, and the operator would watch one of them
        arrive with no way to tell which.
        """
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        machine = str(machine).strip()
        actor = permissions_web.current_actor()

        existing = location.open_request(db_path, machine)
        if existing is not None:
            return jsonify(_payload(machine)), 202

        try:
            command_id = fleet.create_command(
                db_path, machine=machine, command_type=location.COMMAND_TYPE,
                # The device's own time budget. WHICH provider to use is deliberately not
                # here -- see fleet.LOCATION_COMMANDS.
                params={"timeout_seconds": settings.get_int(
                    db_path, "location.default_timeout_seconds")},
                issued_by=actor,
                ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"))
        except ValueError as e:
            # Covers fleet.UnsupportedCommand (this device cannot locate) and a genuinely bad
            # machine name. Both are 400 with the reason, which for the first is a sentence
            # naming the machine, the command and the platform.
            return refusals.refuse(e)

        # Audited at security level, like issuing any other command -- and here the actor is
        # the point of the row rather than a detail of it. `create_command` already writes its
        # own `issue_command` entry; this one exists so that "who located this device, and
        # when" is answerable by searching for one action rather than by reading command
        # params. The device names the same operator in its own notification.
        fleet.audit(db_path, actor=actor, action="locate_device",
                    level=fleet.LEVEL_SECURITY, target=machine,
                    detail={"command_id": command_id})
        return jsonify(_payload(machine)), 202

    return bp
