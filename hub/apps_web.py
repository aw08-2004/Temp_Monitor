"""Flask HTTP surface for the app inventory (roadmap #23 phase D) -- a thin layer over apps.py,
registered as a Blueprint from app.py.

**One gate: `view` plus machine scope.** What is installed on a device is inventory in exactly
the sense its disks and its BIOS settings are, and the same argument applies: an operator who
can see the machine can see what it runs. Making it cost an admin would make "which of these
forty phones still has the old app" a question nobody can answer without one.

It is not a neutral list, though, and that is worth stating rather than assuming: an app list is
about the person carrying the device more than about the device. `require_machine` is used
throughout for that reason -- the same treatment location gets -- and the fleet-wide package
listing is scoped, so an operator sees only packages on machines they already administer.

**Read-only, and it stays that way.** The inventory is what the device says is installed;
policy is a separate thing an operator writes, and it lands in its own pair. A write here would
be a way to tell the hub a device has an app it does not have, which is exactly the mismatch
the `suspended` and `enabled` flags exist to make visible.
"""
from flask import Blueprint, jsonify, request

import apps
import permissions


def create_apps_blueprint(db_path, login_required, access):
    """Build the apps Blueprint. `login_required` (app.py's session gate) and `access` (the
    permission-group layer) are passed in, for the usual two reasons: no circular import, and
    one source of truth per gate."""
    bp = Blueprint("apps", __name__)
    can_view = access.require(permissions.VIEW)

    @bp.route("/api/apps/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_apps(machine):
        """What this device says is installed.

        `reported_at: null` with an empty list is the answer for every machine that has never
        reported -- every Windows PC, and every Android device on an agent too old to send the
        block. The console must render that as "we have not been told" rather than as "this
        device has no apps", which is a state that does not occur.
        """
        name = str(machine).strip()
        payload = apps.get_inventory(db_path, name)
        payload["machine"] = name
        return jsonify(payload), 200

    @bp.route("/api/apps/packages", methods=["GET"])
    @login_required
    @can_view
    def fleet_packages():
        """Every distinct package across the machines this operator can see, with how many
        devices have each.

        What a policy editor picks from, and what answers "how many devices would this actually
        affect" before somebody blocks something. Scoped by intersecting with the caller's
        machine list rather than filtered afterwards: the count beside each package is part of
        the answer, and a count computed over machines the caller cannot see would be a number
        they could not check.
        """
        visible = [m for m in _machines_in_scope()]
        return jsonify({"packages": apps.known_packages(db_path, visible)}), 200

    @bp.route("/api/apps/packages/<package>/machines", methods=["GET"])
    @login_required
    @can_view
    def package_machines(package):
        """Which machines have this package, narrowed to what this operator can see."""
        found = apps.machines_with(db_path, package)
        return jsonify({"package": str(package).strip(),
                        "machines": [m for m in found if access.in_scope(m)]}), 200

    def _machines_in_scope():
        """Every machine name this operator may see, from the apps table itself.

        Derived from the inventory rather than from the fleet roster on purpose: this endpoint
        only ever reports on machines that HAVE an inventory, so asking the roster would mean
        holding a second, larger list to filter against for no extra answer.
        """
        with apps.get_conn(db_path) as conn:
            rows = conn.execute("SELECT DISTINCT machine FROM machine_apps").fetchall()
        return [r["machine"] for r in rows if access.in_scope(r["machine"])]

    return bp
