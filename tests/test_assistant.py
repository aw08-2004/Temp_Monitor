"""The console assistant (roadmap #26) -- driven through the real app with a scripted model.

**The silent failures this file exists to catch:**

  * **A confirm-tier call that runs without a click.** A model asked to "restart PC-01" that
    queues the restart itself looks like a helpful assistant right up to the day it is talked
    into a wipe. So a tool call for a command must leave NO command row, only a pending action
    -- and the action must run once, for its owner, and never again.
  * **A tool that reaches past the operator.** The assistant runs routes as the operator, so a
    machine outside their scope must come back as a refusal, and a route they lack the
    capability for must not even be listed to the model.
  * **A link the model invented.** Only links the hub built from a link token, for a machine
    the viewer can see, may reach the page. A hand-written markdown URL is reduced to its text.
  * **A tier table that names a route that does not exist.** A typo in assistant_tools.py does
    not raise -- the misspelled route simply falls through to the default tier. Every rule the
    tables name is checked against the live url_map.

Run from the repo root so `import app` resolves.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.join(os.path.dirname(HERE), "hub")
sys.path.insert(0, HUB)

_TMPDIR = tempfile.mkdtemp(prefix="hub-assistant-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import ai
import app
import assistant
import assistant_guide
import assistant_tools
import console_session
import permissions
import settings

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


DB = app.DB_PATH

# ---------------------------------------------------------------- the scripted provider
SCRIPT = []          # responses, consumed in order; a callable is called with (messages)
CALLS = []           # (messages, tool names) for every provider call


def fake_complete_chat(config, messages, tools, **kwargs):
    CALLS.append((messages, [t["function"]["name"] for t in tools or []]))
    if not SCRIPT:
        return None, {"content": "done", "tool_calls": []}
    step = SCRIPT.pop(0)
    if callable(step):
        step = step(messages)
    if isinstance(step, str):
        return step, None                       # a provider error
    return None, step


def call(name, **arguments):
    return {"content": "", "tool_calls": [{"id": f"c{len(CALLS)}", "name": name,
                                           "arguments": json.dumps(arguments)}]}


def say(text):
    return {"content": text, "tool_calls": []}


ai.complete_chat = fake_complete_chat


# ---------------------------------------------------------------- fixtures
def seed():
    conn = sqlite3.connect(DB)
    for name in ("PC-01", "PC-02"):
        conn.execute("INSERT OR IGNORE INTO machine_info (machine) VALUES (?)", (name,))
    conn.commit()
    conn.close()
    permissions.create_group(DB, "Sales operators",
                             capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS,
                                           permissions.WIPE_DEVICE],
                             machines=["PC-01"], members=["scoped@x.com"])
    permissions.create_group(DB, "Readers", capabilities=[permissions.VIEW],
                             machines=["PC-01", "PC-02"], members=["viewer@x.com"])
    settings.set_many(DB, {"ai.enabled": True, "ai.base_url": "http://127.0.0.1:9",
                           "ai.model": "test-model", "ai.assistant_max_steps": 4,
                           "ai.assistant_enabled": True}, "test")
    settings.invalidate()


def client_for(email):
    client = app.app.test_client()
    console_session.sign_in(client, email)
    return client


def converse(client, chat_id, text, context=None, timeout=10.0):
    """Send one message and poll the run to the end. Returns (status, events)."""
    r = client.post(f"/api/assistant/chats/{chat_id}/messages",
                    json={"text": text, "context": context or {}})
    if r.status_code != 202:
        return r.status_code, [r.get_json()]
    run_id = r.get_json()["run_id"]
    deadline = time.time() + timeout
    events, after = [], -1
    while time.time() < deadline:
        state = client.get(f"/api/assistant/runs/{run_id}?after_seq={after}").get_json()
        for event in state["events"]:
            events.append(event)
            after = max(after, event["seq"])
        if state["done"]:
            return 202, events
        time.sleep(0.02)
    return 0, events


def new_chat(client):
    return client.post("/api/assistant/chats", json={}).get_json()["id"]


def command_rows(machine):
    conn = sqlite3.connect(DB)
    try:
        return conn.execute("SELECT COUNT(*) FROM commands WHERE machine = ?",
                            (machine,)).fetchone()[0]
    finally:
        conn.close()


def tool_messages(chat_id):
    return [m for m in assistant.list_messages(DB, chat_id) if m["role"] == "tool"]


# ---------------------------------------------------------------- the tier tables
def test_every_named_route_exists():
    print("\n-- every rule the tier tables and curated tools name is a real route --")
    routes = {(m, r.rule) for r in app.app.url_map.iter_rules() for m in r.methods}
    norm = {(m, re.sub(r"<[^>]+>", "<>", rule)) for m, rule in routes}
    for table_name in ("DENIED_ROUTES", "READ_POSTS", "WRITE_ROUTES", "TYPED_CONFIRM_ROUTES"):
        table = getattr(assistant_tools, table_name)
        # Two spellings of one alert route are listed on purpose (converter or not); at least
        # one of them must exist, and every other entry must exist exactly.
        missing = [entry for entry in table
                   if (entry[0], re.sub(r"<[^>]+>", "<>", entry[1])) not in norm]
        check(f"{table_name}: every entry is a live route ({missing[:3]})", not missing)
    missing = [name for name, (_d, method, path, _s) in assistant_tools.CURATED_BY_NAME.items()
               if (method, re.sub(r"<[^>]+>", "<>", path)) not in norm]
    check(f"every curated tool resolves to a live route ({missing[:3]})", not missing)
    denied = [name for name, (_d, method, path, _s) in assistant_tools.CURATED_BY_NAME.items()
              if assistant_tools.classify(method, re.sub(r"<[^>]+>", "<x>", path)) == "denied"]
    check(f"no curated tool is denied by its own tier table ({denied})", not denied)


def test_tiers_of_the_routes_that_matter():
    print("\n-- the dangerous routes are in the tier they must be in --")
    c = assistant_tools.classify
    check("reading a machine runs at once", c("GET", "/api/machines/<machine>") == "read")
    check("queueing a command waits for a click",
          c("POST", "/api/fleet/commands") == "confirm")
    check("a wipe waits for a click", c("POST", "/api/wipe/machines/<machine>/wipe") == "confirm")
    check("...and for the machine's name to be typed",
          assistant_tools.needs_typed_name("POST", "/api/wipe/machines/<machine>/wipe"))
    check("revealing a BitLocker key is never reachable",
          c("POST", "/api/bitlocker/<machine>/reveal") == "denied")
    check("the interactive terminal is never reachable",
          c("POST", "/api/fleet/pty/<sid>/input") == "denied")
    check("agent routes are never reachable", c("POST", "/api/agent/commands") == "denied")
    check("the assistant cannot call itself",
          c("POST", "/api/assistant/chats/<chat_id>/messages") == "denied")
    check("changing settings waits for a click", c("POST", "/api/settings") == "confirm")
    check("an unlisted write route defaults to confirm, not write",
          c("POST", "/api/some/new/route") == "confirm")
    check("a page route is not callable", c("GET", "/machine/<machine>") == "denied")
    check("the agents' ingest is denied", c("POST", "/api/report") == "denied")
    check("...without swallowing the reports API",
          c("GET", "/api/reports/machines/<machine>") == "read")
    check("a fetched file's contents never reach the model",
          c("GET", "/api/machines/<machine>/files/transfers/<transfer_id>/content") == "denied")


def test_path_checks():
    print("\n-- call_endpoint only takes a plain API path --")
    bad = ["/api/../login", "//evil/api/x", "/api/x?y=1", "https://x/api/y", "/login",
           "/api/x\\y"]
    for path in bad:
        check(f"refused: {path}", assistant_tools.check_path(path) is not None)
    check("a plain path passes", assistant_tools.check_path("/api/machines/PC-01") is None)


# ---------------------------------------------------------------- the guide
def test_page_map_matches_the_app():
    print("\n-- the assistant's page map and the sidebar agree --")
    adapter = app.app.url_map.bind("localhost")
    for key, path, _label, _cap, _about in assistant_guide.PAGES:
        try:
            adapter.match(path, method="GET")
            ok = True
        except Exception:
            ok = False
        check(f"page {key} ({path}) is a real GET route", ok)
    with open(os.path.join(HUB, "templates", "partials", "_sidebar.html"),
              encoding="utf-8") as handle:
        sidebar = handle.read()
    prefixes = re.findall(r'data-nav-prefix="([^"]+)"', sidebar)
    paths = {p[1] for p in assistant_guide.PAGES}
    missing = [p.split()[0] for p in prefixes if p.split()[0] not in paths]
    check(f"every sidebar link is in the page map ({missing})", not missing)


# ---------------------------------------------------------------- links
def test_links_are_built_by_the_hub_only():
    print("\n-- only hub-built links survive rendering --")

    def resolve(kind, arg, opts):
        return ("/machine/PC-01", "PC-01") if arg == "PC-01" else None

    text, links = assistant.render_links(
        "See [[machine:PC-01]], [[machine:PC-02]] and [click](https://evil.example/x) "
        "or [here](/api/wipe/machines/PC-01/wipe).", resolve)
    check("a resolvable token became a link", "[PC-01](/machine/PC-01)" in text)
    check("an unresolvable token became plain text",
          "PC-02" in text and "(/machine/PC-02)" not in text)
    check("a hand-written external link lost its url", "evil.example" not in text)
    check("a hand-written internal link lost its url too",
          "/api/wipe" not in text and "here" in text)
    check("exactly one link was emitted", links == ["/machine/PC-01"])


def test_history_is_trimmed():
    print("\n-- old tool results go back to the model trimmed --")
    rows = [{"role": "tool", "content": "x" * 50, "tool_calls": [], "tool_call_id": "a",
             "tool_name": "t"},
            {"role": "user", "content": "hi", "tool_calls": [], "tool_call_id": "",
             "tool_name": ""},
            {"role": "tool", "content": "y" * 5000, "tool_calls": [], "tool_call_id": "b",
             "tool_name": "t"}]
    out = assistant.history_for_model(rows)
    check("the window starts at a user message", out[0]["role"] == "user")
    check("a long tool result is cut", len(out[1]["content"]) < 1000
          and "call the tool again" in out[1]["content"])


def test_actions_expire():
    print("\n-- a pending action cannot be confirmed after it expires --")
    chat = assistant.create_chat(DB, "unit@x.com")
    action = assistant.create_action(DB, chat_id=chat["id"], owner="unit@x.com", tool="t",
                                     method="POST", path="/api/x", rule="/api/x",
                                     now=time.time() - assistant.ACTION_TTL_SECONDS - 5)
    error, claimed = assistant.claim_action(DB, action["id"], "unit@x.com")
    check("an expired action is refused", error is not None and claimed is None)
    check("...and marked expired",
          assistant.get_action(DB, action["id"], "unit@x.com")["state"] == "expired")


# ---------------------------------------------------------------- end to end
def test_a_command_waits_for_its_confirmation():
    print("\n-- a restart is queued as an action, not as a command --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    SCRIPT[:] = [call("run_command", machine="PC-01", type="restart"),
                 say("Queued a restart of [[machine:PC-01|tab=overview]]. "
                     "Details: [docs](https://evil.example)")]
    status, events = converse(scoped, chat_id, "restart PC-01",
                              context={"path": "/machine/PC-01", "machine": "PC-01"})
    check("the turn finished", status == 202 and events and events[-1]["type"] == "done")
    actions = [e["action"] for e in events if e.get("action")]
    check("one action is pending", len(actions) == 1 and actions[0]["state"] == "pending")
    check("NO command row exists yet", command_rows("PC-01") == 0)
    finals = [e["content"] for e in events if e["type"] == "text"]
    check("the answer links the machine through the hub",
          any("[PC-01](/machine/PC-01?tab=overview)" in f for f in finals))
    check("the model's own url is gone", not any("evil.example" in f for f in finals))
    prompt = CALLS[-1][0][0]["content"]
    check("the page context reached the prompt", "machine: PC-01" in prompt)

    other = client_for("viewer@x.com")
    action_id = actions[0]["id"]
    check("another operator cannot confirm it",
          other.post(f"/api/assistant/actions/{action_id}/confirm", json={}).status_code == 404)
    check("...or read the conversation",
          other.get(f"/api/assistant/chats/{chat_id}").status_code == 404)

    r = scoped.post(f"/api/assistant/actions/{action_id}/confirm", json={})
    body = r.get_json() or {}
    check(f"the owner's confirm ran it (HTTP {r.status_code}, "
          f"{(body.get('action') or {}).get('result')})",
          (body.get("action") or {}).get("state") in ("done", "failed"))
    if (body.get("action") or {}).get("state") == "done":
        check("...and the command row exists now", command_rows("PC-01") == 1)
    check("a second confirm is refused",
          scoped.post(f"/api/assistant/actions/{action_id}/confirm", json={}).status_code == 409)
    notes = [m for m in assistant.list_messages(DB, chat_id) if m["role"] == "note"]
    check("the conversation records what the operator decided", len(notes) == 1)


def test_scope_is_the_operators():
    print("\n-- a machine outside scope is refused by the route itself --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    SCRIPT[:] = [call("get_machine", machine="PC-02"), say("I cannot see PC-02.")]
    converse(scoped, chat_id, "what about PC-02?")
    results = [json.loads(m["content"]) for m in tool_messages(chat_id)]
    check("the tool came back refused", results and results[0]["ok"] is False
          and results[0].get("status") == 403)
    SCRIPT[:] = [call("run_command", machine="PC-02", type="restart"), say("ok")]
    converse(scoped, chat_id, "restart PC-02")
    pending = [a for a in assistant.list_actions(DB, chat_id) if a["machine"] == "PC-02"]
    check("an out-of-scope command can be QUEUED as a card...", len(pending) == 1)
    r = scoped.post(f"/api/assistant/actions/{pending[0]['id']}/confirm", json={})
    check("...but confirming it is refused by the route, not run",
          r.status_code == 400 and command_rows("PC-02") == 0)


def test_denied_routes_stay_denied():
    print("\n-- a denied route cannot be reached through call_endpoint --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    SCRIPT[:] = [call("call_endpoint", method="POST", path="/api/bitlocker/PC-01/reveal"),
                 call("call_endpoint", method="GET", path="/api/../logout"),
                 say("no")]
    converse(scoped, chat_id, "show me the bitlocker key")
    results = [json.loads(m["content"]) for m in tool_messages(chat_id)]
    check("the reveal was refused as denied",
          results[0]["ok"] is False and results[0].get("tier") == "denied")
    check("the dot-segment path was refused", results[1]["ok"] is False)
    check("no action was queued for either", not assistant.list_actions(DB, chat_id))


def test_tools_follow_capabilities():
    print("\n-- the model is only offered what the operator can use --")
    viewer = client_for("viewer@x.com")
    chat_id = new_chat(viewer)
    SCRIPT[:] = [call("list_endpoints", q="fleet commands"), say("ok")]
    converse(viewer, chat_id, "what can I run?")
    offered = CALLS[-1][1]
    check("a viewer is not offered run_command", "run_command" not in offered)
    check("...but is offered get_machine", "get_machine" in offered)
    listing = json.loads(tool_messages(chat_id)[0]["content"])["data"]
    check("list_endpoints hides the command POST from a viewer",
          not any(e["method"] == "POST" and e["rule"] == "/api/fleet/commands" for e in listing))
    check("...and never names a denied route",
          not any(e["tier"] == "denied" for e in listing))


def test_wipe_needs_the_typed_name():
    print("\n-- a wipe card needs the machine's name typed, not just a click --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    SCRIPT[:] = [call("call_endpoint", method="POST", path="/api/wipe/machines/PC-01/wipe",
                      body={"confirm": "PC-01"}),
                 say("queued")]
    converse(scoped, chat_id, "wipe PC-01")
    action = assistant.list_actions(DB, chat_id)[0]
    check("the action asks for the typed name", action["typed_name"] is True)
    r = scoped.post(f"/api/assistant/actions/{action['id']}/confirm", json={"typed": "PC-1"})
    check("a wrong name is refused", r.status_code == 400)
    check("...and the action is still pending",
          assistant.get_action(DB, action["id"], "scoped@x.com")["state"] == "pending")


def test_the_loop_ends():
    print("\n-- a model that never stops calling tools is stopped --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    SCRIPT[:] = [call("fleet_summary") for _ in range(10)]
    before = len(CALLS)
    _status, events = converse(scoped, chat_id, "loop")
    check("it stopped at the step limit", len(CALLS) - before == 4)
    check("...and said so", any("step limit" in (e.get("content") or "") for e in events))
    SCRIPT[:] = []

    SCRIPT[:] = ["the AI provider could not be reached"]
    _status, events = converse(scoped, chat_id, "hello")
    check("a provider failure ends the turn with its sentence",
          events and events[-1]["type"] == "error"
          and "could not be reached" in events[-1]["error"])


def test_off_switch():
    print("\n-- the assistant switch is obeyed on the next request --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    settings.set_many(DB, {"ai.assistant_enabled": False}, "test")
    settings.invalidate()
    r = scoped.post(f"/api/assistant/chats/{chat_id}/messages", json={"text": "hi"})
    check("a message is refused while it is off", r.status_code == 400)
    check("the status says it is off",
          scoped.get("/api/assistant/status").get_json()["ready"] is False)
    settings.set_many(DB, {"ai.assistant_enabled": True}, "test")
    settings.invalidate()




# ---------------------------------------------------------------- found in review (PR #97)
def test_assistant_is_off_until_turned_on():
    print("\n-- the assistant is off by default, even on a hub that already uses AI --")
    check("ai.assistant_enabled defaults to False",
          settings.BY_KEY["ai.assistant_enabled"].default is False)


def test_an_encoded_slash_cannot_change_the_route():
    """The tier was decided on the encoded path while the test client routed the decoded
    one, so `PC-01%2Fdownload` classified as a machine read and ran as a backup download."""
    print("\n-- an encoded slash is classified as the route it decodes to --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    SCRIPT[:] = [call("call_endpoint", method="GET",
                      path="/api/backups/machines/PC-01%2Fdownload"),
                 call("get_machine", machine="PC-01/download"),
                 say("no")]
    converse(scoped, chat_id, "download the backup")
    results = [json.loads(m["content"]) for m in tool_messages(chat_id)]
    check("the encoded download path is denied, not read",
          results[0]["ok"] is False and results[0].get("tier") == "denied")
    check("...and a slash smuggled into a curated tool's argument is refused too",
          results[1]["ok"] is False and results[1].get("tier") in ("denied", None)
          and "data" not in results[1])
    rule, _args = app.app.blueprints["assistant"].dispatcher.resolve(
        "GET", "/api/machines/PC-01%2F..%2F..%2Flogin")
    check("an encoded dot segment resolves to nothing", rule is None)


def test_find_page_works_from_the_worker():
    print("\n-- find_page runs on the worker thread, where there is no request --")
    viewer = client_for("viewer@x.com")
    chat_id = new_chat(viewer)
    SCRIPT[:] = [call("find_page", topic="alerts"), say("there")]
    converse(viewer, chat_id, "where are alerts?")
    result = json.loads(tool_messages(chat_id)[0]["content"])
    check("find_page answered", result["ok"] is True)
    check("...with the translated label",
          any(p["key"] == "alerts" and p["label"] == "Alerts" for p in result["data"]))


def test_a_second_poll_keeps_the_links():
    print("\n-- polling the same event twice renders it the same way twice --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    SCRIPT[:] = [say("See [[machine:PC-01]].")]
    r = scoped.post(f"/api/assistant/chats/{chat_id}/messages", json={"text": "hi"})
    run_id = r.get_json()["run_id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        if scoped.get(f"/api/assistant/runs/{run_id}?after_seq=-1").get_json()["done"]:
            break
        time.sleep(0.02)
    texts = []
    for _ in range(2):
        events = scoped.get(f"/api/assistant/runs/{run_id}?after_seq=-1").get_json()["events"]
        texts.append([e["content"] for e in events if e["type"] == "text"][0])
    check("the first poll has the link", "[PC-01](/machine/PC-01)" in texts[0])
    check("...and so does the second", texts[0] == texts[1])


def test_a_stop_mid_step_answers_every_call():
    """An assistant message whose tool_calls have no replies is refused by OpenAI-compatible
    servers, on this turn and every later one."""
    print("\n-- stopping between two tool calls still answers both --")
    chat = assistant.create_chat(DB, "unit-stop@x.com")
    flags = {"stop": False}

    def execute(name, arguments):
        flags["stop"] = True
        return {"ok": True, "data": {}}

    def step(messages, tools):
        return None, {"content": "", "tool_calls": [
            {"id": "a", "name": "fleet_summary", "arguments": "{}"},
            {"id": "b", "name": "list_alerts", "arguments": "{}"}]}

    events = []
    assistant.run_turn(DB, chat["id"], system_prompt="s", user_text="go", complete_step=step,
                       tools=[], execute=execute, emit=events.append,
                       cancelled=lambda: flags["stop"])
    replies = {m["tool_call_id"] for m in assistant.list_messages(DB, chat["id"])
               if m["role"] == "tool"}
    check("the turn reported that it stopped", events[-1].get("error") == "stopped")
    check("both call ids have a reply", replies == {"a", "b"})


def test_long_results_keep_whole_entries():
    """The first cut sliced the JSON at the limit: half an object, and a note to narrow a
    request the tool could not narrow."""
    print("\n-- an oversized result is shortened to whole entries --")
    result = {"ok": True, "data": {"rules": [{"id": i, "name": "x" * 200} for i in range(500)],
                                   "total": 500}}
    content = assistant.fit_result(result, limit=5000)
    parsed = json.loads(content)
    check("the shortened result is still valid JSON", isinstance(parsed, dict))
    check("it fits the limit", len(content) <= 5000)
    shown = parsed["truncated"]["lists"]["rules"]["shown"]
    check("it keeps whole entries and says how many",
          parsed["truncated"]["lists"]["rules"]["total"] == 500
          and len(parsed["data"]["rules"]) == shown > 0)
    small = {"ok": True, "data": [1, 2, 3]}
    check("a result that fits is untouched", assistant.fit_result(small) == json.dumps(small))


def test_disabling_a_rule_by_name():
    """The question that ran out of steps on a real hub: 'disable the high temperature
    alerts'. Two tool calls now -- a filtered list, then a confirmed switch."""
    print("\n-- finding and disabling a rule takes one list and one confirmed switch --")
    import rules as rules_module
    hot = [r for r in rules_module.list_rules(DB) if "temperature" in r["name"].lower()]
    check("the hub has a temperature rule to find", bool(hot))
    if not hot:
        return
    rule_id = hot[0]["id"]
    rules_module.set_rule_enabled(DB, rule_id, True, actor="test")
    for n in range(40):
        rules_module.save_rule(DB, {"name": f"Filler {n}", "description": "y" * 300,
                                    "condition_text": "metric.cpu_temp > 1000",
                                    "target": {"include": [{"kind": "all"}]},
                                    "actions": [{"type": "alert", "params": {"text": "z"}}],
                                    "for_seconds": 0, "cooldown_seconds": 3600},
                               actor="test")
    super_client = client_for("tester@example.com")
    chat_id = new_chat(super_client)
    SCRIPT[:] = [call("list_rules", q="temperature"),
                 call("set_rule_enabled", rule_id=rule_id, enabled=False),
                 say("Queued switching it off.")]
    converse(super_client, chat_id, "Can you disable the high temperature alerts?")
    results = [json.loads(m["content"]) for m in tool_messages(chat_id)]
    listing = results[0]["data"]
    check("the filtered list fits without trimming", "truncated" not in results[0])
    check("...and holds only the temperature rule(s)",
          listing["matched"] >= 1 and listing["total"] > listing["matched"]
          and all("temperature" in (r["name"] + r["condition"]).lower()
                  for r in listing["rules"]))
    check("each row is short: no condition AST, no target",
          all(set(r) == {"id", "name", "enabled", "condition", "actions", "matching",
                         "blocked"} for r in listing["rules"]))
    check("the switch waits for a click", results[1].get("status") == "pending_confirmation")
    check("...and has not run", rules_module.get_rule(DB, rule_id)["enabled"] is True)
    action_id = results[1]["action"]["id"]
    r = super_client.post(f"/api/assistant/actions/{action_id}/confirm", json={})
    check("confirming it switches the rule off",
          r.status_code == 200 and rules_module.get_rule(DB, rule_id)["enabled"] is False)


