"""Flask HTTP surface for security posture (roadmap #25 D) -- a thin layer over posture.py,
registered as a Blueprint from app.py.

**One gate, `view` + scope, and no new capability.** A posture is inventory in the sense a
BIOS version is: the Firmware tab, the encryption card and the device sheet are all readable
on `view`, and a compliance question costing more than walking round the office would only
send people round the office. The one thing a posture names that looks sensitive -- who is a
local administrator -- is group membership any domain user can already read from the
directory, and it is exactly the fact a CIS 5.4 review needs in front of whoever does the
review. Nothing here changes a machine.

**The fleet summary is scoped on the RESULT, never trusted from the query**, the
software_web.py rule: the machine list is built from what the caller can see, so "how many
PCs have the firewall off" answers for the operator's scope and not for the fleet.
"""
from flask import Blueprint, jsonify

import bitlocker
import permissions
import posture


def create_posture_blueprint(db_path, login_required, access):
    bp = Blueprint("posture", __name__)
    can_view = access.require(permissions.VIEW)

    def _encryption(machine):
        return bitlocker.get_inventory(db_path, machine)

    @bp.route("/api/posture/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_posture(machine):
        machine = str(machine).strip()
        stored = posture.get_posture(db_path, machine)
        checks = None
        if stored["posture"] is not None:
            checks = posture.evaluate(stored["posture"],
                                      posture.thresholds_from_settings(db_path),
                                      _encryption(machine))
        return jsonify({"machine": machine, "reported_at": stored["reported_at"],
                        "posture": stored["posture"], "checks": checks,
                        "counts": posture.counts(checks) if checks else None}), 200

    @bp.route("/api/posture/fleet", methods=["GET"])
    @login_required
    @can_view
    def fleet_posture():
        with posture.get_conn(db_path) as conn:
            names = [r["machine"] for r in conn.execute(
                "SELECT machine FROM machine_info ORDER BY machine COLLATE NOCASE")]
        visible = access.filter_machines(names)
        summary = posture.fleet_summary(db_path, visible,
                                        posture.thresholds_from_settings(db_path), _encryption)
        return jsonify(summary), 200

    return bp
