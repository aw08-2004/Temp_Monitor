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

# The conversation-naming call (assistant_web.name_it). A str is the title the "model" answers
# with, None is a provider failure; consumed in order, last one repeated.
TITLES = ["Test conversation"]
TITLE_CALLS = []


def fake_complete(config, messages, **kwargs):
    TITLE_CALLS.append(messages)
    answer = TITLES.pop(0) if len(TITLES) > 1 else TITLES[0]
    if answer is None:
        return "the AI provider could not be reached", None
    return None, answer


ai.complete = fake_complete


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


def wait_run(client, run_id, timeout=15.0):
    """Poll a run to its end and return its events. Used after a confirm, whose follow-up
    turn shares SCRIPT with whatever the next test sets."""
    deadline = time.time() + timeout
    events = []
    while time.time() < deadline:
        state = client.get(f"/api/assistant/runs/{run_id}?after_seq=-1").get_json() or {}
        events = state.get("events", [])
        if state.get("done"):
            break
        time.sleep(0.02)
    return events


def audit_rows():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT action, level FROM audit_log")]
    finally:
        conn.close()


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

    # Seen in German answers: the model wrote its links by hand, and bolded them.
    text, links = assistant.render_links(
        "**[[machine:PC-01]]** und [PC-01](/machine/PC-01) und [PC-02](/machine/PC-02) "
        "und [Warnungen](/alerts)",
        lambda kind, arg, opts: (("/machine/PC-01", opts.get("label") or arg)
                                 if kind == "machine" and arg == "PC-01"
                                 else ("/alerts", opts.get("label") or "Alerts")
                                 if kind == "page" and arg == "alerts" else None))
    check("a bolded token still becomes a hub link", "**[PC-01](/machine/PC-01)**" in text)
    check("a hand-written link to a hub page is read back and checked like a token",
          "[PC-01](/machine/PC-01) und" in text and "[Warnungen](/alerts)" in text)
    check("...and one the resolver refuses is reduced to its label",
          "(/machine/PC-02)" not in text and "PC-02" in text)
    check("token_for_href reads the hub's link shapes",
          assistant.token_for_href("/machine/PC-1?tab=network") == ("machine", "PC-1",
                                                                     {"tab": "network"})
          and assistant.token_for_href("/reports/machines/PC-1") == ("report", "PC-1", {})
          and assistant.token_for_href("/remote?machine=PC-1") == ("remote", "PC-1", {})
          and assistant.token_for_href("//evil.example/machine/x") is None
          and assistant.token_for_href("/api/machines/PC-1") is None)


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
    if body.get("run_id"):
        wait_run(scoped, body["run_id"])
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
    if (r.get_json() or {}).get("run_id"):
        wait_run(scoped, r.get_json()["run_id"])
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
    if (r.get_json() or {}).get("run_id"):
        wait_run(super_client, r.get_json()["run_id"])
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


