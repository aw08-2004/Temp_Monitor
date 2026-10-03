"""Flask HTTP surface for the console assistant (roadmap #26), registered from app.py.

**A tool call is an internal request made AS the operator.** RouteDispatcher.call() replays a
tool call through the app's own test client with a copy of the operator's session and their
CSRF token, so it passes through login_required, `access.require`/`require_machine` and every
inline scope check the route makes -- the exact code path the operator's own click takes.
Nothing in this file decides whether the operator may do something; it only decides whether
the assistant may do it WITHOUT ASKING (assistant_tools.classify), and the answer to that is
never "more than the operator".

Rejected: calling the model halves (fleet.create_command and friends) directly. That is the
obvious design and the wrong one, because the gates live in the web halves -- and not all of
them in decorators. POST /api/fleet/commands checks the machine named in its BODY inline; a
watchdog write needs two capabilities; rules.validate_actions checks issue_commands for a
command nested inside a rule. A tool layer calling the model halves would have to re-implement
every one of those, and the one it forgot would be the hole.

Rejected: running the internal request on the operator's ORIGINAL request thread (streaming
the turn back over the POST). A turn makes several provider calls and can take a minute; the
hub's transport is polling everywhere else, so the POST answers 202 with a run id and the panel
polls the RunLog -- the shape the command-output poll already has.

**Who confirms is checked at confirm time, as whoever is confirming.** The pending action runs
with the session of the confirm request, not the session that queued it, so an operator who
lost a capability between the two is refused by the route itself.

The visibility gate is `view` plus the AI switch. No new capability -- ai_web.py's docstring
and ROADMAP #8 explain why a second authorization model is the thing to avoid.
"""
import datetime
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse

from flask import Blueprint, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

import ai
import assistant
import assistant_guide
import assistant_tools
import fleet
import permissions

GENERIC_ERROR = "the hub could not complete that request; see the hub log for details"
MAX_CONTEXT_VALUE = 300
MAX_SELECTION = 50

# Header marking a request the assistant made, so a route or a log line can tell the two
# apart. Informational only -- nothing grants or denies on it.
ASSISTANT_HEADER = "X-FleetHub-Assistant"


def _json_body():
    """The request's JSON body as a dict, or an empty one. A JSON array or a bare string is
    valid JSON and `get_json` returns it as such; `.get` on it raised, and the route answered
    500 instead of its own validation message (found in review, PR #101)."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _norm(rule):
    """A rule with its converters erased: `/api/x/<int:id>` and `/api/x/<id>` compare equal."""
    return re.sub(r"<[^>]+>", "<>", str(rule or ""))


class RouteDispatcher:
    """The app's own routes, as something a tool can resolve and call."""

    def __init__(self, app):
        self.app = app
        self._catalog = None

    def resolve(self, method, path):
        """(rule, view_args) the path lands on, or (None, None). Never follows a redirect:
        a strict-slashes redirect is a different path, and the tier was decided on this one.

        **Matched on the DECODED path**, because that is what call() routes: the test client
        percent-decodes PATH_INFO before the url_map sees it. Matching the encoded string
        instead let `/api/backups/machines/PC-01%2Fdownload` classify as the read-tier machine
        route and then run as the denied download (found in review, PR #97). A decoded path
        that grows a dot segment or an empty segment is refused rather than normalised."""
        decoded = unquote(str(path or ""))
        if any(seg in (".", "..") for seg in decoded.split("/")) or "//" in decoded \
                or "\\" in decoded:
            return None, None
        adapter = self.app.url_map.bind("localhost")
        try:
            rule, args = adapter.match(decoded, method=method, return_rule=True)
        except HTTPException:
            return None, None
        except Exception:                               # noqa: BLE001 -- RequestRedirect et al.
            return None, None
        return rule.rule, args

    def catalog(self):
        """Every JSON route: method, rule, the capabilities its decorators label, one line of
        its docstring. Built once; the url_map does not change after boot."""
        if self._catalog is None:
            entries = []
            for rule in self.app.url_map.iter_rules():
                if not rule.rule.startswith("/api/"):
                    continue
                view = self.app.view_functions.get(rule.endpoint)
                doc = (getattr(view, "__doc__", "") or "").strip().split("\n")[0][:160]
                caps = tuple(getattr(view, "_fleethub_caps", ()))
                for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
                    entries.append({"method": method, "rule": rule.rule, "summary": doc,
                                    "caps": caps})
            self._catalog = entries
        return self._catalog

    def call(self, session_data, method, path, query=None, body=None):
        """Run one route as the session in `session_data`. Returns (status, payload)."""
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.clear()
            sess.update(session_data or {})
        headers = {ASSISTANT_HEADER: "1",
                   "X-CSRF-Token": str((session_data or {}).get("csrf_token") or "")}
        kwargs = {"method": method, "headers": headers, "query_string": query or None}
        if method not in ("GET", "HEAD"):
            kwargs["json"] = body if body is not None else {}
        response = client.open(path, **kwargs)
        payload = response.get_json(silent=True)
        if payload is None:
            payload = {"content_type": response.mimetype,
                       "text": response.get_data(as_text=True)[:4000]}
        return response.status_code, payload


