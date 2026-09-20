"""Flask HTTP surface for watchdogs (roadmap #20) -- a thin layer over watchdogs.py, in the
same shape as rules_web.py beside it.

**Writing a watchdog takes `manage_rules` AND `issue_commands`, and that pair is the whole
perimeter question this file had to answer.**

`permissions.MANAGE_RULES` says it is deliberately not sufficient on its own to write a rule
that issues COMMANDS: somebody who may raise alerts on a condition has not thereby been handed
a fleet-wide reboot button, and `rules.validate_actions` enforces that. A watchdog restarts a
Windows service, unattended, on every machine its target reaches -- which is `restart_process`
under another name. Gating it on `manage_rules` alone would have handed exactly the button
that rule is there to withhold, to everyone who could already write an alerting rule, on the
day this shipped.

The alternative -- a new `manage_watchdogs` capability -- was rejected on the catalog's own
argument for folding lock and wipe together: it would be one more row in the permissions UI
that nobody would ever grant alone, and it would still have needed the `issue_commands` pair
beside it to be safe. Two capabilities that are always granted together are one capability.

**READING is `view`**, like a rule: a standing instruction that acts on a PC is something
anyone who can see that PC should be able to read. The machine-scoped read is
`access.require_machine(VIEW)` -- capability AND scope -- because it names a machine.

Scope on targets is enforced in the same two places rules_web.py enforces it, and for the same
reason: at save time against the resolved membership, and again at delivery time from the
author scope persisted on the row (`watchdogs.watchdogs_for` -> `rules.scoped_targets`). The
save-time check alone leaks the moment a machine is enrolled into a dynamic target.

Error text is split exactly as rules_web.py splits it: validation messages written out of the
caller's own input go back verbatim, and anything thrown by code we did not write is logged
and answered with one fixed sentence. See `_log_generic`.
"""
import traceback

from flask import Blueprint, jsonify, render_template, request

import fleet
import permissions
import rules
import watchdogs

GENERIC_ERROR = "the hub could not complete that request; see the hub log for details"


def _log_generic(context):
    """Log the exception currently being handled and return the caller-safe message. Same
    split rules_web.py documents: SQLite text carries statement fragments and file paths, and
    this response goes to a browser."""
    print(f"[watchdogs] Failed while {context}:\n{traceback.format_exc()}")
    return GENERIC_ERROR


