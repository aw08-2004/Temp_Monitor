"""Flask HTTP surface for the Alert Brain (roadmap #17) -- a thin layer over correlate.py,
registered as a Blueprint from app.py.

**Three gates, and none of them is new.**

  * **Reading bundles** is `view`, scoped. A bundle is the alert list regrouped, so it can
    reveal nothing `/api/alerts` does not -- and it is scoped the same way, by filtering the
    alert rows BEFORE they are bundled rather than by filtering bundles afterwards. That
    ordering is the whole scope story here: bundle a scoped operator's alerts together with
    somebody else's and the hostname of a machine outside their reach appears in the
    membership, in the cause, and in the recommendation written about it.
  * **Asking for a recommendation** is `manage_rules` + machine scope. Not a new "ai"
    capability: ROADMAP.MD #8 is explicit that a second authorization model is the thing to
    avoid, and ai_web.py already settled that drafting through the provider is `manage_rules`.
    This spends the same provider budget and produces text the console presents with the same
    authority, so it takes the same capability.
  * **Drafting the suggested script into the library** is `issue_commands` + machine scope,
    which is what `POST /api/rules/scripts` already requires. A script that arrived from a
    model is still a script body that will run as SYSTEM if somebody later enables it, and
    scripts.py's module docstring spells out why writing one is gated on the capability to
    run arbitrary code rather than on the capability to edit rules. **Reading** a suggested
    body takes the same capability, so `_readable` strips it from every answer to a caller
    without it -- scripts.py splits list-versus-read on exactly this line, and a body that has
    not reached the library yet is still a body headed there.

**A bundle is addressed as `<machine>/<anchor>`, never by its key.** The key carries a `#`,
which is a URL fragment, and -- more to the point -- putting the machine in a route parameter
is what lets `access.require_machine` do the scope check instead of each route remembering to
call `in_scope()` by hand. The anchor is the bundle's lowest member alert id; the server
re-derives the bundle from the live alert list on every call rather than trusting a
membership the caller sent, so a caller cannot ask for a recommendation about a set of alerts
it assembled itself.

**The list endpoint does not compute anomalies.** A baseline is a scan of two weeks of
`readings` per machine, and the Alerts tab polls; doing it per bundle per poll would make the
alert badge the most expensive query in the hub. Anomalies come back from the single-bundle
read and from the machine page, both of which are opened deliberately by a person.

Bodies are JSON, which is load-bearing CSRF protection -- see fleet_web.py's module docstring,
which applies here verbatim.
"""
import traceback

from flask import Blueprint, jsonify, request

import alerts
import correlate
import fleet
import permissions
import permissions_web
import rules

# One sentence for anything unexpected, copied from ai_web.py and for the same reason: with
# nothing interpolated there is nothing a caller can learn from the shape of a failure.
GENERIC_ERROR = "the hub could not complete that request; see the hub log for details"


def _log_generic(context):
    print(f"[correlate] {context}:\n{traceback.format_exc()}")
    return GENERIC_ERROR


