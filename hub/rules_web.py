"""Flask HTTP surface for the rules engine -- a thin layer over rules.py, in the same shape
as packages_web.py and fleet_web.py.

Two gates, and the difference between them is the point:

  * **Reading** (the variable catalog, the rule list, a machine's custom fields, a rule's
    fire history) is `view`. A rule is fleet configuration; an operator who can see the
    machines should be able to see what standing instructions apply to them.

  * **Writing** is `manage_rules`, and writing a rule that issues COMMANDS additionally
    requires `issue_commands` -- enforced down in rules.validate_actions rather than here,
    so that a follow-up action nested inside a message's on_response map gets exactly the
    same check as a top-level one.

Scope is enforced on TARGETS in two places, and both are needed.

At SAVE time the resolved membership must lie inside the caller's scope, so an operator
cannot name a machine they cannot see. That check alone is not sufficient, because a target
can be DYNAMIC: `{"kind": "all"}` saved by a scoped operator resolves to only their machines
today and to somebody else's the moment one is enrolled. So the author's scope is also
PERSISTED with the rule (`scope_json`) and intersected at evaluation time -- see
rules.scoped_targets. A rule can therefore shrink relative to its author's reach but never
grow past it, and the escalation needs no action from the author to occur, which is exactly
why it cannot be left to the save-time check.

An unrestricted author stores NULL and stays fully dynamic: somebody who can already see the
whole fleet gains nothing from a pinned list, and pinning one would stop legitimate
fleet-wide rules ever covering a new PC.

Bodies are JSON everywhere, which is load-bearing CSRF protection -- see fleet_web.py's
module docstring, which applies here verbatim. There are no multipart endpoints in this file
and there should not be.

ERROR TEXT IS SPLIT IN TWO, deliberately. Validation messages produced by rules.py ("unknown
variable: sys.uptim", "operator '>' cannot be used with sys.online") are written by us out of
the caller's own input, and they go back verbatim -- an expression editor whose errors all
read "invalid expression" is one nobody can use. Anything thrown by code we did not write
(SQLite, the resolver, the OS) does NOT: it is logged and answered with a fixed sentence, via
_log_generic. Exception text from those layers carries statement fragments, file paths and
schema details, and this response goes to a browser.
"""
import traceback

from flask import Blueprint, jsonify, render_template, request

import alerts
import fleet
import i18n
import permissions
import rules


def _lang():
    return i18n.current()


# The one sentence any unexpected failure in this module answers with. Fixed text, so no
# caller can learn anything about the hub's internals from the shape of a failure.
GENERIC_ERROR = "the hub could not complete that request; see the hub log for details"


def _log_generic(context):
    """Log the exception currently being handled, and return the caller-safe message.

    Called from inside an `except` block. The traceback goes to the hub's log -- the same
    place every other scheduler and blueprint prints its failures -- and the return value is
    a constant. Splitting it this way means adding a new broad `except` here cannot
    accidentally start leaking, because there is nothing to interpolate.
    """
    print(f"[rules] Failed while {context}:\n{traceback.format_exc()}")
    return GENERIC_ERROR