def test_the_last_machine_by_name_is_found():
    """A real hub asked to "install VLC on VOSTRO-LAPTOP" answered that no such machine
    existed: the full /api/machines rows ran past the result cap, the cut kept the first
    machines by name, and VOSTRO sorts last. A filter on `name` -- the key is `machine` --
    then matched nothing and said nothing about why."""
    print("\n-- the whole fleet fits, and a filter on a missing key says so --")
    conn = sqlite3.connect(DB)
    names = [f"FLEET-{i:02d}" for i in range(40)] + ["VOSTRO-LAPTOP"]
    for name in names:
        conn.execute("INSERT OR IGNORE INTO machine_info (machine, manufacturer, model, "
                     "os_caption, serial_number) VALUES (?, 'Dell Inc.', 'Vostro 5490', "
                     "'Microsoft Windows 11 Pro', 'CJ0JQT2')", (name,))
    conn.commit()
    conn.close()
    super_client = client_for("tester@example.com")
    chat_id = new_chat(super_client)
    SCRIPT[:] = [call("list_machines"),
                 call("list_machines", where={"name": "VOSTRO-LAPTOP"}),
                 call("list_machines", fields=["machine", "diagnostics.has_sensors"],
                      where={"machine": "vostro"}),
                 say("ok")]
    converse(super_client, chat_id, "install VLC on VOSTRO-LAPTOP")
    results = [json.loads(m["content"]) for m in tool_messages(chat_id)][-3:]
    rows = results[0]["data"]
    shown = [r["machine"] for r in rows]
    not_shown = (results[0].get("truncated") or {}).get("lists", {}).get(
        "(the answer itself)", {}).get("not_shown", [])
    check("short rows without the live diagnostics",
          all("diagnostics" not in r and "machine" in r for r in rows))
    check("...fit far more machines than the full rows did", len(shown) >= 30)
    check("the last machine by name is shown, or named as cut",
          "VOSTRO-LAPTOP" in shown or "VOSTRO-LAPTOP" in not_shown)
    check("...and every machine is accounted for",
          set(names) <= set(shown) | set(not_shown))
    narrowed = results[1]["data"]["_narrowed"]
    check("a filter on a key no row has names the key and the real ones",
          narrowed["matched"] == 0 and narrowed["unknown_keys"] == ["name"]
          and "machine" in narrowed["keys"])
    check("asking for fields still reaches the full row",
          results[2]["data"]["items"] == [{"machine": "VOSTRO-LAPTOP",
                                           "diagnostics.has_sensors": False}])

    # Shortening the rows before the filter dropped the key the filter was on.
    rows = [{"machine": "PC-A", "diagnostics": {"has_sensors": True}, "status": "online"},
            {"machine": "PC-B", "diagnostics": {"has_sensors": False}, "status": "online"}]
    out = assistant_tools.shape("list_machines", rows,
                                {"where": {"diagnostics.has_sensors": True}})
    check("a filter on a reading the short row leaves out still matches",
          [r["machine"] for r in out["items"]] == ["PC-A"]
          and "diagnostics" not in out["items"][0] and out["_narrowed"]["matched"] == 1)
    out = assistant_tools.narrow(rows, {"where": {"diagnostics.nope": True}})
    check("an unknown dotted key is reported even when its parent exists",
          out["_narrowed"].get("unknown_keys") == ["diagnostics.nope"])
    out = assistant_tools.narrow(rows + [{"machine": "PC-C", "diagnostics": None}],
                                 {"where": {"diagnostics.has_sensors": "maybe"}})
    check("...but a key that exists is not called unknown",
          "unknown_keys" not in out["_narrowed"])

    # The cap is the operator's to set (ai.assistant_result_chars): a turn must use it.
    for chars in (4000, 100000):
        settings.set_many(DB, {"ai.assistant_result_chars": chars})
        chat_id = new_chat(super_client)
        SCRIPT[:] = [call("list_machines", fields=["machine", "diagnostics", "os"]), say("ok")]
        converse(super_client, chat_id, "list everything")
        content = tool_messages(chat_id)[-1]["content"]
        if chars == 4000:
            check("a small result size cuts the answer to it",
                  len(content) <= 4000 and "truncated" in json.loads(content))
        else:
            check("a large result size lets the whole answer through",
                  len(content) > 12000 and "truncated" not in json.loads(content))
    settings.set_many(DB, {"ai.assistant_result_chars": 12000})

    # A name that grows when escaped as JSON must not push the result over its limit.
    escaped = [{"machine": "\x01" * 60 + str(i), "note": "n" * 200} for i in range(200)]
    content = assistant.fit_result({"ok": True, "data": escaped}, limit=6000)
    check("names that grow when escaped still leave valid JSON within the limit",
          len(content) <= 6000 and json.loads(content)["truncated"]["lists"])


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