def create_watchdogs_blueprint(db_path, login_required, access):
    bp = Blueprint("watchdogs", __name__)
    can_view = access.require(permissions.VIEW)
    # Both, not either. See the module docstring -- a watchdog is an unattended service
    # restart, so writing one is the same act as issuing a command and is gated by the same
    # capability, on top of the one that lets somebody write standing instructions at all.
    can_manage = access.require(permissions.MANAGE_RULES)
    can_restart = access.require(permissions.ISSUE_COMMANDS)

    def _actor():
        return access.email() or "unknown"

    def _may_manage():
        return (access.can(permissions.MANAGE_RULES)
                and access.can(permissions.ISSUE_COMMANDS))

    def _extra():
        # The custom fields, derived variables and probes a `field` target selector may name.
        # Read from rules.py rather than re-derived, so a target means the same thing here as
        # it does on a rule.
        return rules.all_extra_variables(db_path)

    def _author_scope():
        """The machines this caller can reach, or None if unrestricted. Persisted on the row
        and re-intersected at delivery time -- rules.scoped_targets says why."""
        machines = (access.current() or {}).get("machines")
        return None if machines is None else sorted(machines)

    def _scope_error(target):
        """Refuse a target that reaches outside the caller's scope, checked on the RESOLVED
        membership rather than on the selectors: "all except two" and an OU selector both
        reach machines the selector text never names."""
        try:
            machines = rules.resolve_targets(db_path, target)
        except Exception:                             # noqa: BLE001
            return _log_generic("resolving a watchdog's targets")
        outside = [m for m in machines if not access.in_scope(m)]
        if outside:
            return ("this watchdog would target machines outside your access: "
                    + ", ".join(sorted(outside)[:5])
                    + (f" (and {len(outside) - 5} more)" if len(outside) > 5 else ""))
        return None

    def _audit(action, watchdog):
        watchdog = watchdog or {}
        fleet.audit(db_path, actor=_actor(), action=action, level=fleet.LEVEL_SECURITY,
                    target=watchdog.get("name"),
                    detail={"watchdog_id": watchdog.get("id"),
                            "service": watchdog.get("service"),
                            "max_restarts": watchdog.get("max_restarts"),
                            "window_seconds": watchdog.get("window_seconds")})

    def _save(watchdog_id=None):
        body = request.get_json(silent=True) or {}
        error, target = rules.validate_target(body.get("target"), _extra())
        if not error:
            error = _scope_error(target)
        if error:
            return jsonify({"error": error}), 400
        error, new_id = watchdogs.save_watchdog(
            db_path, body, watchdog_id=watchdog_id, actor=_actor(),
            author_scope=_author_scope(), extra=_extra())
        if error:
            return jsonify({"error": error}), 400
        saved = watchdogs.get_watchdog(db_path, new_id)
        _audit("watchdog_updated" if watchdog_id else "watchdog_created", saved)
        return jsonify(saved), 200 if watchdog_id else 201

    # ---------------- Authoring ----------------

    @bp.route("/api/watchdogs", methods=["GET"])
    @login_required
    @can_view
    def list_all():
        """Every watchdog, each with a tally of what the fleet last said about it.

        The tally is the point of the page. A watchdog nobody has reported on is
        indistinguishable from a healthy one unless the console can count the silence, so
        `machines` (how many are holding it and reporting) is returned beside the trouble
        counts rather than only the trouble counts.
        """
        out = []
        for watchdog in watchdogs.list_watchdogs(db_path):
            states = [s for s in watchdogs.states_for(db_path, watchdog["id"])
                      if access.in_scope(s["machine"])]
            out.append({
                **watchdog,
                "machines": len(states),
                "restarting": sum(1 for s in states
                                  if s["status"] == watchdogs.STATUS_RESTARTED),
                "trouble": sum(1 for s in states
                               if s["status"] in watchdogs.ESCALATING_STATUSES),
                "missing": sum(1 for s in states if s["status"] == watchdogs.STATUS_MISSING),
            })
        return jsonify({
            "watchdogs": out,
            "can_manage": _may_manage(),
            # Sent so the editor can say WHY its controls are disabled rather than just
            # disabling them: "you can write rules but not issue commands" is a sentence an
            # operator can act on, and a greyed-out form is not.
            "can_manage_rules": access.can(permissions.MANAGE_RULES),
            "can_issue_commands": access.can(permissions.ISSUE_COMMANDS),
            "limits": {
                "grace_seconds": [watchdogs.MIN_GRACE_SECONDS, watchdogs.MAX_GRACE_SECONDS],
                "max_restarts": [watchdogs.MIN_RESTARTS, watchdogs.MAX_RESTARTS],
                "window_seconds": [watchdogs.MIN_WINDOW_SECONDS,
                                   watchdogs.MAX_WINDOW_SECONDS],
                "protected_services": sorted(watchdogs.PROTECTED_SERVICES),
            },
        }), 200

    @bp.route("/api/watchdogs", methods=["POST"])
    @login_required
    @can_manage
    @can_restart
    def create():
        return _save()

    @bp.route("/api/watchdogs/<int:watchdog_id>", methods=["GET"])
    @login_required
    @can_view
    def get_one(watchdog_id):
        watchdog = watchdogs.get_watchdog(db_path, watchdog_id)
        if not watchdog:
            return jsonify({"error": "no such watchdog"}), 404
        states = [s for s in watchdogs.states_for(db_path, watchdog_id)
                  if access.in_scope(s["machine"])]
        return jsonify({**watchdog, "state": states}), 200

    @bp.route("/api/watchdogs/<int:watchdog_id>", methods=["PUT"])
    @login_required
    @can_manage
    @can_restart
    def update(watchdog_id):
        if not watchdogs.get_watchdog(db_path, watchdog_id):
            return jsonify({"error": "no such watchdog"}), 404
        return _save(watchdog_id)

    @bp.route("/api/watchdogs/<int:watchdog_id>", methods=["DELETE"])
    @login_required
    @can_manage
    @can_restart
    def remove(watchdog_id):
        watchdog = watchdogs.get_watchdog(db_path, watchdog_id)
        if not watchdog:
            return jsonify({"error": "no such watchdog"}), 404
        watchdogs.delete_watchdog(db_path, watchdog_id)
        _audit("watchdog_deleted", watchdog)
        return jsonify({"status": "deleted"}), 200

    @bp.route("/api/watchdogs/<int:watchdog_id>/enabled", methods=["PUT"])
    @login_required
    @can_manage
    @can_restart
    def toggle(watchdog_id):
        watchdog = watchdogs.get_watchdog(db_path, watchdog_id)
        if not watchdog:
            return jsonify({"error": "no such watchdog"}), 404
        enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
        watchdogs.set_enabled(db_path, watchdog_id, enabled, actor=_actor())
        _audit("watchdog_enabled" if enabled else "watchdog_disabled", watchdog)
        return jsonify({"status": "ok", "enabled": enabled}), 200

    # ---------------- History ----------------

    @bp.route("/api/watchdogs/events", methods=["GET"])
    @login_required
    @can_view
    def fleet_events():
        """What watchdogs have been doing across the fleet, newest first.

        Scope filtering happens here rather than in the query, the same division wake_web.py
        keeps: watchdogs.py deliberately knows nothing about permission groups.
        """
        rows = watchdogs.list_events(db_path, limit=300)
        return jsonify({"events": [r for r in rows if access.in_scope(r["machine"])]}), 200

    @bp.route("/api/watchdogs/<int:watchdog_id>/events", methods=["GET"])
    @login_required
    @can_view
    def watchdog_events(watchdog_id):
        rows = watchdogs.list_events(db_path, watchdog_id=watchdog_id, limit=300)
        return jsonify({"events": [r for r in rows if access.in_scope(r["machine"])]}), 200

    @bp.route("/api/machines/<machine>/watchdogs", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_watchdogs(machine):
        """What this one PC is watching, for the machine page.

        `require_machine`, not `require`: the route names a machine, so it needs the
        capability AND the scope. See hub/wake_web.py, the canonical example.
        """
        return jsonify({"machine": machine,
                        "watchdogs": watchdogs.machine_states(db_path, machine),
                        "events": watchdogs.list_events(db_path, machine=machine,
                                                        limit=50)}), 200

    # ---------------- Page ----------------

    @bp.route("/watchdogs")
    @login_required
    @can_view
    def watchdogs_page():
        return render_template("watchdogs.html")

    return bp