def create_rules_blueprint(db_path, login_required, access, resolve_vars, rules_config):
    """Build the rules Blueprint.

    `resolve_vars(machine)` and `rules_config()` are passed in from app.py for the same
    reason the evaluator takes them: rules.py stays free of Flask, settings and the sensor
    parsing that lives in app, and this layer is where those get injected.
    """
    bp = Blueprint("rules", __name__)
    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_RULES)

    def _extra():
        return rules.all_extra_variables(db_path)

    def _may_issue_commands():
        return access.can(permissions.ISSUE_COMMANDS)

    def _actor():
        return access.email() or "unknown"

    def _author_scope():
        """The machines this caller can reach, or None if they are unrestricted.

        Stored on the rule and intersected at evaluation time. `permissions["machines"]` is
        already the fully-resolved set (AD OU scopes included), and None there means
        unrestricted -- the same convention this returns, so the two cannot drift.
        """
        machines = (access.current() or {}).get("machines")
        return None if machines is None else sorted(machines)

    def _catalog_entry(var, value=None):
        key = f"{rules.VARIABLE_TEXT_KEY}.{var.name}"
        label = i18n.translate(f"{key}.label", _lang())
        description = i18n.translate(f"{key}.description", _lang())
        entry = {
            "name": var.name,
            "kind": var.kind,
            "group": var.group,
            "unit": var.unit,
            "max_age_seconds": var.max_age,
            # A catalog entry with no translation falls back to its own name rather than
            # showing the raw key. Dynamic families (disk.<letter>.*, field.*) genuinely
            # cannot all be in the catalog, so a missing entry here is normal, not a bug.
            "label": var.name if label == f"{key}.label" else label,
            "description": "" if description == f"{key}.description" else description,
            "operators": list(rules.OPERATORS_BY_KIND.get(var.kind, ())),
        }
        if value is not None:
            entry["known"] = value.known
            entry["value"] = None if not value.known else value.value
            entry["age_seconds"] = value.age_seconds
        return entry

    # ---------------- The variable catalog ----------------

    @bp.route("/api/rules/variables", methods=["GET"])
    @login_required
    @can_view
    def list_variables():
        """The fleet-wide catalog: everything a condition may reference.

        Per-volume `disk.<letter>.*` entries are NOT here -- which drive letters exist is a
        per-machine fact, and offering `disk.q.used_pct` fleet-wide because one machine has a
        Q: would be noise on the other four hundred. The disk AGGREGATES are here, and they
        are what most rules should use anyway.
        """
        return jsonify({
            "variables": [_catalog_entry(v) for v in rules.catalog(db_path)],
            "operators": {op: i18n.translate(f"{rules.OPERATOR_TEXT_KEY}.{op}.label", _lang())
                          for op in rules.ALL_OPERATORS},
            "groups": list(dict.fromkeys(v.group for v in rules.catalog(db_path))),
        }), 200

    @bp.route("/api/rules/variables/<machine>", methods=["GET"])
    @login_required
    @can_view
    def list_variables_for(machine):
        """The catalog WITH this machine's current values and ages.

        This is the endpoint that makes the feature usable: an operator writing a condition
        can see what the variable actually reads on a real PC right now, including which ones
        are unknown and how stale the rest are. Building a rule blind against a name list is
        how you end up with a condition that can never be true.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        resolved = resolve_vars(machine)
        entries = []
        for name in sorted(resolved):
            var = rules.lookup_variable(name, _extra())
            if var is None:
                continue
            entries.append(_catalog_entry(var, resolved[name]))
        return jsonify({"machine": machine, "variables": entries}), 200

    # ---------------- Custom fields ----------------

    @bp.route("/api/rules/fields", methods=["GET"])
    @login_required
    @can_view
    def list_custom_fields():
        return jsonify({"fields": rules.list_fields(db_path),
                        "kinds": list(rules.VALUE_KINDS)}), 200

    @bp.route("/api/rules/fields", methods=["POST"])
    @login_required
    @can_manage
    def create_custom_field():
        body = request.get_json(silent=True) or {}
        error, field = rules.save_field(
            db_path, body.get("name"), body.get("label"), body.get("kind"),
            choices=body.get("choices"), default_value=body.get("default_value"),
            description=body.get("description") or "", actor=_actor(),
        )
        if error:
            return jsonify({"error": error}), 400
        return jsonify(field), 201

    @bp.route("/api/rules/fields/<name>", methods=["PUT"])
    @login_required
    @can_manage
    def update_custom_field(name):
        body = request.get_json(silent=True) or {}
        existing = rules.get_field(db_path, name)
        if not existing:
            return jsonify({"error": "no such field"}), 404
        error, field = rules.save_field(
            db_path, name, body.get("label", existing["label"]),
            body.get("kind", existing["kind"]),
            choices=body.get("choices", existing["choices"]),
            default_value=body.get("default_value", existing["default_value"]),
            description=body.get("description", existing.get("description") or ""),
            actor=_actor(),
        )
        if error:
            return jsonify({"error": error}), 400
        return jsonify(field), 200

    @bp.route("/api/rules/fields/<name>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_custom_field(name):
        if not rules.delete_field(db_path, name):
            return jsonify({"error": "no such field"}), 404
        return jsonify({"status": "deleted"}), 200

    @bp.route("/api/rules/fields/<name>/values", methods=["POST"])
    @login_required
    @can_manage
    def set_field_bulk(name):
        """Set one field across a selection -- the primary editing path.

        Every machine in the selection is scope-checked BEFORE anything is written, and one
        out-of-scope name refuses the whole request. Same reasoning as packages_web's
        deployment targets: a bulk edit that quietly applied to nine of the ten machines you
        picked is worse than one that failed.
        """
        body = request.get_json(silent=True) or {}
        machines = body.get("machines") or []
        if not isinstance(machines, list) or not machines:
            return jsonify({"error": "no machines selected"}), 400
        out_of_scope = [m for m in machines if not access.in_scope(m)]
        if out_of_scope:
            return jsonify({"error": "You do not have access to: "
                                     + ", ".join(sorted(out_of_scope)[:5])}), 403
        error, count = rules.set_machine_field_bulk(db_path, machines, name,
                                                    body.get("value"), actor=_actor())
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"status": "ok", "updated": count}), 200

    @bp.route("/api/machines/<machine>/fields", methods=["GET"])
    @login_required
    @can_view
    def get_machine_field_values(machine):
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        stored = rules.get_machine_fields(db_path, machine)
        out = []
        for field in rules.list_fields(db_path):
            raw = stored.get(field["name"])
            _, value = rules.coerce_field_value(
                field["kind"],
                raw if raw not in (None, "") else field.get("default_value"),
                field["choices"])
            out.append({**field, "value": value, "is_default": raw in (None, "")})
        return jsonify({"machine": machine, "fields": out}), 200

    @bp.route("/api/machines/<machine>/fields", methods=["PUT"])
    @login_required
    @can_manage
    def set_machine_field_values(machine):
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        body = request.get_json(silent=True) or {}
        values = body.get("values")
        if not isinstance(values, dict):
            return jsonify({"error": "values must be an object"}), 400
        for name, value in values.items():
            error, _ = rules.set_machine_field(db_path, machine, name, value, actor=_actor())
            if error:
                return jsonify({"error": f"{name}: {error}"}), 400
        return jsonify({"status": "ok"}), 200

    # ---------------- Derived variables ----------------

    @bp.route("/api/rules/derived", methods=["GET"])
    @login_required
    @can_view
    def list_derived_variables():
        return jsonify({"derived": rules.list_derived(db_path)}), 200

    @bp.route("/api/rules/derived", methods=["POST"])
    @login_required
    @can_manage
    def save_derived_variable():
        body = request.get_json(silent=True) or {}
        error, derived = rules.save_derived(
            db_path, body.get("name"), body.get("expression"),
            description=body.get("description") or "", unit=body.get("unit") or "",
            actor=_actor())
        if error:
            return jsonify({"error": error}), 400
        return jsonify(derived), 200

    @bp.route("/api/rules/derived/<name>", methods=["DELETE"])
    @login_required
    @can_manage
    def remove_derived_variable(name):
        error, deleted = rules.delete_derived(db_path, name)
        if error:
            return jsonify({"error": error}), 400
        if not deleted:
            return jsonify({"error": "no such calculation"}), 404
        return jsonify({"status": "deleted"}), 200

    # ---------------- Probes ----------------

    @bp.route("/api/rules/probes", methods=["GET"])
    @login_required
    @can_view
    def list_all_probes():
        return jsonify({"probes": rules.list_probes(db_path),
                        "kinds": list(rules.PROBE_KINDS),
                        "script_allowed": rules_config().get("probes_allow_script", False)}), 200

    @bp.route("/api/rules/probes", methods=["POST"])
    @login_required
    @can_manage
    def save_one_probe():
        body = request.get_json(silent=True) or {}
        error, probe = rules.save_probe(
            db_path, body.get("name"), body.get("kind"), body.get("spec"),
            value_kind=body.get("value_kind") or rules.KIND_TEXT,
            interval_seconds=body.get("interval_seconds", 3600),
            timeout_seconds=body.get("timeout_seconds", 30),
            description=body.get("description") or "",
            enabled=bool(body.get("enabled", True)),
            # The script gate is read from settings HERE rather than trusted from the body:
            # a probe kind that runs arbitrary code fleet-wide must not be enableable by the
            # request that wants to use it.
            allow_script=rules_config().get("probes_allow_script", False),
            actor=_actor())
        if error:
            return jsonify({"error": error}), 400
        return jsonify(probe), 200

    @bp.route("/api/rules/probes/<name>", methods=["DELETE"])
    @login_required
    @can_manage
    def remove_probe(name):
        if not rules.delete_probe(db_path, name):
            return jsonify({"error": "no such probe"}), 404
        return jsonify({"status": "deleted"}), 200

    # ---------------- Targets and preview ----------------

    def _scope_error(target):
        """Refuse a target that reaches outside the caller's scope. Returns an error or None.

        Checked on the RESOLVED membership rather than on the selectors, because "all except
        two" and an OU selector both reach machines the selector text never names.
        """
        try:
            machines = rules.resolve_targets(db_path, target)
        except Exception:                             # noqa: BLE001
            # Deliberately NOT the exception's text. This catches anything the target
            # resolver can throw -- SQLite errors carry statement fragments and file paths,
            # and this response goes to a browser. The detail belongs in the hub's log,
            # where an operator debugging it can see it and a caller cannot.
            return _log_generic("resolving a rule's targets")
        outside = [m for m in machines if not access.in_scope(m)]
        if outside:
            return ("this rule would target machines outside your access: "
                    + ", ".join(sorted(outside)[:5])
                    + (f" (and {len(outside) - 5} more)" if len(outside) > 5 else ""))
        return None

    @bp.route("/api/rules/targets", methods=["POST"])
    @login_required
    @can_view
    def preview_targets():
        """Which machines a target spec currently addresses. Drives the live membership
        count in the editor, so "all except one" is verifiable before saving rather than
        after firing."""
        body = request.get_json(silent=True) or {}
        error, target = rules.validate_target(body.get("target"), _extra())
        if error:
            return jsonify({"error": error}), 400
        machines = [m for m in rules.resolve_targets(db_path, target) if access.in_scope(m)]
        return jsonify({"count": len(machines), "machines": machines[:500],
                        "truncated": len(machines) > 500}), 200

    @bp.route("/api/rules/preview", methods=["POST"])
    @login_required
    @can_view
    def preview_condition():
        """Evaluate a condition across a target set without saving anything.

        Returns true/false/unknown per machine WITH the resolved operand values, because a
        bare `false` leaves an operator guessing which clause was responsible -- and because
        the most common mistake here is a condition that is unknown everywhere, which looks
        identical to one that is merely false unless you say so.
        """
        body = request.get_json(silent=True) or {}
        extra = _extra()

        if body.get("condition_text"):
            error, condition = rules.parse_expression(body["condition_text"], extra)
        else:
            error, condition = rules.validate_condition(body.get("condition"), extra)
        if error:
            return jsonify({"error": error}), 400

        error, target = rules.validate_target(
            body.get("target") or {"include": [{"kind": "all"}]}, extra)
        if error:
            return jsonify({"error": error}), 400

        machines = [m for m in rules.resolve_targets(db_path, target) if access.in_scope(m)]
        limit = max(1, min(200, int(body.get("limit") or 50)))
        results, tally = [], {"true": 0, "false": 0, "unknown": 0}
        for machine in machines:
            try:
                resolved = resolve_vars(machine)
                outcome = rules._result_name(rules.evaluate(condition, resolved))
            except Exception:                         # noqa: BLE001
                # Same reasoning as _scope_error: one machine failing to resolve must not
                # put a raw exception into a preview an operator is reading, and must not
                # abandon the other three hundred machines either.
                results.append({"machine": machine, "result": "unknown",
                                "error": _log_generic(f"previewing {machine}")})
                tally["unknown"] += 1
                continue
            tally[outcome] += 1
            if len(results) < limit:
                results.append({"machine": machine, "result": outcome,
                                "detail": rules.explain(condition, resolved)})
        return jsonify({
            "condition": condition,
            "condition_text": rules.format_expression(condition),
            "variables": rules.condition_variables(condition),
            "matched": tally["true"], "tally": tally,
            "targeted": len(machines), "results": results,
        }), 200

    # ---------------- Rules ----------------

    @bp.route("/api/rules", methods=["GET"])
    @login_required
    @can_view
    def list_all_rules():
        config = rules_config()
        out = []
        for rule in rules.list_rules(db_path):
            state = rules.get_rule_state(db_path, rule["id"])
            allowed, reason = rules.rule_can_run(rule["actions"],
                                                 config["command_actions_enabled"])
            out.append({**rule,
                        "matching": sum(1 for s in state.values() if s.get("firing")),
                        "blocked": None if allowed else reason})
        return jsonify({"rules": out,
                        "actions_enabled": config["actions_enabled"],
                        "command_actions_enabled": config["command_actions_enabled"],
                        "can_manage": access.can(permissions.MANAGE_RULES),
                        "can_issue_commands": _may_issue_commands()}), 200

    @bp.route("/api/rules", methods=["POST"])
    @login_required
    @can_manage
    def create_rule():
        body = request.get_json(silent=True) or {}
        config = rules_config()
        error, target = rules.validate_target(body.get("target"), _extra())
        if not error:
            error = _scope_error(target)
        if error:
            return jsonify({"error": error}), 400
        error, rule = rules.save_rule(
            db_path, body, actor=_actor(), extra=_extra(), author_scope=_author_scope(),
            allow_command=_may_issue_commands(),
            max_targets_cap=config["max_targets_per_tick"],
            command_cooldown_floor=config["command_cooldown_floor_seconds"],
        )
        if error:
            return jsonify({"error": error}), 400
        _audit("rule_created", rule)
        return jsonify(rule), 201

    @bp.route("/api/rules/<int:rule_id>", methods=["GET"])
    @login_required
    @can_view
    def get_one_rule(rule_id):
        rule = rules.get_rule(db_path, rule_id)
        if not rule:
            return jsonify({"error": "no such rule"}), 404
        state = rules.get_rule_state(db_path, rule_id)
        return jsonify({**rule,
                        "state": [{"machine": m, **s} for m, s in sorted(state.items())
                                  if access.in_scope(m)]}), 200

    @bp.route("/api/rules/<int:rule_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update_rule(rule_id):
        if not rules.get_rule(db_path, rule_id):
            return jsonify({"error": "no such rule"}), 404
        body = request.get_json(silent=True) or {}
        config = rules_config()
        error, target = rules.validate_target(body.get("target"), _extra())
        if not error:
            error = _scope_error(target)
        if error:
            return jsonify({"error": error}), 400
        error, rule = rules.save_rule(
            db_path, body, rule_id=rule_id, actor=_actor(), extra=_extra(),
            author_scope=_author_scope(),
            allow_command=_may_issue_commands(),
            max_targets_cap=config["max_targets_per_tick"],
            command_cooldown_floor=config["command_cooldown_floor_seconds"],
        )
        if error:
            return jsonify({"error": error}), 400
        _audit("rule_updated", rule)
        return jsonify(rule), 200

    @bp.route("/api/rules/<int:rule_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def remove_rule(rule_id):
        rule = rules.get_rule(db_path, rule_id)
        if not rule:
            return jsonify({"error": "no such rule"}), 404
        # Resolve the alerts it raised first: an alert whose rule no longer exists names a
        # condition nobody can look up and offers no way to act on it.
        alerts.resolve_for_rule(db_path, rule_id)
        rules.delete_rule(db_path, rule_id)
        _audit("rule_deleted", rule)
        return jsonify({"status": "deleted"}), 200

    @bp.route("/api/rules/<int:rule_id>/enabled", methods=["PUT"])
    @login_required
    @can_manage
    def toggle_rule(rule_id):
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        if not rules.set_rule_enabled(db_path, rule_id, enabled, actor=_actor()):
            return jsonify({"error": "no such rule"}), 404
        if not enabled:
            alerts.resolve_for_rule(db_path, rule_id)
        rule = rules.get_rule(db_path, rule_id)
        _audit("rule_enabled" if enabled else "rule_disabled", rule)
        return jsonify(rule), 200

    @bp.route("/api/rules/<int:rule_id>/fires", methods=["GET"])
    @login_required
    @can_view
    def rule_history(rule_id):
        try:
            limit = max(1, min(500, int(request.args.get("limit") or 100)))
        except (TypeError, ValueError):
            limit = 100
        fires = rules.list_fires(db_path, rule_id, limit=limit)
        return jsonify({"fires": [f for f in fires if access.in_scope(f["machine"])]}), 200

    @bp.route("/api/machines/<machine>/rules", methods=["GET"])
    @login_required
    @can_view
    def rules_for_machine(machine):
        """Which rules currently apply to one machine, and what they have done to it.

        The machine page reads this. Note it is strictly a READ -- rules are authored
        fleet-wide against a target set, never per machine, so there is deliberately no
        endpoint here that creates a rule for one PC.
        """
        if not access.in_scope(machine):
            return jsonify({"error": "You do not have access to that machine."}), 403
        applies = []
        for rule in rules.list_rules(db_path):
            targets = rules.resolve_targets(db_path, rule["target"])
            if machine not in targets:
                continue
            state = rules.get_rule_state(db_path, rule["id"]).get(machine) or {}
            applies.append({"id": rule["id"], "name": rule["name"],
                            "enabled": rule["enabled"],
                            "condition_text": rule["condition_text"],
                            "firing": bool(state.get("firing")),
                            "last_fired_at": state.get("last_fired_at"),
                            "snoozed_until": state.get("snoozed_until")})
        return jsonify({"machine": machine, "rules": applies,
                        "fires": rules.list_fires(db_path, machine=machine, limit=25)}), 200

    # ---------------- Page ----------------

    @bp.route("/rules")
    @login_required
    @can_view
    def rules_page():
        return render_template("rules.html")

    # ---------------- helpers ----------------

    def _audit(action, rule):
        rule = rule or {}
        fleet.audit(db_path, actor=_actor(), action=action, level=fleet.LEVEL_SECURITY,
                    target=rule.get("name"),
                    detail={"rule_id": rule.get("id"),
                            "condition": rule.get("condition_text"),
                            "actions": [a.get("type") for a in rule.get("actions") or []]})

    return bp