def test_a_stuck_deployment_target_can_be_found():
    """The Dashboard counted one deployment target in flight for weeks, and there was no list
    of targets to find it in: the only list was of the newest deployments, and this target sat
    in one whose own status said `complete`. The assistant ran out of steps looking."""
    print("\n-- the target behind 'Deployments in flight' is listed, wherever it is --")
    import packages
    pkg = packages.create_package(
        DB, name="7-Zip", source={"kind": packages.SOURCE_UPLOAD, "sha256": "a" * 64,
                                  "file_name": "7z.msi", "file_size": 1234},
        install_command="msiexec.exe", install_args='/i "{file}" /qn', actor="test")
    dep = packages.create_deployment(DB, package_id=pkg, machines=["PC-01", "PC-02"],
                                     created_by="test")
    weeks_ago = int(time.time()) - 21 * 86400
    with packages.get_conn(DB) as conn:
        conn.execute("UPDATE deployments SET status = 'complete' WHERE id = ?", (dep,))
        conn.execute("UPDATE deployment_targets SET status = 'succeeded' "
                     "WHERE deployment_id = ? AND machine = 'PC-02'", (dep,))
        conn.execute("UPDATE deployment_targets SET updated_at = ? "
                     "WHERE deployment_id = ? AND machine = 'PC-01'", (weeks_ago, dep))
    check("the Dashboard counts it",
          packages.count_deployment_states(DB)["running"] >= 1)

    admin = client_for("tester@example.com")
    r = admin.get("/api/deployments/targets")
    rows = [t for t in (r.get_json() or {}).get("targets", []) if t["deployment_id"] == dep]
    check("the targets route lists it", r.status_code == 200 and len(rows) == 1
          and rows[0]["machine"] == "PC-01" and rows[0]["status"] == "pending")
    check("...with its deployment's own status and package",
          rows and rows[0]["deployment_status"] == "complete"
          and rows[0]["package_name"] == "7-Zip")
    check("an unknown status is refused",
          admin.get("/api/deployments/targets?status=bogus").status_code == 400)
    check("an operator without deploy_packages is refused",
          client_for("viewer@x.com").get("/api/deployments/targets").status_code == 403)

    permissions.create_group(DB, "Deployers PC-02",
                             capabilities=[permissions.VIEW, permissions.DEPLOY_PACKAGES],
                             machines=["PC-02"], members=["deployer@x.com"])
    scoped = client_for("deployer@x.com").get("/api/deployments/targets").get_json()
    check("a scoped deployer does not see a machine outside their scope",
          not any(t["machine"] == "PC-01" for t in scoped["targets"]))

    chat_id = new_chat(admin)
    SCRIPT[:] = [call("deployment_targets"), say("PC-01 is stuck.")]
    converse(admin, chat_id, "It says 1 deployment is in flight. Which one?")
    result = json.loads(tool_messages(chat_id)[0]["content"])
    check("the assistant finds it in one call",
          result["ok"] and any(t["machine"] == "PC-01" and t["deployment_id"] == dep
                               for t in result["data"]["targets"]))
    check("the assistant is offered the tool",
          "deployment_targets" in CALLS[-1][1])

    dashboard = admin.get("/", headers={"Sec-Fetch-Dest": "iframe"}).get_data(as_text=True)
    check("the Dashboard knows this operator may follow the tile",
          'data-can-deploy="yes"' in dashboard)
    viewer_page = client_for("viewer@x.com").get(
        "/", headers={"Sec-Fetch-Dest": "iframe"}).get_data(as_text=True)
    check("...and that a viewer may not", 'data-can-deploy=""' in viewer_page)


def test_where_operators():
    print("\n-- where takes >, in, exists --")
    rows = {"deployments": [{"id": 1, "target_counts": {"pending": 1}},
                            {"id": 2, "target_counts": {"succeeded": 3}},
                            {"id": 3, "status": "done", "target_counts": {"in_flight": 2}}]}
    pick = lambda where: [d["id"] for d in assistant_tools.narrow(rows, {"where": where})
                          ["deployments"]]
    check("> 0 finds a count that is present", pick({"target_counts.pending": {">": 0}}) == [1])
    check("...and not one that is absent", pick({"target_counts.failed": {">": 0}}) == [])
    check("in matches any of a list, case-insensitively",
          pick({"status": {"in": ["DONE", "x"]}}) == [3])
    check("exists false finds the entries without the key",
          pick({"status": {"exists": False}}) == [1, 2])
    check("!= excludes", pick({"status": {"!=": "done"}}) == [])