def test_any_answer_can_be_narrowed():
    """'Which deployment is stuck?' ran out of steps against a route with no filter, the same
    way the rules question had. Narrowing is now the model's to apply to any read."""
    print("\n-- fields / where / limit narrow any list in any answer --")
    payload = {"deployments": [
        {"id": "a1", "package_name": "7-Zip", "target_counts": {"succeeded": 40}},
        {"id": "b2", "package_name": "Chrome", "target_counts": {"in_flight": 1, "succeeded": 9}},
        {"id": "c3", "package_name": "chrome beta", "target_counts": {"failed": 2}}],
        "can_manage": True}
    out = assistant_tools.narrow(payload, {"where": {"target_counts.in_flight": 1},
                                           "fields": ["id", "package_name"]})
    check("a dotted where finds the stuck one",
          out["deployments"] == [{"id": "b2", "package_name": "Chrome"}])
    check("...and reports how many it kept of how many",
          out["_narrowed"] == {"path": "deployments", "total": 3, "matched": 1, "shown": 1})
    check("other keys of the answer are kept", out["can_manage"] is True)
    out = assistant_tools.narrow(payload, {"where": {"package_name": "CHROME"}})
    check("text matches case-insensitively as a substring", len(out["deployments"]) == 2)
    out = assistant_tools.narrow(payload, {"where": {"target_counts.failed": "2"}})
    check("a number written as text still matches", [d["id"] for d in out["deployments"]] == ["c3"])
    nulls = {"rows": [{"id": 1, "finished_at": None}, {"id": 2}, {"id": 3, "finished_at": 5}]}
    out = assistant_tools.narrow(nulls, {"where": {"finished_at": None}})
    check("where null matches an explicit null, not a missing key",
          [r["id"] for r in out["rows"]] == [1])
    out = assistant_tools.narrow(payload, {"limit": 1})
    check("limit keeps the first entries", len(out["deployments"]) == 1)
    check("no narrowing leaves the answer untouched",
          assistant_tools.narrow(payload, {}) is payload)

    specs = {s["function"]["name"]: s["function"]["parameters"]["properties"]
             for s in assistant_tools.tool_specs({"list_deployments", "run_command"})}
    check("GET tools are described with fields/where/limit",
          {"fields", "where", "limit"} <= set(specs["list_deployments"]))
    check("...and so is call_endpoint", "where" in specs["call_endpoint"])
    check("...but not a write tool", "where" not in specs["run_command"])

    viewer = client_for("viewer@x.com")
    chat_id = new_chat(viewer)
    SCRIPT[:] = [call("list_machines", where={"machine": "PC-02"}, fields=["machine"]),
                 call("call_endpoint", method="GET", path="/api/machines",
                      where={"machine": "PC-01"}, fields=["machine"]),
                 say("ok")]
    converse(viewer, chat_id, "find PC-02")
    results = [json.loads(m["content"]) for m in tool_messages(chat_id)]
    check("a curated tool narrows the real route's answer",
          results[0]["data"].get("items") == [{"machine": "PC-02"}])
    check("...and so does call_endpoint",
          results[1]["data"].get("items") == [{"machine": "PC-01"}])


