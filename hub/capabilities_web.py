"""Flask HTTP surface for machine capabilities (roadmap #23) -- a thin layer over
capabilities.py, registered as a Blueprint from app.py.

**Read-only, and it will stay that way.** Nothing here writes: a capability is something a
machine reports about itself on the heartbeat, and an operator override would be a way to tell
the hub a phone can run a script. The one honest answer to "this machine cannot do that" is to
change the machine, not the record of it.

**One gate: `view` plus machine scope.** What a machine is able to do is inventory in exactly
the sense its model, its disks and its NIC list are -- the same call bios_web.py and wake_web.py
draw. It is also the input the console needs to decide which tabs to render at all, so putting
it behind anything narrower than the gate on the page itself would leave an operator looking at
a page whose buttons it could not decide about.

The fleet listing exists because the per-machine route cannot answer the question the Inventory
filter asks. `machine_detail` carries a machine's own platform, so a page needs no extra call;
"which of these forty machines are phones" is a different question, and asking it forty times
over is how a list view becomes the slowest page in the console.
"""
from flask import Blueprint, jsonify

import capabilities
import permissions


def create_capabilities_blueprint(db_path, login_required, access):
    """Build the capabilities Blueprint. `login_required` (app.py's session gate) and `access`
    (the permission-group layer) are both passed in, for the same reasons as everywhere else:
    no circular import, and one source of truth per gate."""
    bp = Blueprint("capabilities", __name__)
    can_view = access.require(permissions.VIEW)

    @bp.route("/api/capabilities/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_capabilities(machine):
        """What one machine reports it can do.

        `reported: false` is the answer for every Windows agent in the field, and the console
        must render it as "we have not been told" rather than as "it cannot do anything" --
        that is the absent-report rule (see capabilities.py), and inverting it here would hide
        every button on every PC in the fleet.
        """
        name = str(machine).strip()
        payload = capabilities.get_capabilities(db_path, name)
        payload["machine"] = name
        payload["reported"] = payload["reported_at"] is not None
        return jsonify(payload), 200

    @bp.route("/api/capabilities/platforms", methods=["GET"])
    @login_required
    @can_view
    def fleet_platforms():
        """`{machine: platform}` across the fleet, narrowed to what this operator can see.

        Scope filtering happens here rather than in the query because capabilities.py knows
        nothing about permission groups -- the same division fleet.py, wake.py and packages.py
        keep. A machine absent from the mapping has not reported a platform; the console shows
        that as unknown, never as Windows.
        """
        found = capabilities.platforms_for(db_path)
        return jsonify({
            "platforms": {machine: platform for machine, platform in found.items()
                          if access.in_scope(machine)},
            # Served rather than hardcoded in the console for the same reason the settings
            # registry and the capability list are: a platform added here must not need a
            # second edit in JavaScript to become visible.
            "known": list(capabilities.PLATFORMS),
        }), 200

    return bp