def test_a_chat_keeps_answering_after_you_leave_it():
    """Switching chats used to stop the panel listening, never resume, and leave the chat
    refusing new messages as 'still answering'. The run goes on in the hub; the panel has to
    be able to find it again."""
    print("\n-- a chat that is answering can be left and picked up again --")
    import threading
    admin = client_for("tester@example.com")
    gate = threading.Event()
    entered = threading.Event()

    def slow(messages):
        entered.set()
        gate.wait(10)
        return say("finished while you were away")

    SCRIPT[:] = [slow]
    chat_a = new_chat(admin)
    r = admin.post(f"/api/assistant/chats/{chat_a}/messages", json={"text": "take your time"})
    run_id = r.get_json()["run_id"]
    # Wait for chat A to TAKE the slow step before chat B is given its own: SCRIPT is shared,
    # and a sleep here once let chat B's worker reach the slow step first.
    check("chat A is answering", entered.wait(5))
    listed = {c["id"]: c for c in admin.get("/api/assistant/chats").get_json()["chats"]}
    check("the history shows it still answering", listed[chat_a]["running"] is True)
    opened = admin.get(f"/api/assistant/chats/{chat_a}").get_json()
    check("opening it again hands back the run to listen to",
          opened["run"] and opened["run"]["id"] == run_id)
    chat_b = new_chat(admin)
    SCRIPT.append(say("b answered"))
    status_b, events_b = converse(admin, chat_b, "meanwhile, in another chat")
    check(f"another chat can be used meanwhile ({status_b}, {events_b[-2:]})",
          status_b == 202 and events_b[-1]["type"] == "done")
    gate.set()
    deadline = time.time() + 10
    while time.time() < deadline and admin.get(f"/api/assistant/chats/{chat_a}").get_json()["run"]:
        time.sleep(0.05)
    opened = admin.get(f"/api/assistant/chats/{chat_a}").get_json()
    check("when it finishes the answer is stored in the chat",
          opened["run"] is None
          and any("finished while you were away" in m["content"] for m in opened["messages"]))
    listed = {c["id"]: c for c in admin.get("/api/assistant/chats").get_json()["chats"]}
    check("...and the history stops showing it as running", listed[chat_a]["running"] is False)
    SCRIPT[:] = [say("again")]
    status, _events = converse(admin, chat_a, "and now?")
    check("the chat takes new messages again", status == 202)


def test_conversations_are_named_by_what_they_were_for():
    """The first message used to BE the title -- a cut-off sentence, unreadable in a list.
    The model names the conversation once, from that message, and never over a name the
    operator typed."""
    print("\n-- a conversation is named for its purpose, and a typed name sticks --")
    admin = client_for("tester@example.com")
    TITLES[:] = ["\"Stuck deployment on PC-01.\""]
    chat_id = new_chat(admin)
    SCRIPT[:] = [say("Looking into it.")]
    _status, events = converse(admin, chat_id,
                               "It says 1 deployment is in flight but I cannot find it")
    deadline = time.time() + 5
    while time.time() < deadline and assistant.get_chat(
            DB, chat_id, "tester@example.com")["title_source"] != "ai":
        time.sleep(0.02)
    chat = assistant.get_chat(DB, chat_id, "tester@example.com")
    check("the model's name replaces the first message, cleaned",
          chat["title"] == "Stuck deployment on PC-01" and chat["title_source"] == "ai")
    check("the panel is told without waiting for the list",
          any(e["type"] == "title" for e in events)
          or chat["title_source"] == "ai")
    asked = len(TITLE_CALLS)
    TITLES[:] = ["Something else entirely"]
    SCRIPT[:] = [say("ok")]
    converse(admin, chat_id, "and the second question")
    time.sleep(0.2)
    check("only the FIRST message names it", len(TITLE_CALLS) == asked
          and assistant.get_chat(DB, chat_id, "tester@example.com")["title"]
          == "Stuck deployment on PC-01")

    r = admin.patch(f"/api/assistant/chats/{chat_id}", json={"title": "  My ticket 4711 "})
    check("the operator can rename it", r.status_code == 200
          and r.get_json()["title"] == "My ticket 4711"
          and r.get_json()["title_source"] == "manual")
    assistant.rename_chat(DB, chat_id, "tester@example.com", "Model's idea",
                          source=assistant.TITLE_AI)
    check("...and the model never overwrites a typed name",
          assistant.get_chat(DB, chat_id, "tester@example.com")["title"] == "My ticket 4711")
    check("an empty name is refused",
          admin.patch(f"/api/assistant/chats/{chat_id}", json={"title": "  "}).status_code == 400)
    check("a JSON body that is not an object is refused, not a 500",
          admin.patch(f"/api/assistant/chats/{chat_id}", json=["x"]).status_code == 400)
    check("a prefixed, quoted title is cleaned",
          assistant.clean_title('Title: "Stuck deployment."') == "Stuck deployment")
    check("another operator cannot rename it",
          client_for("viewer@x.com").patch(f"/api/assistant/chats/{chat_id}",
                                           json={"title": "x"}).status_code == 404)

    TITLES[:] = [None]          # the provider fails
    other = new_chat(admin)
    SCRIPT[:] = [say("ok")]
    converse(admin, other, "Which machines are hot right now?")
    time.sleep(0.3)
    chat = assistant.get_chat(DB, other, "tester@example.com")
    check("a provider that fails leaves the first message as the title",
          chat["title"] == "Which machines are hot right now?" and chat["title_source"] == "auto")