def test_trimming_never_cuts_an_id():
    print("\n-- trimming cuts long text, never an id --")
    long_id = "d" * 400
    rows = [{"id": long_id, "deployment_id": long_id, "note": "n" * 2000} for _ in range(30)]
    parsed = json.loads(assistant.fit_result({"ok": True, "data": {"rows": rows}}, limit=8000))
    kept = parsed["data"]["rows"]
    check("ids survive whole", kept and all(r["id"] == long_id and r["deployment_id"] == long_id
                                            for r in kept))
    check("long text is cut", all(len(r["note"]) < 400 for r in kept))
    check("the hint names the narrowing arguments",
          "`where`" in parsed["truncated"]["hint"] and "`fields`" in parsed["truncated"]["hint"])


def test_narrowing_reaches_nested_lists():
    """An audit of every read route found the big lists a level or more down -- a history
    series, a report section, a backup manifest -- where top-level narrowing did nothing."""
    print("\n-- narrowing reaches a list at any depth --")
    history = {"machine": "PC-01", "metrics": {
        "temp": [[t, 40 + t % 30] for t in range(500)],
        "cpu_load": [[t, t % 100] for t in range(500)]}}
    out = assistant_tools.narrow(history, {"list": "metrics.temp", "limit": 5})
    check("path picks a nested series", out["metrics"]["temp"] == [[t, 40 + t % 30]
                                                                   for t in range(5)])
    check("...leaves its neighbours alone", len(out["metrics"]["cpu_load"]) == 500)
    check("...and names the other lists",
          out["_narrowed"]["other_lists"] == {"metrics.cpu_load": 500})
    out = assistant_tools.narrow(history, {"list": "metrics.temp", "where": {"x": 1}})
    check("where on a series says why it cannot",
          "error" in out and "metrics.temp" in out["lists"])
    out = assistant_tools.narrow(history, {"list": "metrics.nope", "limit": 1})
    check("an unknown path lists the real ones", set(out["lists"]) == {"metrics.temp",
                                                                      "metrics.cpu_load"})

    sheet = {"machine": "PC-01", "sections": {
        "hardware": {"data": {"cpu": "x"}},
        "software": {"data": [{"name": f"App {i}", "version": "1"} for i in range(300)]
                     + [{"name": "Google Chrome", "version": "120"}]},
        "groups": {"data": [{"name": "Sales"}]}}}
    out = assistant_tools.narrow(sheet, {"where": {"name": "chrome"}})
    check("without a path the largest list of objects is narrowed",
          out["_narrowed"]["path"] == "sections.software.data"
          and out["sections"]["software"]["data"] == [{"name": "Google Chrome",
                                                       "version": "120"}])
    check("...and the original answer is not modified",
          len(sheet["sections"]["software"]["data"]) == 301)


