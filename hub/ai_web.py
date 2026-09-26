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
rule so a dynamic `{"kind": "all"}` cannot grow past its author later. Asking a question ABOUT
a machine is the one route here that names one, so it takes `require_machine` rather than
`require` -- capability and scope, not capability alone. The two fleet-wide answering routes
have the matching problem in a shape that is easier to miss: a query and a count both describe
machines without naming one, so `access.in_scope` is threaded into the evaluation and into the
figures rather than applied to what comes back.

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
        """A draft plus a deterministic summary PER STAGE, as the console reads it.

        The summaries are computed here rather than in the browser because they are the
        sentences an operator confirms, and each stage's `condition_text` is echoed because
        the console posts it straight to `/api/rules/preview` -- there is no dry-run route in
        this file for exactly that reason.

        `access.in_scope` goes in with them so the machine count is the caller's own, not the
        fleet's -- the same filter preview_targets applies, and for the stronger reason here:
        this count is what somebody reads before pressing commit.

        **The stage's INDEX is in the payload, and it is what commit takes.** The browser
        could count rows itself, but then the number identifying a rule on the way back would
        be one the browser invented, and a stale card would commit whichever stage happens to
        sit at that position now.
        """
        staged = ai.draft_rules(draft)
        try:
            summaries = ai.summarise_rules(db_path, draft, in_scope=access.in_scope)
        except Exception:                             # noqa: BLE001
            summaries = [""] * len(staged)
            _log_generic("summarising a draft")
        return {"draft": draft,
                "rules": [{"index": index,
                           "name": rule.get("name", ""),
                           "condition_text": rule.get("condition_text", ""),
                           # The target travels because the console posts it straight back to
                           # /api/rules/preview -- a preview run against `all` when the stage
                           # names an OU answers a question nobody asked.
                           "target": rule.get("target"),
                           "summary": summaries[index] if index < len(summaries) else ""}
                          for index, rule in enumerate(staged)],
                "can_issue_commands": _may_issue_commands()}

    # ---------------- Status ----------------

    @bp.route("/api/ai/status", methods=["GET"])
    @login_required
    @can_view
    def ai_status():
        """Whether the feature is usable, and what it is pointed at.

        **Never the key**, and now belt and braces about it. `provider_config()` no longer
        returns the key at all, so this response could not carry it by accident; the fields are
        still listed one at a time rather than handed back as that dict, because a field added
        to the resolved config later would otherwise appear in this response by default, and
        the default for anything resolved from settings must be "not exposed".
        """
        config = ai_config()
        error, resolved = ai.provider_config(config)
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
        """Turn ONE stage of a draft into a real rule. The one route here that changes the
        fleet's standing instructions, and the only one an operator has to press deliberately.

        **One stage per press, not the whole set.** An escalation is two rules an operator
        should read separately -- the gentle one and the one that reboots somebody's machine
        are not the same decision -- and a single button that created both would put the
        forced restart on the other side of a click nobody aimed at it. `index` says which;
        omitted it is the first stage, which keeps this callable from a script with a body
        of `{}`.

        The committed stage is removed from the draft and the rest are kept, so creating the
        warning does not throw the enforcement away. The draft row goes when its last stage
        does.

        Everything about the save is the ordinary path: rules.save_rule, the caller's
        `author_scope` stamped on, the fleet-wide target cap and the command cooldown floor
        from settings. The rule arrives DISABLED (ai.rule_payload) -- committing a draft is
        agreeing that it says the right thing, not agreeing that it should start firing before
        anybody has previewed it.
        """
        draft = ai.get_draft(db_path, draft_id)
        if not draft or draft.get("actor") != _actor():
            return jsonify({"error": "no such draft"}), 404

        staged = ai.draft_rules(draft)
        body = request.get_json(silent=True) or {}
        try:
            index = int(body.get("index") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "which rule to create must be a number"}), 400
        if not 0 <= index < len(staged):
            return jsonify({"error": "that draft has no such rule"}), 400

        payload = ai.rule_payload(staged[index], source_text=draft.get("source_text", ""))
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

        remaining = [r for i, r in enumerate(staged) if i != index]
        if remaining:
            draft["rules"] = remaining
            stored = ai.save_draft(db_path, draft, draft_id=draft_id, actor=_actor())
            rendered = _rendered(stored)
        else:
            ai.delete_draft(db_path, draft_id)
            rendered = {"draft": None, "rules": []}
        return jsonify({"rule": rule, "source_text": draft.get("source_text", ""),
                        "remaining": rendered["rules"],
                        "draft": rendered["draft"]}), 201

    @bp.route("/api/ai/rules/drafts/<draft_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_draft(draft_id):
        draft = ai.get_draft(db_path, draft_id)
        if not draft or draft.get("actor") != _actor():
            return jsonify({"error": "no such draft"}), 404
        ai.delete_draft(db_path, draft_id)
        return jsonify({"status": "deleted"}), 200

    # ---------------- Answering, rather than authoring ----------------
    # Three routes that ANSWER a question instead of drafting a rule. Each one's gate is the
    # interesting part, and each is different:
    #
    #  * the machine route names a machine, so it takes `require_machine` -- capability AND
    #    scope. Written this way when it was still a 501, precisely so it could not later be
    #    copied from a sibling that only needed `require`.
    #  * the query route answers over machines, and `access.in_scope` goes INTO the evaluation
    #    rather than filtering its output, so a machine outside somebody's reach is never
    #    resolved at all. `view` is the right capability: asking which machines are hot is
    #    reading the fleet, not managing rules.
    #  * the summary route counts alerts and rule fires, and a count is the easiest thing in
    #    this file to leak a fleet through -- "14 alerts today" reads as an answer rather than
    #    as a statement about machines the caller cannot see. So the same predicate goes in.

    @bp.route("/api/ai/machines/<machine>/ask", methods=["POST"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def ask_about_machine(machine):
        """The machine chat panel. Capability AND scope, because the route names a machine.

        The answer travels with the SNAPSHOT it was built from, and ai.py's docstring argues
        why at length: this is the one response in this file that carries a model's prose to
        an operator, and the readings underneath it are what make that prose checkable.
        """
        body = request.get_json(silent=True) or {}
        try:
            error, answer = ai.answer_machine_question(
                db_path, ai_config(), machine, body.get("text"), resolve_vars=resolve_vars,
                api_key=_api_key(), actor=_actor())
        except Exception:                             # noqa: BLE001
            # The resolver reads eight tables and the live cache. One machine whose values
            # will not resolve is a hub-side failure, not a sentence about the request, so it
            # follows this file's split: logged in full, answered with the fixed message.
            return jsonify({"error": _log_generic(f"answering a question about {machine}")}), 500
        if error:
            return jsonify({"error": error}), 400
        return jsonify(answer), 200

    @bp.route("/api/ai/query", methods=["POST"])
    @login_required
    @can_view
    def fleet_query():
        """Natural-language fleet query. Answers over the rules evaluator, filtered by
        access.in_scope -- never over generated SQL, which cannot be scoped. See ai.fleet_query
        and ROADMAP.MD #24."""
        body = request.get_json(silent=True) or {}
        try:
            error, answer = ai.fleet_query(
                db_path, ai_config(), body.get("text"), in_scope=access.in_scope,
                resolve_vars=resolve_vars, extra=_extra(), api_key=_api_key(),
                actor=_actor(), limit=body.get("limit"))
        except Exception:                             # noqa: BLE001
            return jsonify({"error": _log_generic("answering a fleet query")}), 500
        if error:
            return jsonify({"error": error}), 400
        return jsonify(answer), 200

    @bp.route("/api/ai/summary", methods=["POST"])
    @login_required
    @can_view
    def fleet_summary():
        """What was raised, cleared and left open. **A POST, and not because it changes
        anything.**

        It changes nothing; it SPENDS something. A GET would be fetchable from a third party's
        page by an `<img>` tag, and every fetch is a request this hub pays a provider for. The
        JSON body is the same CSRF protection every other route in this file relies on, and
        here it is protecting a bill rather than a write.

        A provider that is off or unreachable is not an error: ai.daily_summary computes the
        figures locally and reports the provider's failure beside them, so the report arrives
        either way and the console prints what is missing.
        """
        body = request.get_json(silent=True) or {}
        try:
            window = int(body.get("window_days") or 1)
        except (TypeError, ValueError):
            return jsonify({"error": "the report window must be a number of days"}), 400
        try:
            error, summary = ai.daily_summary(
                db_path, ai_config(), window_days=window, in_scope=access.in_scope,
                api_key=_api_key(), actor=_actor())
        except Exception:                             # noqa: BLE001
            return jsonify({"error": _log_generic("building the fleet summary")}), 500
        if error:
            return jsonify({"error": error}), 400
        return jsonify(summary), 200

    return bp