def test_modes_decide_what_runs_unasked():
    """Command modes (hub 1.135.5). The silent failure is an action running without a click
    in a mode that should have asked -- or a wipe running unasked in ANY mode."""
    print("\n-- Ask / Auto / Bypass decide what runs without a click --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    check("a new conversation starts in Ask",
          scoped.get(f"/api/assistant/chats/{chat_id}").get_json()["chat"]["mode"] == "ask")
    check("an unknown mode is refused",
          scoped.patch(f"/api/assistant/chats/{chat_id}", json={"mode": "yolo"}).status_code == 400)
    check("another operator cannot change it",
          client_for("viewer@x.com").patch(f"/api/assistant/chats/{chat_id}",
                                           json={"mode": "bypass"}).status_code == 404)

    r = scoped.patch(f"/api/assistant/chats/{chat_id}", json={"mode": "auto"})
    check("the owner switches to Auto", r.status_code == 200 and r.get_json()["mode"] == "auto")
    check("...and the switch is in the audit log at security level",
          any(e["action"] == "assistant.mode_changed" and e["level"] == "security"
              for e in audit_rows()))
    before = command_rows("PC-01")
    SCRIPT[:] = [call("run_command", machine="PC-01", type="run_script",
                      params={"script": "reg query HKLM\\Software"}, risk="routine",
                      impact="Reads one registry key; changes nothing."),
                 call("run_command", machine="PC-01", type="restart", risk="critical",
                      impact="Restarts PC-01 and closes the signed-in user's programs."),
                 call("run_command", machine="PC-01", type="gpupdate"),
                 say("done")]
    converse(scoped, chat_id, "check the key, then restart it, then gpupdate")
    results = [json.loads(m["content"]) for m in tool_messages(chat_id)]
    check("Auto runs what the model marked routine, at once",
          results[0].get("ran_without_asking") is True and results[0]["ok"]
          and command_rows("PC-01") == before + 1)
    check("...recording its judgement on the action",
          results[0]["action"]["risk"] == "routine"
          and "registry" in results[0]["action"]["impact"]
          and results[0]["action"]["auto"] is True)
    check("...and in the audit log",
          any(e["action"] == "assistant.action_auto" for e in audit_rows()))
    check("Auto still asks for what the model marked critical",
          results[1].get("status") == "pending_confirmation"
          and results[1]["action"]["impact"].startswith("Restarts"))
    check("...and for an action the model did not judge at all",
          results[2].get("status") == "pending_confirmation")
    check("neither of those ran", command_rows("PC-01") == before + 1)
    prompt = CALLS[-1][0][0]["content"]
    check("the model is told which mode it is in", "AUTO mode" in prompt)

    scoped.patch(f"/api/assistant/chats/{chat_id}", json={"mode": "bypass"})
    SCRIPT[:] = [call("run_command", machine="PC-01", type="restart"),
                 call("call_endpoint", method="POST", path="/api/wipe/machines/PC-01/wipe",
                      body={"confirm": "PC-01"}, risk="routine"),
                 say("done")]
    converse(scoped, chat_id, "restart and wipe")
    results = [json.loads(m["content"]) for m in tool_messages(chat_id)][-2:]
    check("Bypass runs an action without a judgement or a click",
          results[0].get("ran_without_asking") is True)
    check("...but a wipe keeps its card and typed name in every mode",
          results[1].get("status") == "pending_confirmation"
          and results[1]["action"]["typed_name"] is True)

    fresh = new_chat(scoped)
    check("a NEW conversation is back in Ask",
          scoped.get(f"/api/assistant/chats/{fresh}").get_json()["chat"]["mode"] == "ask")
    created = scoped.post("/api/assistant/chats", json={"mode": "auto"}).get_json()
    check("a conversation can be created in a chosen mode", created["mode"] == "auto")


