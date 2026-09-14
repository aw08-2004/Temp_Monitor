"""Flask HTTP surface for the AI drafter (roadmap #24) -- a thin layer over ai.py, registered
as a Blueprint from app.py.

**Two gates, and neither of them is new.** Reading whether the feature is configured is `view`;
drafting, refining and committing are `manage_rules`, because a draft is a rule in every sense
that matters -- ROADMAP.MD #8 is explicit that a second authorization model is the thing to
avoid, and an "ai" capability would be exactly that. A rule that issues COMMANDS still needs
`issue_commands` on top, and that check is threaded down into rules.validate_actions rather
than made here, so a follow-up action nested inside a message's on_response map gets the same
treatment as a top-level one.

**Committing a draft goes through the same two scope checks a hand-written rule does**, and
for the reason rules_web.py's docstring spells out at length: the resolved target membership
must lie inside the caller's scope at save time, AND the author's scope is persisted with the
rule so a dynamic `{"kind": "all"}` cannot grow past its author later. Drafting by machine is
the one route here that names a machine, so it takes `require_machine` rather than `require` --
capability and scope, not capability alone.

Error text is split in two, copied from rules_web.py deliberately. Validation messages produced
by rules.py out of the caller's own input ("unknown variable: cpu.temp_c") go back VERBATIM:
that sentence is the most useful thing this feature produces, and a drafter whose every
refusal reads "invalid request" is one nobody can learn to use. Anything thrown by code we did
not write -- SQLite, the resolver, the HTTP client -- does not: it is logged and answered with
one fixed sentence.

The provider's own text is treated as neither. A model's output is untrusted input, so it
reaches a response only as a `refusal` field the model was asked to write in plain language, or
as an expression that has already parsed. Nothing else it says is forwarded.

Bodies are JSON everywhere, which is load-bearing CSRF protection -- see fleet_web.py's module
docstring, which applies here verbatim. There are no multipart endpoints in this file and there
should not be.
"""
import traceback

from flask import Blueprint, jsonify, request

import ai
import envfile
import fleet
import permissions
import rules

# The one sentence any unexpected failure in this module answers with. Fixed text, so no
# caller can learn anything about the hub's internals from the shape of a failure.
GENERIC_ERROR = "the hub could not complete that request; see the hub log for details"

# Where the provider key lives, named once so the route, its audit row and app.py's lookup
# cannot disagree. And a bound on it: no provider issues a key anywhere near this long, and the
# value is written into a file that holds the hub's perimeter.
KEY_ENV = "AI_API_KEY"
MAX_KEY_CHARS = 512


def _log_generic(context):
    """Log the exception currently being handled, and return the caller-safe message.

    Called from inside an `except` block. The traceback goes to the hub's log and the return
    value is a constant -- the same split rules_web.py uses, and for the same reason: a new
    broad `except` here cannot start leaking, because there is nothing to interpolate.
    """
    print(f"[ai] Failed while {context}:\n{traceback.format_exc()}")
    return GENERIC_ERROR