def test_trimming_reaches_nested_lists():
    print("\n-- trimming shrinks the largest list wherever it is --")
    history = {"ok": True, "data": {"machine": "PC-01", "metrics": {
        name: [[t, t * 1.5] for t in range(4000)] for name in ("temp", "cpu_load", "memory")}}}
    content = assistant.fit_result(history, limit=6000)
    parsed = json.loads(content)
    series = parsed["data"]["metrics"]["temp"]
    check("a history answer fits and stays valid JSON", len(content) <= 6000)
    check("every series survives, thinned", all(parsed["data"]["metrics"][n]
                                                for n in ("temp", "cpu_load", "memory")))
    check("...evenly, keeping the newest point", series[-1] == [3999, 5998.5]
          and series[0][0] < 200)
    cut = parsed["truncated"]["lists"]
    check("...and says it sampled each one",
          cut["metrics.temp"]["sampled"] is True and cut["metrics.temp"]["total"] == 4000)

    sheet = {"ok": True, "data": {"sections": {"software": {"data": [
        {"id": i, "name": f"App {i}", "publisher": "p" * 900} for i in range(400)]}}}}
    parsed = json.loads(assistant.fit_result(sheet, limit=8000))
    rows = parsed["data"]["sections"]["software"]["data"]
    check("a nested list of objects keeps its first whole entries",
          rows and [r["id"] for r in rows] == list(range(len(rows))))
    check("...with long text cut inside them",
          all(len(r["publisher"]) < 400 for r in rows))
    nested = {"ok": True, "data": {"targets": [
        {"id": "t1", "result": {"stdout": "x" * 20000, "run_id": "r" * 500}}]}}
    parsed = json.loads(assistant.fit_result(nested, limit=3000))
    entry = parsed["data"]["targets"][0] if parsed["data"]["targets"] else {}
    check("an entry whose nested note is too long keeps the entry, not just the id",
          entry.get("id") == "t1" and len(entry["result"]["stdout"]) < 400
          and entry["result"]["run_id"] == "r" * 500)