def create_correlate_blueprint(db_path, login_required, access, ai_config, api_key=None):
    """Build the correlation Blueprint.

    `ai_config` is app.py's `_ai_config()` callable, read fresh per request so the off switch
    takes effect on the next call rather than at the next restart -- the same seam ai_web.py
    uses, and deliberately not a value captured at registration. `api_key` is a lookup for the
    same reason: a key saved from Settings has to work on the next request, not the next
    restart.
    """
    bp = Blueprint("correlate", __name__)
    can_view = access.require(permissions.VIEW)

    def _actor():
        return permissions_web.current_actor()

    def _extra():
        return rules.all_extra_variables(db_path)

    def _require_json():
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _visible_alerts():
        """Open alerts this caller may see, in `alerts.list_open()`'s shape.

        Deliberately a copy of the membership test `/api/alerts` makes rather than a call
        into it: that endpoint enriches duplicate_serial rows with machine status for its own
        renderer, and nothing here needs that. The test itself is `alerts.PER_MACHINE_KINDS`
        in both places, which is where it belongs -- app.py's own comment records what
        happened the last time that set was written out by hand at a second call site.
        """
        keep = access.machine_filter()
        if keep is None:
            return alerts.list_open(db_path)
        visible = []
        for alert in alerts.list_open(db_path):
            if alert.get("kind") in alerts.PER_MACHINE_KINDS:
                machine = alert.get("machine")
                if machine and not keep(machine):
                    continue
                visible.append(alert)
                continue
            involved = alert.get("machines") or []
            if involved and not [m for m in involved if keep(m)]:
                continue
            visible.append(alert)
        return visible

    def _rules_by_id():
        return {r["id"]: r for r in rules.list_rules(db_path)}

    def _readable(recommendation):
        """Strip the suggested script's BODY from an answer unless the caller may read one.

        scripts.py splits its own reads for exactly this reason: listing a script is `view`,
        reading its body is `issue_commands`, because a body is SYSTEM-privileged code. A
        drafted body is not in the library yet, but it is a body headed there, so it takes
        the same gate -- a `manage_rules` operator who may ask for a recommendation still may
        not read the code it suggests. The explanation and the steps, which are what they
        asked for, come back untouched.
        """
        if not recommendation or access.can(permissions.ISSUE_COMMANDS):
            return recommendation
        redacted = dict(recommendation)
        redacted.pop("script", None)
        return redacted

    def _bundles(alert_rows=None):
        rows = _visible_alerts() if alert_rows is None else alert_rows
        return rows, correlate.bundle_alerts(rows, rules_by_id=_rules_by_id())

    def _find(machine, anchor):
        """(alert_rows, bundle) for one bundle, or (rows, None).

        Re-derived from the live alert list every time. The alternative -- trusting an
        `alert_ids` array from the body -- would let a caller name alerts that are not
        actually grouped, and then get a recommendation written about a set of machines it
        chose. Scope would still hold; the bundle's honesty would not.
        """
        rows, bundles = _bundles()
        for bundle in bundles:
            if bundle["machine"] == machine and bundle["anchor"] == anchor:
                return rows, bundle
        return rows, None

    # ---------------- Read ----------------
    @bp.route("/api/alerts/bundles", methods=["GET"])
    @login_required
    @can_view
    def list_bundles():
        """Every open alert this caller can see, grouped. The Alerts tab's whole payload.

        Each bundle carries its stored recommendation when there is one for this exact
        membership (see correlate.stored_recommendation on why membership is checked), so the
        tab renders a previously-asked-for explanation without a second round trip and
        without a second provider call.
        """
        try:
            rows, bundles = _bundles()
            for bundle in bundles:
                bundle["recommendation"] = _readable(
                    correlate.stored_recommendation(db_path, bundle))
            return jsonify({
                "bundles": bundles,
                "alerts": {str(a["id"]): a for a in rows},
                # Presentation only -- every route below is gated on its own. This is what
                # lets the tab hide a button the caller cannot use, the same way wake_web's
                # `can_wake` does.
                "can_recommend": access.can(permissions.MANAGE_RULES),
                "can_draft_script": access.can(permissions.ISSUE_COMMANDS),
            }), 200
        except Exception:
            return jsonify({"error": _log_generic("listing bundles")}), 500

    @bp.route("/api/alerts/bundles/<machine>/<int:anchor>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def read_bundle(machine, anchor):
        """One bundle, with the anomaly figures the list endpoint deliberately skips."""
        try:
            rows, bundle = _find(machine, anchor)
            if bundle is None:
                return jsonify({"error": "no such alert bundle"}), 404
            facts = correlate.bundle_facts(db_path, bundle, rows,
                                           rules_by_id=_rules_by_id())
            return jsonify({
                "bundle": bundle,
                "facts": facts,
                "recommendation": _readable(
                    correlate.stored_recommendation(db_path, bundle)),
            }), 200
        except Exception:
            return jsonify({"error": _log_generic("reading a bundle")}), 500

    @bp.route("/api/machines/<machine>/anomalies", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def machine_anomalies(machine):
        """This machine's metrics that sit outside its own trailing normal (FR-02).

        An empty list means "nothing to say" and not "all clear" -- a machine with three days
        of history has no baseline yet, and correlate.anomalies returns the same empty list
        for that as for a machine behaving itself. `baselines` comes back beside it so the
        console can tell the two apart and say which.
        """
        try:
            return jsonify({
                "machine": machine,
                "anomalies": correlate.anomalies(db_path, machine),
                "baselines": correlate.baselines(db_path, machine),
                "min_samples": correlate.MIN_BASELINE_SAMPLES,
            }), 200
        except Exception:
            return jsonify({"error": _log_generic("reading anomalies")}), 500

    # ---------------- The wording layer ----------------
    @bp.route("/api/alerts/bundles/<machine>/<int:anchor>/recommend", methods=["POST"])
    @login_required
    @access.require_machine(permissions.MANAGE_RULES)
    def recommend_bundle(machine, anchor):
        """Ask the configured provider to word a fix for this bundle.

        Only ever reached from an operator pressing a button on one bundle. Nothing in the
        hub calls this on a timer, which is the difference between a feature somebody chose
        and a provider bill somebody discovers.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        try:
            rows, bundle = _find(machine, anchor)
            if bundle is None:
                return jsonify({"error": "no such alert bundle"}), 404
            facts = correlate.bundle_facts(db_path, bundle, rows,
                                           rules_by_id=_rules_by_id())
            error, recommendation = correlate.recommend(
                db_path, ai_config(), bundle, facts,
                api_key=(api_key() if api_key else ""), actor=_actor())
            if error:
                # The provider's own failures are the caller's to read -- "the AI provider did
                # not answer in time" is the most useful thing this endpoint can say when it
                # fails, and ai.complete() already guarantees the sentence is ours. 502, not
                # 400: the request was fine and a third party was not.
                return jsonify({"error": error}), 502
            fleet.audit(db_path, actor=_actor(), action="recommend_fix",
                        target=machine,
                        detail={"bundle": bundle["key"], "alerts": bundle["alert_ids"],
                                "cause": (bundle.get("cause") or {}).get("reason"),
                                "script": bool(recommendation.get("script"))})
            return jsonify(_readable(recommendation)), 200
        except Exception:
            return jsonify({"error": _log_generic("recommending a fix")}), 500

    @bp.route("/api/alerts/bundles/<machine>/<int:anchor>/script", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def draft_bundle_script(machine, anchor):
        """Put the recommendation's suggested script into the script library, switched off.

        **Drafted, never executed** -- the PRD's mitigation, kept verbatim by ROADMAP.MD #17.
        The body goes through `scripts.validate_script` on the way in, exactly as a
        hand-written one does, and lands disabled for a human to read and enable.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        try:
            _rows, bundle = _find(machine, anchor)
            if bundle is None:
                return jsonify({"error": "no such alert bundle"}), 404
            extra = _extra()
            error, script = correlate.draft_script(
                db_path, bundle,
                known_variable=lambda ref: rules.lookup_variable(ref, extra) is not None,
                actor=_actor())
            if error:
                return jsonify({"error": error}), 400
            # LEVEL_SECURITY, like every other write to the script library: what this row
            # records is the content of code that will run as SYSTEM the moment somebody
            # enables it. `source` names where the body came from, because a reviewer reading
            # the library six months from now cannot tell a drafted script from a typed one.
            fleet.audit(db_path, actor=_actor(), action="draft_suggested_script",
                        level=fleet.LEVEL_SECURITY, target=script["name"],
                        detail={"machine": machine, "bundle": bundle["key"],
                                "source": "recommendation", "enabled": script["enabled"],
                                "chars": len(script["body"])})
            return jsonify(script), 200
        except Exception:
            return jsonify({"error": _log_generic("drafting a suggested script")}), 500

    @bp.route("/api/alerts/bundles/<machine>/<int:anchor>/recommend", methods=["DELETE"])
    @login_required
    @access.require_machine(permissions.MANAGE_RULES)
    def clear_bundle_recommendation(machine, anchor):
        """Throw away a recommendation an operator disagrees with, so the next ask starts
        clean rather than re-rendering the answer they rejected."""
        try:
            _rows, bundle = _find(machine, anchor)
            if bundle is None:
                return jsonify({"error": "no such alert bundle"}), 404
            correlate.clear_recommendation(db_path, bundle["key"])
            return jsonify({"status": "cleared"}), 200
        except Exception:
            return jsonify({"error": _log_generic("clearing a recommendation")}), 500

    return bp