def create_ai_blueprint(db_path, login_required, access, resolve_vars, rules_config,
                        ai_config, api_key="", env_path=None):
    """Build the AI Blueprint.

    `resolve_vars(machine)`, `rules_config()` and `ai_config()` come from app.py for the same
    reason the rules blueprint takes the first two: ai.py and rules.py stay free of Flask,
    settings and the sensor parsing that lives in app, and this layer is where those get
    injected.

    **`ai_config()` is called per request, never cached in this closure.** An admin switching
    the feature off has to be obeyed on the next request rather than at the next restart,
    which is the entire point of having an off switch -- the same reasoning `_rules_config`
    carries in app.py.

    `api_key` is a string, or a zero-argument callable returning one. app.py passes a callable
    that reads os.environ on every request, because the key can be set from Settings (POST
    /api/ai/key) and a key captured at boot would ignore that until the next restart -- the
    same move the TURN secret control made, for the same reason. A plain string still works,
    which keeps a caller with nothing to look up simple.

    `env_path` is the `.env` a key set from the console is written to. None means this
    deployment cannot write it, and the route says so rather than pretending to save.
    """
    bp = Blueprint("ai", __name__)
    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_RULES)
    # Refreshing the model list is CONFIGURING the provider, not authoring a rule: it is the
    # one action here that makes this hub talk to the third party without an operator having
    # asked a question, and the field it fills in lives on the Settings page. So it is gated
    # with the rest of that page rather than with the drafter.
    can_configure = access.require(permissions.MANAGE_SETTINGS)

    def _api_key():
        """The provider key as of THIS request. Never cached -- see the docstring above."""
        value = api_key() if callable(api_key) else api_key
        return str(value or "")

    def _extra():
        return rules.all_extra_variables(db_path)

    def _actor():
        return access.email() or "unknown"

    def _may_issue_commands():
        return access.can(permissions.ISSUE_COMMANDS)

    def _author_scope():
        """The machines this caller can reach, or None if unrestricted. Same convention as
        rules_web._author_scope, read from the same place so the two cannot drift."""
        machines = (access.current() or {}).get("machines")
        return None if machines is None else sorted(machines)

    def _scope_error(target):
        """Refuse a target that reaches outside the caller's scope. Returns an error or None.

        Lifted from rules_web._scope_error, and checked on the RESOLVED membership rather
        than on the selectors for the reason given there: "all except two" and an OU selector
        both reach machines the selector text never names. It matters more here, not less --
        the selector was written by a model, so nobody read it before it arrived.
        """
        try:
            machines = rules.resolve_targets(db_path, target)
        except Exception:                             # noqa: BLE001
            return _log_generic("resolving a drafted rule's targets")
        outside = [m for m in machines if not access.in_scope(m)]
        if outside:
            return ("this rule would target machines outside your access: "
                    + ", ".join(sorted(outside)[:5])
                    + (f" (and {len(outside) - 5} more)" if len(outside) > 5 else ""))
        return None

    def _rendered(draft):
        """A draft plus the deterministic summary, as the console reads it.

        The summary is computed here rather than in the browser because it is the sentence an
        operator confirms, and `condition_text` is echoed because the console posts it
        straight to `/api/rules/preview` -- there is no dry-run route in this file for exactly
        that reason.

        `access.in_scope` goes in with it so the machine count is the caller's own, not the
        fleet's -- the same filter preview_targets applies, and for the stronger reason here:
        this count is what somebody reads before pressing commit.
        """
        try:
            summary = ai.summarise_draft(db_path, draft, in_scope=access.in_scope)
        except Exception:                             # noqa: BLE001
            summary = ""
            _log_generic("summarising a draft")
        return {"draft": draft, "summary": summary,
                "condition_text": draft.get("condition_text", ""),
                "can_issue_commands": _may_issue_commands()}

    # ---------------- Status ----------------

    @bp.route("/api/ai/status", methods=["GET"])
    @login_required
    @can_view
    def ai_status():
        """Whether the feature is usable, and what it is pointed at.

        **Never the key.** provider_config() carries it, so the response is assembled field by
        field here rather than by handing that dict back with something popped out of it -- a
        later field added to the config would then appear in this response by default, and the
        default for a config that holds a credential must be "not exposed".
        """
        config = ai_config()
        error, resolved = ai.provider_config(config, _api_key())
        if error:
            return jsonify({"enabled": ai.is_enabled(config), "ready": False,
                            "error": error, "providers": list(ai.PROVIDERS),
                            "provider": ai.preset_for(config.get("provider")).name,
                            "has_api_key": bool(_api_key()),
                            "can_write_key": bool(env_path)}), 200
        return jsonify({
            "enabled": True,
            "ready": True,
            "provider": resolved["provider"],
            "model": resolved["model"],
            "base_url": resolved["base_url"],
            "send_machine_names": resolved["send_machine_names"],
            "providers": list(ai.PROVIDERS),
            # Whether a key EXISTS, never the key. An operator debugging a 401 needs to know
            # that .env was read; nobody needs the value back out of the hub that holds it.
            "has_api_key": bool(_api_key()),
            "can_write_key": bool(env_path),
            "can_manage": access.can(permissions.MANAGE_RULES),
        }), 200

    # ---------------- The provider key ----------------

    @bp.route("/api/ai/key", methods=["POST"])
    @login_required
    @can_configure
    def set_ai_key():
        """Set or remove the provider key from the console. **It is never echoed back.**

        Written to .env AND to the live environment -- the pair envfile.apply_to_environ exists
        for -- so it takes effect on the next request with no restart, and survives the next
        restart because the file is what python-dotenv reads at boot. The shape of the TURN
        secret control, with one deliberate difference: that route returns the secret once so it
        can be pasted into coturn, and nothing needs a copy of this one, so the answer says only
        whether a key is now set.

        An empty value REMOVES the key rather than storing an empty one: envfile.set_vars deletes
        on None, and a file reading `AI_API_KEY=` claims a configuration that is not there.

        **A control character is refused, not stripped.** The value becomes one line of a dotenv
        file that also holds ALLOWED_EMAILS and the enrollment secret. A line break in it would
        write a second line, and a second line in that file is a change to the hub's perimeter
        made through an API-key box. Refusing says so; stripping would save something the admin
        did not type.
        """
        if not env_path:
            return jsonify({"error": "this hub cannot write its .env file in this deployment; "
                                     "set AI_API_KEY on the server instead"}), 400
        body = request.get_json(silent=True) or {}
        value = str(body.get("key") or "").strip()
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            return jsonify({"error": "an API key cannot contain line breaks or other control "
                                     "characters"}), 400
        if len(value) > MAX_KEY_CHARS:
            return jsonify({"error": f"that key is too long (limit {MAX_KEY_CHARS} "
                                     "characters)"}), 400
        update = {KEY_ENV: value or None}
        try:
            envfile.set_vars(env_path, update)
        except OSError:
            # The exception text carries the file path, and this answer goes to a browser.
            return jsonify({"error": _log_generic("writing the AI key to .env")}), 500
        envfile.apply_to_environ(update)
        # Never the key, and not its length either: a length narrows a guess and helps nobody.
        fleet.audit(db_path, actor=_actor(),
                    action="ai_api_key_set" if value else "ai_api_key_cleared",
                    level=fleet.LEVEL_SECURITY, target="hub")
        return jsonify({"has_api_key": bool(_api_key())}), 200

    # ---------------- The model list ----------------

    @bp.route("/api/ai/models", methods=["GET"])
    @login_required
    @can_view
    def ai_models():
        """What the chosen provider last reported. Reads the cache, never the network."""
        config = ai_config()
        provider = ai.preset_for(config.get("provider")).name
        listing = ai.list_models(db_path, provider)
        listing["provider"] = provider
        listing["can_refresh"] = access.can(permissions.MANAGE_SETTINGS)
        return jsonify(listing), 200

    @bp.route("/api/ai/models/refresh", methods=["POST"])
    @login_required
    @can_configure
    def refresh_ai_models():
        """Ask the provider what it serves. The one route here that reaches out on its own.

        502 when the provider is the problem, which separates "your hub is fine and theirs is
        not" from a 400 about this hub's own configuration -- the same split sharing_web.py
        draws for a peer that will not answer.
        """
        config = ai_config()
        error, listing = ai.refresh_models(db_path, config, api_key=_api_key())
        if error:
            unconfigured = ("switched off" in error or "no AI provider" in error)
            return jsonify({"error": error}), (400 if unconfigured else 502)
        listing["provider"] = ai.preset_for(config.get("provider")).name
        fleet.audit(db_path, actor=_actor(), action="ai_models_refreshed",
                    target=listing["provider"],
                    detail={"models": len(listing["models"])})
        return jsonify(listing), 200

    # ---------------- Drafting ----------------

    @bp.route("/api/ai/rules/draft", methods=["POST"])
    @login_required
    @can_manage
    def draft_rule():
        """English in, a validated draft out. **Saves no rule and fires nothing.**

        `allow_command` is this caller's own ISSUE_COMMANDS, so an operator who may raise
        alerts but not issue commands gets a draft refused at the action rather than a rule
        they cannot save.
        """
        body = request.get_json(silent=True) or {}
        error, draft = ai.draft_rule(db_path, ai_config(), body.get("text"), extra=_extra(),
                                     api_key=_api_key(), actor=_actor(),
                                     allow_command=_may_issue_commands())
        if error:
            return jsonify({"error": error}), 400
        config = ai_config()
        stored = ai.save_draft(db_path, draft, actor=_actor(),
                               provider=str(config.get("provider") or ""),
                               model=str(config.get("model") or ""))
        return jsonify(_rendered(stored)), 201

    @bp.route("/api/ai/rules/drafts", methods=["GET"])
    @login_required
    @can_manage
    def list_drafts():
        """This caller's drafts only. A draft is somebody's unfinished sentence, not fleet
        configuration -- see ai.list_drafts."""
        return jsonify({"drafts": ai.list_drafts(db_path, actor=_actor())}), 200

    @bp.route("/api/ai/rules/drafts/<draft_id>", methods=["GET"])
    @login_required
    @can_manage
    def get_draft(draft_id):
        draft = ai.get_draft(db_path, draft_id)
        if not draft or draft.get("actor") != _actor():
            # One 404 for "no such draft" and for "not yours", so this endpoint cannot be used
            # to count how much drafting a colleague has been doing.
            return jsonify({"error": "no such draft"}), 404
        return jsonify(_rendered(draft)), 200

    @bp.route("/api/ai/rules/drafts/<draft_id>/refine", methods=["POST"])
    @login_required
    @can_manage
    def refine_draft(draft_id):
        """"Make it 100 instead." Replaces the draft in place, keeping its id and accumulating
        the English that produced it."""
        draft = ai.get_draft(db_path, draft_id)
        if not draft or draft.get("actor") != _actor():
            return jsonify({"error": "no such draft"}), 404
        body = request.get_json(silent=True) or {}
        error, refined = ai.refine_draft(db_path, ai_config(), draft, body.get("text"),
                                         extra=_extra(), api_key=_api_key(), actor=_actor(),
                                         allow_command=_may_issue_commands())
        if error:
            return jsonify({"error": error}), 400
        stored = ai.save_draft(db_path, refined, draft_id=draft_id, actor=_actor())
        return jsonify(_rendered(stored)), 200

    @bp.route("/api/ai/rules/drafts/<draft_id>/commit", methods=["POST"])
    @login_required
    @can_manage
    def commit_draft(draft_id):
        """Turn a draft into a real rule. The one route here that changes the fleet's standing
        instructions, and the only one an operator has to press deliberately.

        Everything about the save is the ordinary path: rules.save_rule, the caller's
        `author_scope` stamped on, the fleet-wide target cap and the command cooldown floor
        from settings. The rule arrives DISABLED (ai.rule_payload) -- committing a draft is
        agreeing that it says the right thing, not agreeing that it should start firing before
        anybody has previewed it.
        """
        draft = ai.get_draft(db_path, draft_id)
        if not draft or draft.get("actor") != _actor():
            return jsonify({"error": "no such draft"}), 404

        payload = ai.rule_payload(draft)
        config = rules_config()
        error, target = rules.validate_target(payload.get("target"), _extra())
        if not error:
            error = _scope_error(target)
        if error:
            return jsonify({"error": error}), 400

        error, rule = rules.save_rule(
            db_path, payload, actor=_actor(), extra=_extra(),
            author_scope=_author_scope(), allow_command=_may_issue_commands(),
            max_targets_cap=config["max_targets_per_tick"],
            command_cooldown_floor=config["command_cooldown_floor_seconds"],
        )
        if error:
            return jsonify({"error": error}), 400
        ai.delete_draft(db_path, draft_id)
        return jsonify({"rule": rule, "source_text": draft.get("source_text", "")}), 201

    @bp.route("/api/ai/rules/drafts/<draft_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_draft(draft_id):
        draft = ai.get_draft(db_path, draft_id)
        if not draft or draft.get("actor") != _actor():
            return jsonify({"error": "no such draft"}), 404
        ai.delete_draft(db_path, draft_id)
        return jsonify({"status": "deleted"}), 200

    # ---------------- Not built yet (roadmap #24) ----------------
    # Both routes exist and both answer 501. They are here rather than absent so the console
    # can ask what this hub supports without a version check, and so the GATE each one needs
    # is written down now, while the reasoning is fresh -- `require_machine` on the machine
    # route is the whole point of it, and adding it later, to a route somebody had already
    # copied from a sibling, is how a scope leak gets introduced.

    @bp.route("/api/ai/machines/<machine>/ask", methods=["POST"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def ask_about_machine(machine):
        """The machine chat panel. Capability AND scope, because the route names a machine."""
        return jsonify({"error": "the machine chat panel is not built yet (roadmap #24)"}), 501

    @bp.route("/api/ai/query", methods=["POST"])
    @login_required
    @can_view
    def fleet_query():
        """Natural-language fleet query. Answers over the rules evaluator, filtered by
        access.in_scope -- never over generated SQL, which cannot be scoped. See ai.fleet_query
        and ROADMAP.MD #24."""
        return jsonify({"error": "fleet query is not built yet (roadmap #24)"}), 501

    return bp
