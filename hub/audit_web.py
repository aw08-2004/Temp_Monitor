"""Flask surface for the Audit Log tab -- the read path over fleet.audit_log.

The hub has written an audit line for every command, enrollment, permission change and
key operation since fleet management shipped, but until now there was no way to READ it
short of opening the SQLite file. With command signing gone, that trail IS the
accountability control (see fleet.py's module docstring), so it has to be visible to the
people who are accountable.

Two capabilities, not one:

  * VIEW_AUDIT_LOG opens the tab and returns info + notice rows.
  * VIEW_SECURITY_AUDIT widens the same read to security-level rows.

The level perimeter is computed HERE from the session's capabilities and handed to
fleet.list_audit, which applies it in SQL. A `level` query param can only NARROW that set,
never widen it -- so asking for security rows without the capability returns an empty page
rather than a 403, and no security row ever reaches the client's JSON. Filtering in the
page's JavaScript would be a leak, not a filter.

Unlike almost every other read surface in this codebase, nothing here is machine-scoped.
That is deliberate: most audit actions have no machine dimension at all (settings, users,
packages, backup keys), so scoping on `target` would hide most of the log while leaking
inconsistently on the rest. The capability is the whole perimeter.

The CSRF note in permissions_web.py applies by construction: every route here is GET and
nothing mutates.
"""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, request

import fleet
import permissions

# Fetch sizes for one page of the tab. The cap matches fleet.list_audit's own clamp.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# How many distinct actors the filter's datalist offers. A suggestion list, not a directory.
MAX_ACTORS = 200


def _parse_bound(value, end_of_day=False):
    """`YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS` -> epoch seconds. Raises ValueError on junk.

    A bare date as the upper bound means the whole of that day: an operator who types the
    same date in both boxes means "that day", not "an empty range at midnight".
    """
    cleaned = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if end_of_day and fmt == "%Y-%m-%d":
            parsed = parsed + timedelta(days=1) - timedelta(seconds=1)
        try:
            return int(parsed.timestamp())
        except (OSError, OverflowError, ValueError):
            # A naive .timestamp() goes through the platform's local-time conversion, which
            # on Windows refuses anything before 1970. An operator typing 1969 into a date
            # box deserves an empty page, not a 500, so fall back to fixed-offset
            # arithmetic -- an hour out across a DST boundary, on a date with no audit
            # rows in it either way.
            offset = datetime.now().astimezone().utcoffset() or timedelta(0)
            return int((parsed - datetime(1970, 1, 1) - offset).total_seconds())
    raise ValueError("Invalid date format; use YYYY-MM-DD.")


def _arg(name):
    return (request.args.get(name) or "").strip() or None


def _int_arg(name):
    raw = _arg(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def create_audit_blueprint(db_path, login_required, access):
    """Read-only views over the audit trail. `login_required` is app.py's session gate;
    `access` is the Access instance each route is additionally gated by."""
    bp = Blueprint("audit", __name__)
    view = access.require(permissions.VIEW_AUDIT_LOG)

    def _allowed_levels():
        """The levels this caller may read, recomputed per request. Never trusts input."""
        if access.can(permissions.VIEW_SECURITY_AUDIT):
            return set(fleet.AUDIT_LEVELS)
        return {fleet.LEVEL_INFO, fleet.LEVEL_NOTICE}

    @bp.route("/audit")
    @login_required
    @view
    def audit_page():
        return render_template("audit.html")

    @bp.route("/api/audit", methods=["GET"])
    @login_required
    @view
    def list_audit_route():
        allowed = _allowed_levels()
        # A requested level NARROWS the perimeter; it can never widen it. An unknown level
        # name intersects to nothing, which is the honest answer to a nonsense filter.
        requested = _arg("level")
        levels = allowed & {requested} if requested else allowed

        try:
            since = _parse_bound(request.args["from"]) if _arg("from") else None
            until = _parse_bound(request.args["to"], end_of_day=True) if _arg("to") else None
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        limit = _int_arg("limit")
        page = fleet.list_audit(
            db_path,
            q=_arg("q"),
            actor=_arg("actor"),
            action=_arg("action"),
            since=since,
            until=until,
            levels=levels,
            before_ts=_int_arg("before_ts"),
            before_id=_int_arg("before_id"),
            limit=limit if limit is not None else DEFAULT_PAGE_SIZE,
        )
        # The page renders its own level filter from `levels`, so it never has to guess
        # whether the security tier exists for this operator.
        page["can_view_security"] = access.can(permissions.VIEW_SECURITY_AUDIT)
        page["levels"] = [lv for lv in fleet.AUDIT_LEVELS if lv in allowed]
        return jsonify(page), 200

    @bp.route("/api/audit/actors", methods=["GET"])
    @login_required
    @view
    def list_audit_actors_route():
        # Same perimeter as the entries: an actor who appears only in security rows must
        # not be enumerable by someone who cannot read those rows.
        actors = fleet.list_audit_actors(db_path, levels=_allowed_levels(), limit=MAX_ACTORS)
        return jsonify({"actors": actors}), 200

    return bp