def test_a_confirmed_action_is_followed_up():
    """Confirming used to queue the command and stop: the operator had to ask "did it work?"
    every time. The hub now starts a turn that reads the outcome."""
    print("\n-- confirming an action starts a turn that reads its outcome --")
    scoped = client_for("scoped@x.com")
    chat_id = new_chat(scoped)
    SCRIPT[:] = [call("run_command", machine="PC-01", type="restart", risk="critical",
                      impact="Restarts PC-01."), say("Queued.")]
    converse(scoped, chat_id, "restart PC-01")
    action = assistant.list_actions(DB, chat_id)[-1]

    def read_output(messages):
        note = [m for m in messages if m["role"] == "user"
                and m["content"].startswith(assistant.NOTE_PREFIX)]
        command_id = (action_now()["result"] or {}).get("data", {}).get("command_id", "")
        return call("command_output", command_id=command_id, wait_seconds=1) if note \
            else say("no note")

    def action_now():
        return assistant.get_action(DB, action["id"], "scoped@x.com")

    SCRIPT[:] = [read_output, say("It has not run yet; PC-01 has not picked it up.")]
    r = scoped.post(f"/api/assistant/actions/{action['id']}/confirm", json={})
    data = r.get_json()
    check("the confirm answers with a follow-up run", r.status_code == 200 and data["run_id"])
    events = wait_run(scoped, data["run_id"])
    output = [json.loads(m["content"]) for m in tool_messages(chat_id)][-1]
    check("the follow-up read the command's outcome",
          output["ok"] and output["data"]["status"] == "pending"
          and output["data"]["finished"] is False)
    check("...and reported back",
          any("has not run yet" in (e.get("content") or "") for e in events))

    shaped = assistant_tools.shape_command_output(
        {"status": "done", "chunks": [], "truncated": False,
         "result": {"success": 1, "output": "a\nNoDriveTypeAutorun 0xff\nb\nc", "completed_at": 1}},
        {"find": "autorun"})
    check("find keeps the lines that matter",
          shaped["output"] == "NoDriveTypeAutorun 0xff" and shaped["lines_total"] == 4)
    long_output = {"ok": True, "data": {"status": "done",
                                        "output": "x" * 2000 + "MIDDLE" + "y" * 9000 + "END"}}
    parsed = json.loads(assistant.fit_result(long_output, limit=8000))
    check("long output keeps its start and its end",
          parsed["data"]["output"].startswith("x" * 100)
          and parsed["data"]["output"].endswith("END"))