def test_every_read_route_survives_narrowing_and_trimming():
    """The property, against the real routes: whatever shape a read answers with, inflating
    every list in it a hundredfold and passing it through narrow() and fit_result() gives
    valid JSON inside the limit, cut at entries -- never the raw character slice."""
    print("\n-- every read route's answer can be narrowed and trimmed --")
    admin = client_for("tester@example.com")
    fill = {"machine": "PC-01"}
    tried, sliced, crashed = 0, [], []

    def inflate(value, inside_list=False):
        # A hundredfold at the FIRST list level only. Inflating every level compounds -- a
        # list inside a list became 10,000x -- and turned /api/settings into a 1.7 GB answer
        # that took four minutes to trim, which tested nothing a real route can return.
        if isinstance(value, list):
            items = [inflate(v, True) for v in value]
            return items * 100 if items and not inside_list else items
        if isinstance(value, dict):
            return {k: inflate(v, inside_list) for k, v in value.items()}
        return value

    for rule in app.app.url_map.iter_rules():
        if "GET" not in rule.methods or not rule.rule.startswith("/api/"):
            continue
        if assistant_tools.classify("GET", rule.rule) != "read":
            continue
        if set(rule.arguments) - set(fill):
            continue
        path = re.sub(r"<(?:[a-z_]+:)?([a-z_]+)>", lambda m: fill[m.group(1)], rule.rule)
        body = admin.get(path).get_json(silent=True)
        if body is None:
            continue
        tried += 1
        try:
            big = inflate(body)
            narrowed = assistant_tools.narrow(big, {"limit": 3})
            for candidate in (big, narrowed):
                content = assistant.fit_result({"ok": True, "data": candidate}, limit=4000)
                json.loads(content)
                if len(content) > 4000 + 40 or content.endswith('narrow the request]"'):
                    sliced.append(rule.rule)
        except Exception as exc:                        # noqa: BLE001
            crashed.append(f"{rule.rule}: {type(exc).__name__} {exc}")
    check(f"read routes exercised ({tried})", tried >= 80)
    check(f"no route's answer crashes narrow/fit ({crashed[:3]})", not crashed)
    check(f"no route's answer falls back to a character slice ({sorted(set(sliced))[:5]})",
          not sliced)


def main():
    test_assistant_is_off_until_turned_on()
    seed()
    test_every_named_route_exists()
    test_tiers_of_the_routes_that_matter()
    test_path_checks()
    test_page_map_matches_the_app()
    test_links_are_built_by_the_hub_only()
    test_history_is_trimmed()
    test_actions_expire()
    test_a_command_waits_for_its_confirmation()
    test_scope_is_the_operators()
    test_denied_routes_stay_denied()
    test_tools_follow_capabilities()
    test_wipe_needs_the_typed_name()
    test_the_loop_ends()
    test_off_switch()
    test_an_encoded_slash_cannot_change_the_route()
    test_find_page_works_from_the_worker()
    test_a_second_poll_keeps_the_links()
    test_a_stop_mid_step_answers_every_call()
    test_long_results_keep_whole_entries()
    test_disabling_a_rule_by_name()
    test_any_answer_can_be_narrowed()
    test_trimming_never_cuts_an_id()
    test_narrowing_reaches_nested_lists()
    test_trimming_reaches_nested_lists()
    test_every_read_route_survives_narrowing_and_trimming()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
