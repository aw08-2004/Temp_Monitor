"""Flask HTTP surface for the app-usage ledger (roadmap #23 phase E) -- a thin layer over
usage.py, registered as a Blueprint from app.py.

**One route, one gate, and no fleet-wide view.** Reading a machine's usage is `view` PLUS
machine scope, exactly like its location -- and unlike the app inventory, there is deliberately
no "which apps does the fleet use" endpoint to go with it. The inventory has one because a
policy author needs to pick a package from somewhere. Usage has none because the question it
would answer at fleet scale is "who spends the most time on their phone", and that is not a
question this product should make easy to ask.

**Read-only, like every other thing a device reports about itself.** A write here would be a
way to tell the hub somebody used an app they did not.

The wider argument, and the retention that goes with it, is in usage.py and in SECURITY.MD's
personal-data inventory.
"""
from flask import Blueprint, jsonify, request

import permissions
import settings
import usage


def create_usage_blueprint(db_path, login_required, access):
    """Build the usage Blueprint. `login_required` and `access` are passed in for the usual two
    reasons: no circular import, and one source of truth per gate."""
    bp = Blueprint("usage", __name__)

    @bp.route("/api/usage/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_usage(machine):
        """How long each app was in the foreground, by day.

        `reported_at: null` with no days is the answer for a device that has never reported --
        every Windows PC, every Android agent too old to send the block, and every device where
        usage access was never granted. The last of those is the one worth rendering carefully:
        it is not "this person used nothing", it is a permission somebody has to grant on the
        device, and the console is the only place that can say so.
        """
        name = str(machine).strip()
        # Capped at the retention window rather than at whatever was asked for: there is never
        # anything older to fetch, and an endpoint that accepts `days=3650` invites a caller to
        # believe there might be.
        keep = settings.get_int(db_path, "data.usage_retention_days")
        try:
            wanted = min(int(request.args.get("days", keep)), keep)
        except (TypeError, ValueError):
            wanted = keep

        payload = usage.get_usage(db_path, name, days=max(1, wanted))
        payload["machine"] = name
        payload["retention_days"] = keep
        return jsonify(payload), 200

    return bp