def create_assistant_blueprint(db_path, login_required, access, ai_config, *, api_key,
                               dispatcher, machine_exists, translate, language, setting,
                               audit=fleet.audit, workers=4, public_url="",
                               wait_interval=2.0):
    """Build the assistant Blueprint.

    `dispatcher` is a RouteDispatcher over the real app (a fake in tests); `machine_exists`,
    `translate(key)`, `language()` and `setting(key)` come from app.py so this file reads no
    globals. `ai_config()` and `api_key()` are read per request for the reason ai_web.py gives:
    an off switch has to be obeyed on the next request, not at the next restart.
    """
    bp = Blueprint("assistant", __name__)
    can_view = access.require(permissions.VIEW)
    runs = assistant.RunLog()
    # Bounded, so a burst of messages queues instead of opening a provider connection each.
    pool = ThreadPoolExecutor(max_workers=max(1, int(workers)),
                              thread_name_prefix="assistant")
    # Naming a conversation has its own two workers. On the answer pool a burst of new
    # conversations queued their names in front of other operators' answers, for a feature
    # that is cosmetic; here it can only ever wait behind other names.
    namer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assistant-name")

    def _key():
        value = api_key() if callable(api_key) else api_key
        return str(value or "")

    def _owner():
        return access.email() or ""

    def _enabled(config):
        return ai.is_enabled(config) and bool(setting("ai.assistant_enabled"))

    # ---------------- Links ----------------

    def _resolve_link(kind, arg, opts):
        """Turn one link token into (href, label), or None. Runs in a request context, so
        `access` here is the operator LOOKING at the answer -- a link is checked when it is
        shown, not when it was written."""
        label = opts.get("label") or arg
        if kind == "page":
            entry = assistant_guide.PAGES_BY_KEY.get(arg.strip().lower())
            if not entry:
                return None
            path, label_key, cap, _about = entry
            caps = cap if isinstance(cap, tuple) else (cap,)
            if not any(access.can(c) for c in caps):
                return None
            return path, opts.get("label") or translate(label_key)
        machine = arg.strip()
        if not machine or not access.in_scope(machine) or not machine_exists(machine):
            return None
        slug = quote(machine, safe="")
        tab = (opts.get("tab") or "").lower()
        if kind == "report" or tab == "report":
            return f"/reports/machines/{slug}", label
        if kind == "remote":
            if not access.can(permissions.REMOTE_CONTROL):
                return None
            return f"/remote?machine={slug}", label
        if tab in assistant_guide.MACHINE_TABS:
            return f"/machine/{slug}?tab={tab}", label
        return f"/machine/{slug}", label

    def _own_hosts():
        """The names this hub answers to, for reading back an absolute link the model wrote
        (https://hub.example/machine/PC-12). This request's host, and the public URL the hub
        was configured with, which is the one the model has seen."""
        hosts = {request.host}
        if public_url:
            hosts.add(urlparse(public_url).netloc)
        return hosts

    def _render(text):
        rendered, _links = assistant.render_links(text, _resolve_link, _own_hosts())
        return rendered

    def _public_action(action):
        if not action:
            return None
        return {key: action[key] for key in ("id", "tool", "method", "path", "machine",
                                             "query", "body", "typed_name", "state",
                                             "result", "created_at", "expires_at",
                                             "risk", "impact", "auto", "mode")}

    # ---------------- Tools ----------------

    def _offered_curated(caps):
        """Curated tools whose route's labelled capabilities this operator holds."""
        by_rule = {}
        for entry in dispatcher.catalog():
            by_rule.setdefault((entry["method"], _norm(entry["rule"])), entry["caps"])
        names = set()
        for name, (_desc, method, template, _schema) in assistant_tools.CURATED_BY_NAME.items():
            needed = by_rule.get((method, _norm(template)))
            if needed is None:
                continue
            if all(c in caps for c in needed):
                names.add(name)
        return names

    def _wait_for_command(session_data, path, query, seconds, cancelled):
        """Poll a command's output route until the command has finished, or `seconds` pass.

        A fleet command runs when the agent next polls, seconds to minutes after it was
        queued, so a model that reads the output straight away sees `pending` and nothing
        else. Waiting here, a step at a time, is what lets the follow-up after a confirmed
        action report the outcome instead of "it was queued"."""
        deadline = time.monotonic() + max(0, min(int(seconds),
                                                 assistant_tools.MAX_WAIT_SECONDS))
        while True:
            status, payload = dispatcher.call(session_data, "GET", path, query, None)
            finished = isinstance(payload, dict) and payload.get("status") in (
                "done", "failed", "expired")
            if status >= 400 or finished or time.monotonic() >= deadline or cancelled():
                return status, payload
            time.sleep(wait_interval)

    def _run_action_now(action, session_data, owner):
        """Run a stored action at once (Auto said routine, or Bypass). Same record as a click:
        claimed once, run as the operator, its result and an audit row written."""
        error, claimed = assistant.claim_action(db_path, action["id"], owner)
        if error:
            return None, None, error
        try:
            status, payload = dispatcher.call(session_data, claimed["method"], claimed["path"],
                                              claimed["query"], claimed["body"])
        except Exception:                               # noqa: BLE001
            print(f"[assistant] action run without asking failed:\n{traceback.format_exc()}")
            status, payload = 500, {"error": GENERIC_ERROR}
        done = assistant.finish_action(db_path, claimed["id"], owner, status < 400,
                                       {"status": status, "data": payload})
        audit(db_path, actor=owner, action="assistant.action_auto",
              target=done["machine"] or done["path"],
              detail={"action": done["id"], "tool": done["tool"], "method": done["method"],
                      "rule": done["rule"], "status": status, "mode": done["mode"],
                      "risk": done["risk"], "impact": done["impact"]})
        return done, (status, payload), None

    def _make_executor(*, chat_id, owner, session_data, caps, cancelled=lambda: False):
        catalog = dispatcher.catalog()
        # Resolved NOW, inside the request: `execute` runs on a pool thread with no Flask
        # context, and translate() reads the language off `g`. Called from there it raised on
        # every find_page, which the loop reported to the model as a hub failure.
        labels = {label_key: translate(label_key)
                  for _key, _path, label_key, _cap, _about in assistant_guide.PAGES}

        def execute(name, raw_arguments):
            error, args = assistant_tools.parse_arguments(raw_arguments)
            if error:
                return {"ok": False, "error": error}
            if name == "find_page":
                return {"ok": True, "tier": assistant_tools.TIER_READ,
                        "data": assistant_guide.find_pages(
                            args.get("topic"), caps, lambda key: labels.get(key, key))}
            if name == "list_endpoints":
                found = assistant_tools.matching_endpoints(
                    catalog, args.get("q"), lambda e: all(c in caps for c in e["caps"]))
                return {"ok": True, "tier": assistant_tools.TIER_READ,
                        "data": [{k: e[k] for k in ("method", "rule", "tier", "summary")}
                                 for e in found]}
            local = {}
            if name == "call_endpoint":
                args, local = assistant_tools.split_local(name, args)
                method = str(args.get("method") or "GET").upper()
                path = str(args.get("path") or "")
                error = assistant_tools.check_path(path)
                if error:
                    return {"ok": False, "error": error}
                query = args.get("query") if isinstance(args.get("query"), dict) else None
                body = args.get("body") if isinstance(args.get("body"), dict) else None
            elif name in assistant_tools.CURATED_BY_NAME:
                args, local = assistant_tools.split_local(name, args)
                error, method, path, query, body = assistant_tools.build_request(name, args)
                if error:
                    return {"ok": False, "error": error}
            else:
                return {"ok": False, "error": f"there is no tool called {name}"}

            rule, view_args = dispatcher.resolve(method, path)
            if not rule:
                return {"ok": False, "error": f"no route answers {method} {path}"}
            tier = assistant_tools.classify(method, rule)
            machine = str((view_args or {}).get("machine") or (body or {}).get("machine")
                          or (query or {}).get("machine") or "")
            if tier == assistant_tools.TIER_DENIED:
                return {"ok": False, "tier": tier,
                        "error": "the assistant may not use this route; the operator can "
                                 "do it in the console"}
            if tier == assistant_tools.TIER_CONFIRM:
                # The conversation's mode, read NOW rather than when the turn began: an
                # operator who switches back to Ask mid-answer is obeyed from the next action.
                chat_now = assistant.get_chat(db_path, chat_id, owner) or {}
                mode = chat_now.get("mode", assistant.MODE_ASK)
                risk = str(local.get("risk") or "").lower()
                typed = assistant_tools.needs_typed_name(method, rule)
                action = assistant.create_action(
                    db_path, chat_id=chat_id, owner=owner, tool=name, method=method,
                    path=path, rule=rule, query=query, body=body, machine=machine,
                    typed_name=typed, risk=risk, impact=str(local.get("impact") or ""),
                    mode=mode, auto=False)
                # A wipe keeps its card and its typed name in every mode (owner's decision).
                run_now = not typed and (mode == assistant.MODE_BYPASS or (
                    mode == assistant.MODE_AUTO and risk == assistant.RISK_ROUTINE))
                if not run_now:
                    return {"ok": True, "tier": tier, "status": "pending_confirmation",
                            "action": _public_action(action),
                            "note": "Not run. The operator sees a confirmation card and "
                                    "decides."}
                with assistant.get_conn(db_path) as conn:
                    conn.execute("UPDATE assistant_actions SET auto = 1 WHERE id = ?",
                                 (action["id"],))
                done, outcome, error = _run_action_now(action, session_data, owner)
                if error:
                    return {"ok": False, "tier": tier, "error": error}
                status, payload = outcome
                result = {"ok": status < 400, "tier": tier, "status": status,
                          "ran_without_asking": True, "mode": mode,
                          "action": _public_action(done)}
                if status >= 400:
                    result["error"] = (payload.get("error") if isinstance(payload, dict)
                                       else None) or f"HTTP {status}"
                else:
                    result["data"] = payload
                return result
            if name == "command_output" and local.get("wait_seconds"):
                try:
                    seconds = int(local.get("wait_seconds") or 0)
                except (TypeError, ValueError):
                    seconds = 0
                status, payload = _wait_for_command(session_data, path, query, seconds,
                                                    cancelled)
            else:
                status, payload = dispatcher.call(session_data, method, path, query, body)
            if status < 400:
                payload = assistant_tools.shape(name, payload, local)
            if tier == assistant_tools.TIER_WRITE:
                audit(db_path, actor=owner, action="assistant.tool", target=machine or path,
                      detail={"tool": name, "method": method, "rule": rule,
                              "status": status})
            if status >= 400:
                message = payload.get("error") if isinstance(payload, dict) else None
                return {"ok": False, "tier": tier, "status": status,
                        "error": message or f"HTTP {status}"}
            return {"ok": True, "tier": tier, "status": status, "data": payload}

        return execute

    # ---------------- Context and prompt ----------------

    def _context(raw):
        raw = raw if isinstance(raw, dict) else {}
        out = {}
        for key in ("path", "query", "hash", "title", "machine", "tab"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()[:MAX_CONTEXT_VALUE]
        selection = raw.get("selection")
        if isinstance(selection, list):
            out["selection"] = [str(s)[:100] for s in selection[:MAX_SELECTION]]
        # A machine named by the page is only context if the operator can see it. The panel
        # reads this from a URL, and a URL is something anybody can type.
        if out.get("machine") and not access.in_scope(out["machine"]):
            out.pop("machine")
        return out

    def _prompt(perms, context, mode=assistant.MODE_ASK):
        caps = sorted(perms.get("capabilities") or ())
        return assistant.system_prompt(
            operator=_owner(), capabilities=caps, scope=perms.get("machines"),
            pages=assistant_guide.pages_for(set(caps), translate), context=context,
            today=datetime.date.today().isoformat(), language=language(), mode=mode)

    # ---------------- Routes ----------------

    @bp.route("/assistant", methods=["GET"])
    @login_required
    @can_view
    def page():
        return render_template("assistant.html")

    @bp.route("/api/assistant/status", methods=["GET"])
    @login_required
    @can_view
    def status():
        config = ai_config()
        error, resolved = ai.provider_config(config)
        if not error and not setting("ai.assistant_enabled"):
            error = "the assistant is switched off in Settings, under AI"
        return jsonify({"ready": not error, "error": error or "",
                        "model": (resolved or {}).get("model", "") if not error else "",
                        "can_configure": access.can(permissions.MANAGE_SETTINGS)}), 200

    @bp.route("/api/assistant/chats", methods=["GET"])
    @login_required
    @can_view
    def chats():
        owner = _owner()
        running = runs.active_chats(owner)
        return jsonify({"chats": [dict(c, running=c["id"] in running)
                                  for c in assistant.list_chats(db_path, owner)]}), 200

    @bp.route("/api/assistant/chats", methods=["POST"])
    @login_required
    @can_view
    def new_chat():
        body = _json_body()
        mode = str(body.get("mode") or assistant.MODE_ASK)
        if mode not in assistant.MODES:
            return jsonify({"error": "unknown mode"}), 400
        chat = assistant.create_chat(db_path, _owner(), mode=mode)
        if mode != assistant.MODE_ASK:
            _audit_mode(chat, mode)
        return jsonify(chat), 201

    def _audit_mode(chat, mode):
        """Security level: the row an auditor needs is "who let the model act unasked, and
        when", for every conversation where it could."""
        audit(db_path, actor=_owner(), action="assistant.mode_changed", target=chat["id"],
              detail={"mode": mode, "title": chat.get("title", "")})

    @bp.route("/api/assistant/chats/<chat_id>", methods=["GET"])
    @login_required
    @can_view
    def get_chat(chat_id):
        chat = assistant.get_chat(db_path, chat_id, _owner())
        if not chat:
            return jsonify({"error": "no such conversation"}), 404
        shown = []
        for message in assistant.list_messages(db_path, chat_id):
            if message["role"] == assistant.ROLE_TOOL:
                continue
            if message["role"] == assistant.ROLE_ASSISTANT and not message["content"]:
                continue
            # `step`: an assistant message that called tools is the model thinking aloud on
            # its way to an answer, and the panel shows it smaller than the answer itself.
            shown.append({"id": message["id"], "role": message["role"],
                          "step": bool(message["tool_calls"]),
                          "content": (_render(message["content"])
                                      if message["role"] == assistant.ROLE_ASSISTANT
                                      else message["content"]),
                          "tools": [c["name"] for c in message["tool_calls"]],
                          "created_at": message["created_at"]})
        actions = [_public_action(a) for a in assistant.list_actions(db_path, chat_id)]
        # A turn still answering: the panel renders what is stored, then polls the run from
        # `seq` on, so nothing already shown arrives twice.
        active = runs.active(chat_id, _owner())
        run = {"id": active[0], "seq": active[1]} if active else None
        return jsonify({"chat": chat, "messages": shown, "actions": actions,
                        "run": run}), 200

    @bp.route("/api/assistant/chats/<chat_id>", methods=["PATCH"])
    @login_required
    @can_view
    def rename(chat_id):
        """Rename a conversation, or set its command mode. The operator's title is final: the
        model never replaces it. A mode is the operator's own choice for this conversation
        (any operator may choose any mode; their permissions still bound what runs)."""
        body = _json_body()
        owner = _owner()
        if not assistant.get_chat(db_path, chat_id, owner):
            return jsonify({"error": "no such conversation"}), 404
        chat = None
        if "mode" in body:
            mode = str(body.get("mode") or "")
            if mode not in assistant.MODES:
                return jsonify({"error": "unknown mode"}), 400
            chat = assistant.set_mode(db_path, chat_id, owner, mode)
            _audit_mode(chat, mode)
        if "title" in body:
            title = str(body.get("title") or "")
            if not assistant.clean_title(title):
                return jsonify({"error": "type a name for the conversation"}), 400
            chat = assistant.rename_chat(db_path, chat_id, owner, title)
        if chat is None:
            return jsonify({"error": "nothing to change"}), 400
        return jsonify(chat), 200

    @bp.route("/api/assistant/chats/<chat_id>", methods=["DELETE"])
    @login_required
    @can_view
    def delete_chat(chat_id):
        if not assistant.delete_chat(db_path, chat_id, _owner()):
            return jsonify({"error": "no such conversation"}), 404
        return jsonify({"status": "deleted"}), 200

    def _start_turn(chat, owner, text, context):
        """Start one turn in the pool. Returns (error, status, run_id).

        `text` None is a FOLLOW-UP turn with no operator message: the one the hub starts after
        an action is confirmed, so the model reads the outcome without being asked. It runs
        with the session of the request that started it -- the operator's, either way.
        """
        chat_id = chat["id"]
        config = ai_config()
        if not _enabled(config):
            return "the assistant is switched off in Settings, under AI", 400, None
        error, _resolved = ai.provider_config(config)
        if error:
            return error, 400, None
        run_id = runs.start(chat_id, owner)
        if not run_id:
            return "the assistant is still answering in this conversation", 409, None

        perms = access.current() or {}
        caps = set(perms.get("capabilities") or ())
        prompt = _prompt(perms, _context(context), chat.get("mode", assistant.MODE_ASK))
        tools = assistant_tools.tool_specs(_offered_curated(caps))
        execute = _make_executor(chat_id=chat_id, owner=owner,
                                 session_data=dict(session), caps=caps,
                                 cancelled=lambda: runs.cancelled(run_id))
        key = _key()
        try:
            max_steps = int(setting("ai.assistant_max_steps") or assistant.DEFAULT_MAX_STEPS)
        except (TypeError, ValueError):
            max_steps = assistant.DEFAULT_MAX_STEPS

        def step(messages, tool_specs):
            error, message = ai.complete_chat(config, messages, tool_specs, api_key=key)
            ai.record_request(db_path, actor=owner, kind=ai.KIND_ASSISTANT,
                              provider=str(config.get("provider") or ""),
                              model=str(config.get("model") or ""),
                              prompt_chars=sum(len(str(m.get("content") or ""))
                                               for m in messages),
                              outcome=ai.OUTCOME_PROVIDER_ERROR if error else ai.OUTCOME_OK,
                              error=error or "")
            return error, message

        def work():
            try:
                assistant.run_turn(db_path, chat_id, system_prompt=prompt, user_text=text,
                                   complete_step=step, tools=tools, execute=execute,
                                   emit=lambda e: runs.emit(run_id, e),
                                   cancelled=lambda: runs.cancelled(run_id),
                                   max_steps=max_steps)
            except Exception:                           # noqa: BLE001
                print(f"[assistant] turn failed:\n{traceback.format_exc()}")
                runs.emit(run_id, {"type": "error", "error": GENERIC_ERROR})

        def name_it():
            """Name the conversation from its first message, beside the answer rather than
            after it, so the reply is not kept waiting. Only a title that is still the
            first-message placeholder is replaced (assistant.rename_chat), and a provider that
            fails leaves that placeholder in place -- a worse title, not an error."""
            try:
                error, raw = ai.complete(config, assistant.title_messages(text, language_now),
                                         api_key=key, max_tokens=40)
                ai.record_request(db_path, actor=owner, kind=ai.KIND_ASSISTANT,
                                  provider=str(config.get("provider") or ""),
                                  model=str(config.get("model") or ""),
                                  prompt_chars=len(text),
                                  outcome=ai.OUTCOME_PROVIDER_ERROR if error else ai.OUTCOME_OK,
                                  error=error or "")
                if error:
                    return
                named = assistant.rename_chat(db_path, chat_id, owner, raw,
                                              source=assistant.TITLE_AI)
                if named and named["title_source"] == assistant.TITLE_AI:
                    runs.emit(run_id, {"type": "title", "title": named["title"]})
            except Exception:                           # noqa: BLE001
                print(f"[assistant] naming a conversation failed:\n{traceback.format_exc()}")

        language_now = language()
        pool.submit(work)
        if text and chat.get("title_source", assistant.TITLE_AUTO) == assistant.TITLE_AUTO \
                and not chat.get("title"):
            namer.submit(name_it)
        return None, 202, run_id

    @bp.route("/api/assistant/chats/<chat_id>/messages", methods=["POST"])
    @login_required
    @can_view
    def send(chat_id):
        owner = _owner()
        chat = assistant.get_chat(db_path, chat_id, owner)
        if not chat:
            return jsonify({"error": "no such conversation"}), 404
        body = _json_body()
        text = str(body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "type a message first"}), 400
        if len(text) > assistant.MAX_USER_CHARS:
            return jsonify({"error": f"that message is too long (limit "
                                     f"{assistant.MAX_USER_CHARS} characters)"}), 400
        error, status, run_id = _start_turn(chat, owner, text, body.get("context"))
        if error:
            return jsonify({"error": error}), status
        return jsonify({"run_id": run_id}), 202

    @bp.route("/api/assistant/runs/<run_id>", methods=["GET"])
    @login_required
    @can_view
    def poll(run_id):
        try:
            after = int(request.args.get("after_seq", -1))
        except (TypeError, ValueError):
            return jsonify({"error": "after_seq must be an integer"}), 400
        state = runs.read(run_id, _owner(), after)
        if state is None:
            return jsonify({"error": "no such run"}), 404
        # Copies. runs.read() hands back the stored dicts, and rendering one in place meant a
        # second poll of the same event (a retry, a re-poll from -1) rendered it AGAIN --
        # render_links() first reduces every markdown link to its label, hub-built ones
        # included, so the links vanished.
        state["events"] = [dict(event, content=_render(event.get("content", "")))
                           if event.get("type") == "text" else dict(event)
                           for event in state["events"]]
        return jsonify(state), 200

    @bp.route("/api/assistant/runs/<run_id>/cancel", methods=["POST"])
    @login_required
    @can_view
    def cancel(run_id):
        if not runs.cancel(run_id, _owner()):
            return jsonify({"error": "no such run"}), 404
        return jsonify({"status": "stopping"}), 200

    @bp.route("/api/assistant/actions/<action_id>/confirm", methods=["POST"])
    @login_required
    @can_view
    def confirm(action_id):
        """Run a queued action, ONCE, as the operator pressing the button now."""
        owner = _owner()
        action = assistant.get_action(db_path, action_id, owner)
        if not action:
            return jsonify({"error": "no such action"}), 404
        body = _json_body()
        if action["typed_name"]:
            typed = str(body.get("typed") or "").strip()
            if not action["machine"] or typed.lower() != action["machine"].lower():
                return jsonify({"error": "type the machine's name exactly to confirm"}), 400
        error, action = assistant.claim_action(db_path, action_id, owner)
        if error:
            return jsonify({"error": error}), 409
        call_body = action["body"]
        if action["typed_name"]:
            # The route checks the typed name itself (wipe.confirm_wipe); it is the
            # operator's typing that goes in, never a value the model filled.
            call_body = dict(call_body or {}, confirm=str(body.get("typed") or "").strip())
        try:
            status_code, payload = dispatcher.call(dict(session), action["method"],
                                                   action["path"], action["query"], call_body)
        except Exception:                               # noqa: BLE001
            print(f"[assistant] confirmed action failed:\n{traceback.format_exc()}")
            status_code, payload = 500, {"error": GENERIC_ERROR}
        ok = status_code < 400
        action = assistant.finish_action(db_path, action["id"], owner, ok,
                                         {"status": status_code, "data": payload})
        audit(db_path, actor=owner, action="assistant.action_confirmed",
              target=action["machine"] or action["path"],
              detail={"action": action["id"], "tool": action["tool"],
                      "method": action["method"], "rule": action["rule"],
                      "status": status_code})
        summary = str(payload)[:1500]
        assistant.append_message(
            db_path, action["chat_id"], assistant.ROLE_NOTE,
            f"The operator confirmed action {action['id']} ({action['method']} "
            f"{action['path']}). The hub answered HTTP {status_code}: {summary}. "
            "Find out what happened now -- for a fleet command, command_output with its "
            "command_id and wait_seconds; for a download, staged_download with its id and "
            "wait_seconds -- and tell the operator.")
        # The follow-up turn: confirming used to queue the command and stop there, so the
        # operator had to ask "did it work?" every time (seen on a real hub, five times in
        # one conversation). The model now reads the outcome on its own. Started for a
        # refusal too, so the model can say why it did not run.
        follow_up = None
        chat = assistant.get_chat(db_path, action["chat_id"], owner)
        if chat:
            _error, _status, follow_up = _start_turn(chat, owner, None, body.get("context"))
        return jsonify({"action": _public_action(action), "run_id": follow_up}),             (200 if ok else 400)

    @bp.route("/api/assistant/actions/<action_id>/reject", methods=["POST"])
    @login_required
    @can_view
    def reject(action_id):
        owner = _owner()
        action = assistant.get_action(db_path, action_id, owner)
        if not action:
            return jsonify({"error": "no such action"}), 404
        if not assistant.reject_action(db_path, action_id, owner):
            return jsonify({"error": "this action is no longer pending"}), 409
        audit(db_path, actor=owner, action="assistant.action_rejected",
              target=action["machine"] or action["path"],
              detail={"action": action["id"], "tool": action["tool"]})
        assistant.append_message(
            db_path, action["chat_id"], assistant.ROLE_NOTE,
            f"The operator declined action {action['id']} ({action['method']} "
            f"{action['path']}). It was not run.")
        return jsonify({"action": _public_action(
            assistant.get_action(db_path, action_id, owner))}), 200

    bp.dispatcher = dispatcher
    bp.runs = runs
    return bp