def test_package_building_follows_the_modes():
    """Package building (hub 1.136.0). The silent failures: a download that starts in Ask mode
    without a click, a package created unasked in Auto, and the web tools offered to an
    operator who cannot define packages. DNS and HTTP are faked at webfetch's seams."""
    print("\n-- package building: downloads follow the mode, packages wait in Auto --")
    import webfetch

    class Response:
        status, headers = 200, {"Content-Type": "application/octet-stream"}

        def stream(self, size):
            yield b"MZ" * 64

        def release_conn(self):
            pass

    webfetch._resolve = lambda host, port: ["93.184.216.34"]
    webfetch._request = lambda parsed, address: Response()
    settings.set_many(DB, {"ai.assistant_web_enabled": True}, "test")
    settings.invalidate()
    staging = webfetch.staging_root(app.LOG_DIR)
    admin = client_for("tester@example.com")
    chat_id = new_chat(admin)
    download = call("download_installer", url="https://vendor.example/app.exe", risk="routine",
                    impact="Downloads an installer to the hub; nothing runs anywhere.")

    SCRIPT[:] = [download, say("waiting for you")]
    converse(admin, chat_id, "make a package for app")
    result = json.loads(tool_messages(chat_id)[-1]["content"])
    check("Ask: a download is a card", result.get("status") == "pending_confirmation")
    check("...and nothing was downloaded", webfetch.list_staged(staging) == [])
    check("the package tools are offered to an operator who can deploy",
          {"web_read", "winget_manifest", "download_installer", "promote_download",
           "create_package"} <= set(CALLS[-1][1]))

    admin.patch(f"/api/assistant/chats/{chat_id}", json={"mode": "auto"})
    SCRIPT[:] = [download, say("downloading")]
    converse(admin, chat_id, "download it")
    result = json.loads(tool_messages(chat_id)[-1]["content"])
    staging_id = (result.get("data") or {}).get("id")
    check("Auto: a routine download runs unasked",
          result.get("ran_without_asking") is True and bool(staging_id))
    deadline = time.time() + 10
    while time.time() < deadline and (webfetch.get_staged(staging, staging_id) or {}).get(
            "status") == "downloading":
        time.sleep(0.05)
    check("...and finishes in the staging folder",
          (webfetch.get_staged(staging, staging_id) or {}).get("status") == "done")

    SCRIPT[:] = [call("promote_download", staging_id=staging_id),
                 # By hand: call()'s own first parameter is `name`, which create_package takes.
                 {"content": "", "tool_calls": [{"id": "pkg", "name": "create_package",
                                                 "arguments": json.dumps({
                     "name": "App", "sources": [{"kind": "upload"}],
                     "install_command": "{file}", "install_args": "/S", "risk": "critical",
                     "impact": "Defines a package; installs nothing."})}]},
                 say("built")]
    converse(admin, chat_id, "build the package")
    promoted, created = [json.loads(m["content"]) for m in tool_messages(chat_id)[-2:]]
    check("promoting into the store runs at once",
          promoted.get("ok") and promoted["data"]["source"]["kind"] == "upload")
    check("creating a package marked critical still waits in Auto",
          created.get("status") == "pending_confirmation")
    check("POST /api/deployments is still confirm-tier",
          assistant_tools.classify("POST", "/api/deployments") == "confirm")
    # PR #106 review: a page read is an outbound request to a URL the model chose, from a
    # conversation full of fleet data -- read-tier would let an injected page exfiltrate it.
    check("reading a web page follows the mode (confirm), not read-tier",
          assistant_tools.classify("POST", "/api/webfetch/read") == "confirm")
    check("...while a winget lookup, which only reaches GitHub, stays read-tier",
          assistant_tools.classify("POST", "/api/webfetch/winget") == "read")
    SCRIPT[:] = [call("web_read", url="https://vendor.example/?d=PC-01", risk="critical",
                      impact="Reads a page."), say("waiting")]
    admin.patch(f"/api/assistant/chats/{chat_id}", json={"mode": "auto"})
    converse(admin, chat_id, "read that page")
    read = json.loads(tool_messages(chat_id)[-1]["content"])
    check("Auto: a page read the model marked critical waits for a click",
          read.get("status") == "pending_confirmation")

    viewer = client_for("viewer@x.com")
    SCRIPT[:] = [say("hello")]
    converse(viewer, new_chat(viewer), "hi")
    check("...and not offered without deploy_packages",
          not {"web_read", "download_installer", "create_package"} & set(CALLS[-1][1]))
    settings.set_many(DB, {"ai.assistant_web_enabled": False}, "test")
    settings.invalidate()


def test_absolute_links_to_this_hub():
    print("\n-- an absolute link to this hub is read back; another host is not --")
    resolve = lambda kind, arg, opts: (("/machine/PC-01", opts.get("label") or arg)
                                       if arg == "PC-01" else None)
    text, _ = assistant.render_links(
        "[PC-01](https://temp.example.net/machine/PC-01) and "
        "[x](https://evil.example/machine/PC-01)", resolve, {"temp.example.net"})
    check("this hub's absolute link resolves", "[PC-01](/machine/PC-01)" in text)
    check("another host's does not", "evil.example" not in text and " x" in text)


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
    test_a_stuck_deployment_target_can_be_found()
    test_where_operators()
    test_a_chat_keeps_answering_after_you_leave_it()
    test_conversations_are_named_by_what_they_were_for()
    test_modes_decide_what_runs_unasked()
    test_a_confirmed_action_is_followed_up()
    test_package_building_follows_the_modes()
    test_absolute_links_to_this_hub()
    test_the_last_machine_by_name_is_found()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
