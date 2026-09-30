"""Flask HTTP surface for the installed-software inventory (roadmap #25 B) -- a thin layer
over software.py, registered as a Blueprint from app.py.

**One gate, `view` + scope, and no new capability.** What is installed on a PC is inventory in
exactly the sense its model and its BIOS version are; the Firmware and Network tabs and
Android's Apps card are all readable on `view`, and making this one cost more would make a
licence question cost more than walking round the office. What is deliberately NOT here is any
way to change what is installed -- that is `deploy_packages`, on its own page.

**The fleet catalog is scoped on the RESULT, never trusted from the query.** software.catalog
takes the list of machines to count over; this layer builds it from what the caller can see,
so "how many PCs have TeamViewer" answers for the operator's scope and not for the fleet. An
operator whose scope is empty gets an empty catalog, never the unscoped one -- software.catalog
reads an empty list as "nothing", and None only ever comes from an unrestricted caller.
"""
from flask import Blueprint, jsonify, request

import permissions
import software


def create_software_blueprint(db_path, login_required, access):
    bp = Blueprint("software", __name__)
    can_view = access.require(permissions.VIEW)

    def _visible_scope():
        """None for an unrestricted caller, else the visible machines that have reported."""
        if access.machine_filter() is None:
            return None
        with software.get_conn(db_path) as conn:
            names = [r["machine"] for r in conn.execute(
                "SELECT machine FROM machine_software_state ORDER BY machine")]
        return access.filter_machines(names)

    @bp.route("/api/software/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_software(machine):
        return jsonify(software.get_inventory(db_path, str(machine).strip())), 200

    @bp.route("/api/software/catalog", methods=["GET"])
    @login_required
    @can_view
    def fleet_catalog():
        query = (request.args.get("q") or "").strip()[:200]
        rows = software.catalog(db_path, machines=_visible_scope(), query=query)
        return jsonify({"catalog": rows}), 200

    @bp.route("/api/software/catalog/machines", methods=["GET"])
    @login_required
    @can_view
    def catalog_machines():
        """Which visible machines carry one product (optionally one exact version). The click
        behind a catalog row's count."""
        name = (request.args.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        version = request.args.get("version")
        machines = access.filter_machines(software.machines_with(db_path, name, version))
        return jsonify({"machines": machines}), 200

    return bp
