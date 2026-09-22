"""Model-half test for ai.py (roadmap #24) -- the drafter's validation gate.

**The silent failure this file exists to catch is a rule that looks valid because a language
model said so.** The plan this feature came from used `cpu.temp_c`, `proc.cpu_percent`,
`sys.last_seen_age`, `tag:server`, a weekly schedule and a `disable_usb` action. Not one of
those exists on this hub. Every one of them would have produced a rule that stored cleanly,
read correctly in the console, and evaluated to UNKNOWN on every machine for the rest of its
life -- which is the failure rules.py's own docstring calls the fastest way to make an
alerting feature untrustworthy.

So the assertions below are mostly refusals, and each one names a thing a model will actually
try. A fake provider is injected in place of ai.complete, because a test that needed a model
running would be a test nobody runs.

**Three more silent failures arrived with the answering half**, and the last sections are
theirs. A redaction that does not redact looks exactly like one that does -- the panel answers
either way, and the hostname is in a request body nobody reads -- so those assertions search
the prompt the fake provider was handed for each identifier by value. A count that was not
scoped looks like an answer rather than like a statement about a fleet somebody cannot see, so
the scoped figures are asserted against a predicate rather than trusted. And a summary built
on a capped read of `rule_fires` would be wrong only on a busy day, which is the day it is
read, so one fire is deliberately seeded outside the window.
"""
import json
import os
import sqlite3
import sys
import tempfile
from ipaddress import ip_address

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "hub"))
import ai
import alerts
import rules
import scripts

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


CONFIG = {
    "enabled": True,
    "provider": ai.PROVIDER_OPENAI_CHAT,
    "base_url": "http://127.0.0.1:11434",
    "model": "test-model",
    "max_tokens": 512,
    "timeout_seconds": 30,
    "allow_private_endpoint": True,
    "send_machine_names": False,
    "draft_retention_days": 7,
}


def fake_provider(*answers):
    """Replace ai.complete with a queue of canned replies, and count the calls.

    Returns the recorder so a test can assert on how MANY times the model was asked -- which
    is the only way to check the repair loop is bounded.
    """
    queue = list(answers)
    calls = []

    def complete(config, messages, **kwargs):
        calls.append(messages)
        if not queue:
            return "the AI provider returned an empty response", None
        answer = queue.pop(0)
        return (None, answer) if isinstance(answer, str) else answer

    ai.complete = complete
    complete.calls = calls
    return complete


def envelope(**fields):
    """The OLD single-rule envelope, deliberately kept.

    It is what every answer in this file used before staging landed, and it is what a small
    local model still answers with -- ai._parse_envelope wraps it rather than spending the one
    repair round teaching it to use a list. These calls are that contract's test.
    """
    body = {"name": "Test rule", "condition_text": "",
            "target": {"include": [{"kind": "all"}]},
            "actions": [{"type": "alert", "params": {"text": "hello"}}],
            "for_seconds": 0, "cooldown_seconds": 0, "refusal": ""}
    body.update(fields)
    return json.dumps(body)


def staged(*rules_in, refusal=""):
    """The envelope a staged answer arrives in: one rule object per stage."""
    return json.dumps({"rules": [json.loads(envelope(**fields)) for fields in rules_in],
                       "refusal": refusal})


def only(draft):
    """The single stage of a one-rule draft. Most assertions here are about one rule."""
    return ai.draft_rules(draft)[0]


def main():
    real_complete = ai.complete
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        rules.init_rules_db(db_path)
        scripts.init_scripts_db(db_path)
        ai.init_ai_db(db_path)
        # The two machines a target resolves to. `machine_info` belongs to app.init_db, which
        # a model-half test cannot call, so it is seeded by hand with the three columns
        # rules._all_machines reads -- the same approach test_rules.py takes.
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, ad_ou TEXT, "
                     "ad_dn TEXT)")
        conn.executemany("INSERT INTO machine_info VALUES (?,?,?)",
                         [("PC-01", "OU=Sales,DC=corp", ""),
                          ("PC-02", "OU=Sales,DC=corp", "")])
        conn.commit()
        conn.close()
        extra = rules.all_extra_variables(db_path)

        print("== The prompt describes THIS hub, not a generic RMM ==")
        prompt = ai.system_prompt(db_path)
        catalog = {var.name for var in rules.catalog(db_path)}
        listed = {line.split(" (")[0] for line in ai.catalog_prompt(db_path).splitlines()}
        check("every variable in the prompt exists in the engine's catalog",
              listed and listed <= catalog)
        check("the aggregate a real 'any disk low' rule needs is offered",
              "disk.min_free_gb" in listed)
        # The regression that produced ACTION_EXAMPLES: the prompt used to teach an alert
        # shape the engine rejects, so every well-formed request cost a repair round and then
        # failed. An example the validator refuses is a broken drafter, not a cosmetic slip.
        error, _clean = rules.validate_actions(
            [dict(a) for a in ai.ACTION_EXAMPLES], extra, allow_command=True,
            script_specs=scripts.specs(db_path))
        check(f"every action shape the prompt teaches is one the engine accepts ({error})",
              error is None)
        check("the prompt says there are no tags", "NO TAGS" in prompt)
        check("the prompt says a rule has no schedule", "no schedule" in prompt)
        check("the prompt says an escalation is one rule per stage",
              "One rule is one stage" in prompt)
        check("...and teaches the dialog that asks before acting",
              "on_response" in prompt and "show_message" in prompt)
        check("the prompt names the two actions that do not exist yet",
              "disabling USB" in prompt and "locking an account" in prompt)
        for forbidden in ("kill_process", "locate_device", "wipe_device", "shutdown_at"):
            check(f"a command a rule may not issue is not offered: {forbidden}",
                  forbidden not in prompt.split("Commands a rule may issue:")[1]
                  .split("\n")[0])

        print("\n== A variable the model invented is refused ==")
        # The exact expression the source plan for this feature wrote down.
        fake_provider(envelope(condition_text="cpu.temp_c > 90"))
        error, draft = ai.draft_rule(db_path, CONFIG, "alert when the CPU is over 90",
                                     extra=extra)
        check("a draft naming cpu.temp_c is refused", draft is None)
        check("...and the refusal is the parser's own words, not a generic message",
              error and "cpu.temp_c" in error)

        fake_provider(envelope(condition_text="proc.cpu_percent > 80"))
        error, draft = ai.draft_rule(db_path, CONFIG, "kill hungry processes", extra=extra)
        check("a draft naming a per-process variable is refused", draft is None)
        check("...and says which name it did not recognise",
              error and "proc.cpu_percent" in error)

        print("\n== An operator the wrong kind of value is refused ==")
        fake_provider(envelope(condition_text="sys.online > 5"))
        error, draft = ai.draft_rule(db_path, CONFIG, "machines that are very online",
                                     extra=extra)
        check("a numeric comparison against a boolean is refused", draft is None)
        check("...naming the operator and the variable",
              error and "sys.online" in error)

        print("\n== Actions that do not exist ==")
        fake_provider(envelope(condition_text="disk.min_free_gb < 10",
                               actions=[{"type": "disable_usb", "params": {}}]))
        error, draft = ai.draft_rule(db_path, CONFIG, "block USB on the HR machines",
                                     extra=extra)
        check("a draft using disable_usb is refused", draft is None)
        check("...and the message lists what an action may be",
              error and "action type must be one of" in error)

        fake_provider(envelope(condition_text="sys.uptime_days > 7",
                               actions=[{"type": "command",
                                         "params": {"command_type": "kill_process",
                                                    "params": {"pid": 42}}}]))
        error, draft = ai.draft_rule(db_path, CONFIG, "kill it", extra=extra)
        check("a draft issuing a forbidden command is refused", draft is None)

        fake_provider(envelope(condition_text="sys.uptime_days > 7",
                               actions=[{"type": "command",
                                         "params": {"command_type": "restart",
                                                    "params": {}}}]))
        error, draft = ai.draft_rule(db_path, CONFIG, "reboot stale machines", extra=extra,
                                     allow_command=False)
        check("an author without issue_commands cannot draft a command action",
              draft is None and error)

        print("\n== A refusal from the model is passed through, not turned into a rule ==")
        fake_provider(envelope(refusal="A rule has no schedule on this hub."))
        error, draft = ai.draft_rule(db_path, CONFIG,
                                     "reboot the workstations every Sunday at 3am",
                                     extra=extra)
        check("the model's own refusal comes back as the error", draft is None)
        check("...verbatim", error == "A rule has no schedule on this hub.")

        print("\n== A good draft round-trips through the engine ==")
        fake_provider(envelope(name="Low disk",
                               condition_text="disk.min_free_gb < 10",
                               for_seconds=300, cooldown_seconds=3600))
        error, draft = ai.draft_rule(db_path, CONFIG,
                                     "alert me when any drive drops below 10 GB free",
                                     extra=extra)
        check("a valid draft is produced", error is None and draft is not None)
        rule = only(draft)
        check("...with the canonical expression, not the model's spelling",
              rule["condition_text"] == rules.format_expression(rule["condition"]))
        check("...whose text parses again unchanged",
              rules.parse_expression(rule["condition_text"], extra)[0] is None)
        check("...and the variable it depends on is reported",
              rule["variables"] == ["disk.min_free_gb"])
        check("...keeping the English that produced it",
              draft["source_text"].startswith("alert me when"))

        print("\n== An escalation is drafted as STAGES, not collapsed into one rule ==")
        # The failure this section exists for: "warn after 5 days, force a restart after 10"
        # came back as `sys.uptime_days > 10` + restart, with the warning silently gone,
        # because the envelope had room for one rule and the prompt asked for one rule. The
        # warning half vanishing is invisible -- the rule that remains looks correct.
        fake_provider(staged(
            dict(name="Ask for a restart", condition_text="sys.uptime_days > 5",
                 actions=[{"type": "show_message",
                           "params": {"title": "Restart needed", "body": "Restart now?",
                                      "buttons": [{"id": "yes"}, {"id": "later"}]},
                           "on_response": {
                               "yes": [{"type": "command",
                                        "params": {"command_type": "restart", "params": {}}}],
                               "later": [{"type": "snooze", "params": {"seconds": 14400}}]}}]),
            dict(name="Force a restart", condition_text="sys.uptime_days > 10",
                 actions=[{"type": "command",
                           "params": {"command_type": "restart", "params": {}}}])))
        error, escalation = ai.draft_rule(
            db_path, CONFIG,
            "ask for a restart after 5 days up, force one after 10", extra=extra)
        stages = ai.draft_rules(escalation)
        check("both stages survive", error is None and len(stages) == 2)
        check("...the gentle one keeping its own threshold",
              stages[0]["condition_text"] == "sys.uptime_days > 5")
        check("...and the enforcing one keeping its",
              stages[1]["condition_text"] == "sys.uptime_days > 10")
        check("...with the message's follow-up actions intact",
              stages[0]["actions"][0]["on_response"]["yes"][0]["params"]["command_type"]
              == "restart")
        summaries = ai.summarise_rules(db_path, escalation)
        check("each stage is summarised, numbered in escalation order",
              summaries[0].startswith("Stage 1 of 2")
              and summaries[1].startswith("Stage 2 of 2"))
        check("...and a message's follow-up is named, not hidden behind 'show_message'",
              "yes -> command" in summaries[0])

        print("\n== One bad stage rejects the whole answer ==")
        # Half an escalation is worse than none: committed alone, the enforcing stage reboots
        # people with no warning, and the warning stage alone never acts.
        fake_provider(staged(dict(condition_text="sys.uptime_days > 5"),
                             dict(condition_text="cpu.temp_c > 90")),
                      staged(dict(condition_text="sys.uptime_days > 5"),
                             dict(condition_text="cpu.temp_c > 90")))
        error, draft = ai.draft_rule(db_path, CONFIG, "two stages, one invented variable",
                                     extra=extra)
        check("a set with one unusable stage is refused whole", draft is None and error)
        check("...naming which stage, so the repair round has somewhere to aim",
              error.startswith("rule 2:"))

        print("\n== The number of stages is bounded ==")
        over_cap = json.dumps({"rules": [json.loads(envelope(condition_text="sys.online"))]
                               * (ai.MAX_RULES_PER_DRAFT + 1)})
        provider = fake_provider(over_cap, over_cap)
        error, draft = ai.draft_rule(db_path, CONFIG, "a policy document", extra=extra)
        check("more stages than this hub drafts is refused", draft is None and error)
        check("...saying how many it will do", str(ai.MAX_RULES_PER_DRAFT) in error)
        # The one repair round has to aim at the actual failure. Told the usual "keep every
        # stage you already had", a model that over-generated re-sends the same set and the
        # retry buys nothing -- for the one case where a retry could have recovered.
        retry = provider.calls[1][-1]["content"]
        check("...and the retry asks for FEWER rules, not for every stage back",
              "FEWER rules" in retry and "keeping every stage" not in retry)

        provider = fake_provider(envelope(condition_text="cpu.temp_c > 90"),
                                 envelope(condition_text="cpu.temp_c > 90"))
        ai.draft_rule(db_path, CONFIG, "hot CPUs again", extra=extra)
        check("an ordinary rejection still asks for every stage back",
              "keeping every stage" in provider.calls[1][-1]["content"])

        print("\n== The old single-rule envelope is still read ==")
        # Every small local model has seen a thousand examples of the bare object, and the one
        # repair attempt is for a real validator complaint rather than for a shape this hub
        # can read perfectly well.
        provider = fake_provider(envelope(condition_text="disk.min_free_gb < 3"))
        error, draft = ai.draft_rule(db_path, CONFIG, "under 3 GB", extra=extra)
        check("a bare rule object is wrapped rather than repaired",
              error is None and len(ai.draft_rules(draft)) == 1)
        check("...without spending a repair round", len(provider.calls) == 1)

        print("\n== A refinement carries every stage back to the model ==")
        fake_provider(staged(dict(condition_text="sys.uptime_days > 7"),
                             dict(condition_text="sys.uptime_days > 10")))
        provider = ai.complete
        error, refined = ai.refine_draft(db_path, CONFIG, escalation, "make the warning 7 days",
                                         extra=extra)
        sent = provider.calls[0][-1]["content"]
        check("the model is shown both stages, not the one on screen",
              "sys.uptime_days > 5" in sent and "sys.uptime_days > 10" in sent)
        check("...and the refinement keeps both", error is None
              and len(ai.draft_rules(refined)) == 2)
        check("...accumulating the English that produced it",
              refined["source_text"].endswith("| make the warning 7 days"))

        print("\n== A fenced or chatty answer is still read ==")
        fake_provider("Sure! Here you go:\n```json\n"
                      + envelope(condition_text="disk.min_free_gb < 5") + "\n```")
        error, draft = ai.draft_rule(db_path, CONFIG, "under 5 GB", extra=extra)
        check("a fenced code block is unwrapped rather than failed", error is None and draft)

        print("\n== The repair loop is bounded ==")
        provider = fake_provider(envelope(condition_text="cpu.temp_c > 90"),
                                 envelope(condition_text="cpu.temp_c > 90"),
                                 envelope(condition_text="metric.cpu_temp > 90"))
        error, draft = ai.draft_rule(db_path, CONFIG, "hot CPUs", extra=extra)
        check("two bad answers in a row are given up on", draft is None and error)
        check(f"...after exactly {ai.MAX_REPAIR_ATTEMPTS + 1} calls, never more",
              len(provider.calls) == ai.MAX_REPAIR_ATTEMPTS + 1)
        check("...and the retry told the model what the engine said",
              any("rules engine rejected" in m["content"]
                  for m in provider.calls[-1] if m["role"] == "user"))

        provider = fake_provider(envelope(condition_text="cpu.temp_c > 90"),
                                 envelope(condition_text="metric.cpu_temp > 90"))
        error, draft = ai.draft_rule(db_path, CONFIG, "hot CPUs", extra=extra)
        check("a corrected second answer is accepted", error is None and draft is not None)
        check("...and cost one extra call, not a loop", len(provider.calls) == 2)

        print("\n== The provider is not called when the feature is off ==")
        ai.complete = real_complete
        off = dict(CONFIG, enabled=False)
        error, text = ai.complete(off, [{"role": "user", "content": "hello"}])
        check("complete() refuses while ai.enabled is false", text is None and error)
        check("...saying so in words an operator can act on", "switched off" in error)
        error, _ = ai.complete(dict(CONFIG, base_url="", provider=ai.PRESET_CUSTOM), [])
        check("a custom provider with no address is refused before any request",
              "no AI provider is configured" in str(error))
        error, _ = ai.complete(dict(CONFIG, model=""), [])
        check("a missing model is refused before any request", "model" in str(error))

        print("\n== A provider answering with too much is abandoned, not buffered ==")
        # The claim in complete()'s docstring used to be false: response.json() buffers and
        # parses the whole body, so truncating its result bounded what was KEPT and not what
        # was read. The recorder below counts bytes actually handed over, which is the only
        # thing that distinguishes a cap from a promise.
        real_post = ai.requests.post

        class FakeResponse:
            def __init__(self, chunks, status=200):
                self.chunks, self.status_code, self.served = chunks, status, 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_content(self, chunk_size=8192):
                for chunk in self.chunks:
                    self.served += len(chunk)
                    yield chunk

        try:
            oversize = [b"x" * 8192] * ((ai.MAX_RESPONSE_BYTES // 8192) + 40)
            fake = FakeResponse(oversize)
            ai.requests.post = lambda *a, **k: fake
            error, text = ai.complete(CONFIG, [{"role": "user", "content": "hi"}])
            check("an oversized body is refused", text is None and error)
            check("...in words an operator can read", "too large" in str(error))
            check("...having stopped reading near the cap, not at the end",
                  fake.served <= ai.MAX_RESPONSE_BYTES + 8192)
            check("...which is well short of what was on offer",
                  fake.served < sum(len(c) for c in oversize))

            body = json.dumps({"choices": [{"message": {"content": envelope(
                condition_text="disk.min_free_gb < 10")}}]}).encode()
            ai.requests.post = lambda *a, **k: FakeResponse([body[:10], body[10:]])
            error, text = ai.complete(CONFIG, [{"role": "user", "content": "hi"}])
            check("a normal answer split across chunks is reassembled", error is None)
            check("...and carries the model's text", "disk.min_free_gb" in str(text))

            ai.requests.post = lambda *a, **k: FakeResponse([b"<html>proxy</html>"])
            error, text = ai.complete(CONFIG, [{"role": "user", "content": "hi"}])
            check("a proxy's HTML page is refused rather than parsed",
                  text is None and "could not read" in str(error))

            ai.requests.post = lambda *a, **k: FakeResponse([b""], status=503)
            error, text = ai.complete(CONFIG, [{"role": "user", "content": "hi"}])
            check("an HTTP error is reported by its status", "503" in str(error))
        finally:
            ai.requests.post = real_post

        print("\n== Where a prompt may be sent ==")
        check("a model on this machine is allowed",
              ai.check_provider_url("http://127.0.0.1:11434", True) is None)
        check("...and refused when private endpoints are switched off",
              ai.check_provider_url("http://127.0.0.1:11434", False) is not None)
        check("a plaintext endpoint on the internet is refused",
              "https" in str(ai.check_provider_url("http://example.com", True)))
        check("the cloud metadata address is refused outright",
              ai.check_provider_url("http://169.254.169.254", True) is not None)
        check("a scheme that is not http(s) is refused",
              ai.check_provider_url("file:///etc/passwd", True) is not None)

        # The metadata address wearing a costume. CPython files the whole of ::ffff:0:0/96
        # under "private" but reports none of it as link-local or reserved, so an unwrapped
        # ::ffff:169.254.169.254 skips the unconditional refusal above and lands in the
        # merely-private bucket that allow_private waves through. Checked at both levels:
        # the unwrapper on its own, and the whole check against a resolver that returns one.
        for spelling, plain in (("::ffff:169.254.169.254", "169.254.169.254"),
                                ("::ffff:127.0.0.1", "127.0.0.1"),
                                ("2002:a9fe:a9fe::", "169.254.169.254")):
            check(f"{spelling} is unwrapped to {plain}",
                  str(ai._unwrap_v4(ip_address(spelling))) == plain)

        real_getaddrinfo = ai.socket.getaddrinfo

        def resolves_to(address):
            return lambda *a, **k: [(0, 0, 0, "", (address, 80, 0, 0))]

        try:
            ai.socket.getaddrinfo = resolves_to("::ffff:169.254.169.254")
            check("a host resolving to the MAPPED metadata address is refused outright",
                  ai.check_provider_url("http://model.internal", True) is not None)
            ai.socket.getaddrinfo = resolves_to("2002:a9fe:a9fe::")
            check("...and so is the 6to4 spelling of it",
                  ai.check_provider_url("http://model.internal", True) is not None)
            ai.socket.getaddrinfo = resolves_to("::ffff:127.0.0.1")
            check("a mapped loopback is still allowed, private endpoints being on",
                  ai.check_provider_url("http://model.internal", True) is None)
            check("...and refused when they are off",
                  ai.check_provider_url("http://model.internal", False) is not None)

            # The resolver's own words are for the hub log. This string is rendered in a
            # browser, and CodeQL flags the path for the same reason rules_web.py splits its
            # error text: what the OS wrote is not ours to forward.
            def unresolvable(*a, **k):
                raise ai.socket.gaierror(-2, "Name or service not known")

            ai.socket.getaddrinfo = unresolvable
            message = ai.check_provider_url("http://model.internal", True)
            check("an unresolvable host is refused", message)
            check("...naming the host, which the admin typed",
                  "model.internal" in str(message))
            check("...but not the resolver's own text",
                  "Name or service not known" not in str(message))
        finally:
            ai.socket.getaddrinfo = real_getaddrinfo

        print("\n== Picking a provider by name ==")
        check("custom is the default, so no vendor is implied by the list",
              ai.PROVIDER_PRESETS[0].name == ai.PRESET_CUSTOM)
        check("every preset speaks a wire shape this hub implements",
              all(p.wire in ai.WIRE_SHAPES for p in ai.PROVIDER_PRESETS))
        check("a named provider carries its own address",
              ai.resolved_base_url({"provider": "openrouter"})
              == "https://openrouter.ai/api")
        check("...which wins over a url left behind by an earlier choice",
              ai.resolved_base_url({"provider": "openrouter",
                                    "base_url": "http://127.0.0.1:11434"})
              == "https://openrouter.ai/api")
        check("custom reads the typed url, trailing slash trimmed",
              ai.resolved_base_url({"provider": "custom", "base_url": "http://x:1/"})
              == "http://x:1")
        # The setting briefly held a wire shape rather than a preset name. An unrecognised
        # value must leave the hub configurable rather than unreachable.
        check("an unrecognised provider falls back to custom, not to nothing",
              ai.preset_for("openai_chat").name == ai.PRESET_CUSTOM)
        error, resolved = ai.provider_config(
            dict(CONFIG, provider="openrouter", base_url="", model="m"), "k")
        check("a preset alone is enough to be configured", error is None)
        check("...and the call goes to the preset's address",
              resolved["base_url"] == "https://openrouter.ai/api")

        print("\n== The model list is cached, not fetched to draw a page ==")
        listing = ai.list_models(db_path, "openrouter")
        check("an unread provider reports no models", listing["models"] == [])
        check("...and says so by being stale rather than by looking fresh",
              listing["stale"] is True and listing["cached_at"] is None)

        models_body = json.dumps({"data": [{"id": "z-model"}, {"id": "a-model"},
                                           {"id": ""}, "not-an-object"]}).encode()
        real_get = ai.requests.get
        try:
            ai.requests.get = lambda *a, **k: FakeResponse([models_body])
            error, listing = ai.refresh_models(
                db_path, dict(CONFIG, provider="openrouter", model=""), api_key="k")
            check("a refresh works with NO model chosen yet", error is None)
            check("...which is the whole point of the picker", listing is not None)
            check("...sorted, with the junk dropped",
                  listing["models"] == ["a-model", "z-model"])
            check("...and fresh", listing["stale"] is False)
            check("the cache is readable without touching the network",
                  ai.model_choices(db_path, "openrouter") == ["a-model", "z-model"])
            check("...and is keyed by provider, not shared between them",
                  ai.model_choices(db_path, "ollama") == [])

            # A withdrawn model must leave the picker, or it reappears months later as a 404
            # from a draft nobody can explain.
            ai.requests.get = lambda *a, **k: FakeResponse(
                [json.dumps({"data": [{"id": "a-model"}]}).encode()])
            ai.refresh_models(db_path, dict(CONFIG, provider="openrouter"), api_key="k")
            check("a model the provider stopped serving is dropped",
                  ai.model_choices(db_path, "openrouter") == ["a-model"])

            ai.requests.get = lambda *a, **k: FakeResponse([b""], status=401)
            error, listing = ai.refresh_models(
                db_path, dict(CONFIG, provider="openrouter"), api_key="")
            check("a rejected credential is named as one, not as HTTP 401",
                  error and "AI_API_KEY" in error)
            check("...and the previous list survives the failure",
                  ai.model_choices(db_path, "openrouter") == ["a-model"])

            # The regression that prompted MAX_MODELS_BYTES: OpenRouter's catalogue was
            # 718 KB, which the chat-reply cap refused, so every refresh failed with "too
            # large to read". A list bigger than a reply but ordinary for a catalogue must be
            # read; a genuinely unbounded one must still be refused.
            def catalogue_of(size):
                entry = {"id": "", "description": "x" * 1500}
                entries, total = [], 0
                while total < size:
                    entries.append(dict(entry, id="vendor/model-%d" % len(entries)))
                    total += 1600
                return json.dumps({"data": entries}).encode()

            big = catalogue_of(ai.MAX_RESPONSE_BYTES * 3)
            check("the test catalogue really is past the chat-reply cap",
                  len(big) > ai.MAX_RESPONSE_BYTES)
            ai.requests.get = lambda *a, **k: FakeResponse(
                [big[i:i + 8192] for i in range(0, len(big), 8192)])
            error, listing = ai.refresh_models(
                db_path, dict(CONFIG, provider="openrouter"), api_key="k")
            check("a catalogue the size of OpenRouter's is read, not refused", error is None)
            check("...and every model in it is cached",
                  listing is not None and len(listing["models"]) > 100)

            huge = b"x" * (ai.MAX_MODELS_BYTES + 8192 * 4)
            ai.requests.get = lambda *a, **k: FakeResponse(
                [huge[i:i + 8192] for i in range(0, len(huge), 8192)])
            error, _ = ai.refresh_models(db_path, dict(CONFIG, provider="openrouter"))
            check("a list past the catalogue cap is still refused",
                  error and "too large" in error)
            check("...and leaves the good list cached",
                  len(ai.model_choices(db_path, "openrouter")) > 100)

            # Back to the one-model list the assertions below expect.
            ai.requests.get = lambda *a, **k: FakeResponse(
                [json.dumps({"data": [{"id": "a-model"}]}).encode()])
            ai.refresh_models(db_path, dict(CONFIG, provider="openrouter"), api_key="k")

            ai.requests.get = lambda *a, **k: FakeResponse([b"<html>proxy</html>"])
            error, _ = ai.refresh_models(db_path, dict(CONFIG, provider="openrouter"))
            check("a proxy page is refused rather than parsed",
                  error and "not in a shape" in error)

            ai.requests.get = lambda *a, **k: FakeResponse(
                [json.dumps({"data": []}).encode()])
            error, _ = ai.refresh_models(db_path, dict(CONFIG, provider="openrouter"))
            check("an empty catalogue is an error, not an empty picker",
                  error and "no models" in error)
            check("...and still leaves what was cached",
                  ai.model_choices(db_path, "openrouter") == ["a-model"])
        finally:
            ai.requests.get = real_get

        print("\n== Drafts, and what a commit turns one into ==")
        fake_provider(envelope(name="Low disk", condition_text="disk.min_free_gb < 10"))
        error, draft = ai.draft_rule(db_path, CONFIG, "any drive under 10 GB", extra=extra,
                                     actor="a@x.com")
        stored = ai.save_draft(db_path, draft, actor="a@x.com", provider="openai_chat",
                               model="test-model")
        check("a draft can be stored and read back", stored and stored.get("id"))
        check("...keeping its expression",
              only(stored)["condition_text"] == "disk.min_free_gb < 10")
        check("...and the English", stored["source_text"] == "any drive under 10 GB")
        check("a draft is listed for its own author",
              [d["id"] for d in ai.list_drafts(db_path, actor="a@x.com")] == [stored["id"]])
        check("...and not for anybody else",
              ai.list_drafts(db_path, actor="b@x.com") == [])

        payload = ai.rule_payload(only(stored), source_text=stored["source_text"])
        check("a committed draft arrives DISABLED", payload["enabled"] is False)
        check("...and records where it came from",
              payload["description"].startswith("Drafted from: any drive under 10 GB"))
        error, saved = rules.save_rule(db_path, payload, actor="a@x.com", extra=extra)
        check("the payload is one rules.save_rule accepts", error is None and saved)

        summary = ai.summarise_rules(db_path, stored)[0]
        check("the summary is built from the expression, not from the model",
              "disk.min_free_gb < 10" in summary)
        check("...and says how many machines it reaches", "on 2 machine(s)" in summary)
        check("...and a lone rule is not numbered as a stage", "Stage" not in summary)

        check("a draft can be deleted", ai.delete_draft(db_path, stored["id"]))
        check("...and is gone", ai.get_draft(db_path, stored["id"]) is None)

        print("\n== A draft written before staging is still readable ==")
        # The rows already sitting in `ai_drafts` on every hub that had this feature on: one
        # rule at the TOP LEVEL of payload_json, no `rules` key. They are read in place rather
        # than migrated, so this is the only thing standing between an operator's unfinished
        # sentence and a KeyError after the upgrade -- and it is the branch no other test here
        # reaches, because draft_rule has not written that shape since 1.113.0.
        _err, legacy_condition = rules.parse_expression("disk.min_free_gb < 10", extra)
        legacy = {"name": "Old draft", "condition": legacy_condition,
                  "condition_text": "disk.min_free_gb < 10",
                  "target": {"include": [{"kind": "all"}], "exclude": []},
                  "actions": [{"type": "alert", "params": {"text": "hello"}}],
                  "for_seconds": 0, "cooldown_seconds": 0,
                  "source_text": "any drive under 10 GB"}
        old_row = ai.save_draft(db_path, legacy, actor="d@x.com")
        check("an old row has no `rules` key to read", "rules" not in old_row)
        # The DECODED row, not the dict as written: it carries id, actor and provider beside
        # the rule fields, and that is what every caller actually hands to draft_rules.
        stages = ai.draft_rules(old_row)
        check("...and still reads as exactly one stage", len(stages) == 1)
        check("...keeping its expression",
              stages[0]["condition_text"] == "disk.min_free_gb < 10")
        summaries = ai.summarise_rules(db_path, old_row)
        check("...summarised without a stage number", len(summaries) == 1
              and "Stage" not in summaries[0])
        error, saved = rules.save_rule(
            db_path, ai.rule_payload(stages[0], source_text=old_row["source_text"]),
            actor="d@x.com", extra=extra)
        check("...and commits to a rule the engine accepts", error is None and saved)
        ai.delete_draft(db_path, old_row["id"])

        print("\n== The per-actor draft cap ==")
        for i in range(ai.MAX_DRAFTS_PER_ACTOR + 5):
            ai.save_draft(db_path, dict(draft, source_text=f"draft {i}"), actor="c@x.com")
        check("a run of drafts is capped rather than unbounded",
              len(ai.list_drafts(db_path, actor="c@x.com", limit=500))
              == ai.MAX_DRAFTS_PER_ACTOR)

        print("\n== The audit trail ==")
        rows = ai.list_requests(db_path)
        check("every call was recorded", len(rows) >= 10)
        check("...with the outcome, so refusals are countable",
              {r["outcome"] for r in rows} >= {ai.OUTCOME_OK, ai.OUTCOME_REFUSED})
        check("...and the prompt's length rather than the prompt itself",
              all("prompt_chars" in r and "prompt" not in r for r in rows))
        check("...naming the model that answered",
              any(r["model"] == "test-model" for r in rows))

        print("\n== The machine panel withholds what the setting says it withholds ==")
        # The five identifiers a question about one PC carries for free, plus two readings
        # that are the actual substance of "why is this slow", plus a custom field -- which
        # is the case the allow-list exists for, because nobody can know what an operator
        # named it after.
        readings = {
            "sys.machine": rules.Value("PC-01", rules.KIND_TEXT, None),
            "session.user": rules.Value("a.wiens", rules.KIND_TEXT, 30),
            "hw.serial_number": rules.Value("5CG91ZXQ7T", rules.KIND_TEXT, None),
            "net.ipv4": rules.Value("10.4.2.17", rules.KIND_TEXT, 120),
            "ad.ou": rules.Value("OU=Sales,DC=corp", rules.KIND_TEXT, 600),
            "field.owner": rules.Value("Marta Benitez", rules.KIND_TEXT, None),
            "hw.model": rules.Value("EliteDesk 800 G6", rules.KIND_TEXT, None),
            "metric.cpu_temp": rules.Value(91.5, rules.KIND_NUMBER, 12),
            "sys.online": rules.Value(True, rules.KIND_BOOL, None),
            "metric.gpu_temp": rules.unknown(rules.KIND_NUMBER),
        }
        provider = fake_provider("The CPU is at 91.5 C -- see metric.cpu_temp, read 12s ago.")
        error, answer = ai.answer_machine_question(
            db_path, CONFIG, "PC-01", "why is it slow?",
            resolve_vars=lambda m: readings)
        check(f"the panel answers ({error})", error is None and answer)
        sent = json.dumps(provider.calls[0])
        for secret in ("PC-01", "a.wiens", "5CG91ZXQ7T", "10.4.2.17", "OU=Sales",
                       "Marta Benitez"):
            check(f"nothing identifying reaches the provider: {secret}",
                  secret not in sent)
        check("...while the readings that answer the question do", "91.5" in sent)
        check("...and so does configuration, which identifies nobody",
              "EliteDesk 800 G6" in sent)
        # The allow-list's whole point. A custom field is a text variable this hub has never
        # heard of, and the deny-list version of this filter would have sent it.
        check("a custom field is withheld without anybody having listed it",
              "field.owner" not in ai.SHAREABLE_TEXT
              and any(row["name"] == "field.owner" and row["withheld"]
                      for row in answer["snapshot"]))
        check("the snapshot says how many values were withheld", answer["withheld"] == 6)
        check("...and says the machine's name was not sent",
              answer["sent_machine_name"] is False)
        check("a variable the machine never reported reads as unknown, not as withheld",
              any(row["name"] == "metric.gpu_temp" and not row["known"]
                  and not row["withheld"] for row in answer["snapshot"]))
        check("...and the prompt says so, rather than leaving the model to guess",
              "Not reported by this machine" in sent and "metric.gpu_temp" in sent)
        check("the withheld NAMES travel, so the model stops asking for them",
              "session.user" in sent and "Withheld by this hub on purpose" in sent)
        check("the answer carries the readings it was built from, not just prose",
              len(answer["snapshot"]) == len(readings))

        # The other half of the setting. An operator on a LAN-only provider turns this on and
        # gets an answer that can name the PC it is about.
        named = dict(CONFIG, send_machine_names=True)
        provider = fake_provider("PC-01 is at 91.5 C.")
        error, answer = ai.answer_machine_question(
            db_path, named, "PC-01", "why is it slow?", resolve_vars=lambda m: readings)
        sent = json.dumps(provider.calls[0])
        check("with the setting on, the hostname does reach the provider", "PC-01" in sent)
        check("...and nothing is marked withheld", answer["withheld"] == 0)

        fake_provider()
        error, answer = ai.answer_machine_question(
            db_path, dict(CONFIG, enabled=False), "PC-01", "why?",
            resolve_vars=lambda m: readings)
        check("a hub with the feature off refuses before resolving anything",
              answer is None and error and "switched off" in error)

        print("\n== A fleet query is answered by the EVALUATOR, not by the model ==")
        asked = []

        def query_vars(machine):
            asked.append(machine)
            return {"metric.cpu_temp": rules.Value(95.0 if machine == "PC-01" else 40.0,
                                                   rules.KIND_NUMBER, 5)}

        fake_provider(json.dumps({"condition_text": "metric.cpu_temp > 90",
                                  "target": {"include": [{"kind": "all"}]}, "refusal": ""}))
        error, result = ai.fleet_query(db_path, CONFIG, "which machines are over 90?",
                                       in_scope=lambda m: True, resolve_vars=query_vars,
                                       extra=extra)
        check(f"a well-formed question is answered ({error})", error is None and result)
        check("...by the machines the evaluator said match, not by the model",
              [row["machine"] for row in result["results"]] == ["PC-01"])
        check("...with the tally over every machine, matched or not",
              result["tally"] == {"true": 1, "false": 1, "unknown": 0})
        check("...and the canonical expression, so the question asked is readable",
              result["condition_text"] == "metric.cpu_temp > 90")
        check("...carrying the operand values, so a row explains itself",
              result["results"][0]["detail"]["actual"] == 95.0)

        # The reason this is not text-to-SQL, asserted rather than described: a machine out of
        # reach is never RESOLVED, so it cannot appear in a tally either.
        asked.clear()
        fake_provider(json.dumps({"condition_text": "metric.cpu_temp > 90",
                                  "target": {"include": [{"kind": "all"}]}, "refusal": ""}))
        error, result = ai.fleet_query(db_path, CONFIG, "which machines are over 90?",
                                       in_scope=lambda m: m == "PC-01",
                                       resolve_vars=query_vars, extra=extra)
        check("a machine outside the caller's scope is never resolved", asked == ["PC-01"])
        check("...and is not counted, so the tally is not a machine census",
              result["targeted"] == 1 and result["tally"]["false"] == 0)

        fake_provider(json.dumps({"condition_text": "cpu.temp_c > 90", "refusal": ""}),
                      json.dumps({"condition_text": "cpu.temp_c > 90", "refusal": ""}))
        error, result = ai.fleet_query(db_path, CONFIG, "which machines are over 90?",
                                       in_scope=lambda m: True, resolve_vars=query_vars,
                                       extra=extra)
        check("a question naming a variable that does not exist is refused",
              result is None and error and "cpu.temp_c" in error)

        fake_provider(json.dumps({"refusal": "this hub has no record of installed software",
                                  "condition_text": ""}))
        error, result = ai.fleet_query(db_path, CONFIG, "which machines have Photoshop?",
                                       in_scope=lambda m: True, resolve_vars=query_vars,
                                       extra=extra)
        check("a question this namespace cannot ask comes back as the refusal it was asked for",
              result is None and error and "installed software" in error)

        fake_provider(json.dumps({"condition_text": "metric.cpu_temp > 90", "refusal": ""}))
        error, result = ai.fleet_query(db_path, CONFIG, "x" * (ai.MAX_REQUEST_CHARS + 1),
                                       in_scope=lambda m: True, resolve_vars=query_vars,
                                       extra=extra)
        check("an over-long question is refused before a provider is paid for it",
              result is None and error and "too long" in error)

        print("\n== The summary's figures are the hub's arithmetic, not the model's ==")
        alerts.init_alerts_db(db_path)
        now = 1_800_000_000
        # Two episodes on PC-01 and one on PC-02, one of which cleared inside the window --
        # the recovery half, which a query on created_at alone would silently drop.
        alerts.upsert_rule(db_path, "PC-01", 1, "Hot", "over 90", now=now - 3600)
        alerts.end_rule_episode(db_path, "PC-01", 1, now=now - 1800)
        alerts.upsert_rule(db_path, "PC-01", 2, "Low disk", "under 5 GB", now=now - 7200)
        alerts.upsert_rule(db_path, "PC-02", 1, "Hot", "over 90", now=now - 5400)
        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO rule_fires (rule_id, machine, fired_at, actions_json, outcome) "
            "VALUES (?,?,?,?,?)",
            [(1, "PC-01", now - 3600, "[]", "ok"),
             (1, "PC-01", now - 3000, "[]", "ok"),
             (1, "PC-02", now - 5400, "[]", "failed"),
             # Outside the window, and the reason fires_between exists rather than a capped
             # list: a report that counted this one would be reporting last week.
             (2, "PC-01", now - 20 * 86400, "[]", "ok")])
        conn.commit()
        conn.close()

        figures = ai.summary_figures(db_path, window_days=1, now=now)
        check("every episode raised in the window is counted", figures["raised"] == 3)
        check("...and the one that cleared is counted separately", figures["cleared"] == 1)
        check("fires outside the window are not counted", figures["fires"] == 3)
        check("...and the machines they touched are", figures["machines_affected"] == 2)
        check("an outcome that failed is visible rather than averaged away",
              {entry["name"]: entry["count"] for entry in figures["fire_outcomes"]}
              == {"ok": 2, "failed": 1})

        scoped = ai.summary_figures(db_path, window_days=1, in_scope=lambda m: m == "PC-01",
                                    now=now)
        check("a scoped operator's counts cover their own machines only",
              scoped["raised"] == 2 and scoped["fires"] == 2)
        check("...and the report says it was scoped", scoped["scoped"] is True)

        wide = ai.summary_figures(db_path, window_days=9999, now=now)
        check("the window is clamped to what retention can actually answer for",
              wide["window_days"] == ai.MAX_SUMMARY_WINDOW_DAYS)

        provider = fake_provider("Three alerts came up, one cleared, two remain open.")
        error, summary = ai.daily_summary(db_path, CONFIG, now=now)
        check(f"the summary is built ({error})", error is None and summary)
        check("...with the figures as lines, generated here rather than asked for",
              summary["lines"] and "3 alert episode(s) raised" in summary["lines"][0])
        check("...and only the covering note comes from the model",
              summary["prose"].startswith("Three alerts"))
        check("the model is shown the figures and nothing else",
              "alert episode(s) raised" in json.dumps(provider.calls[0]))

        fake_provider(("the AI provider could not be reached", None))
        error, summary = ai.daily_summary(db_path, CONFIG, now=now)
        check("a provider that will not answer costs the sentence, not the report",
              error is None and summary["lines"] and not summary["prose"])
        check("...and the missing note is named rather than left blank",
              "could not be reached" in summary["prose_error"])

        fake_provider()
        error, summary = ai.daily_summary(db_path, dict(CONFIG, enabled=False), now=now)
        check("the figures are arithmetic over local tables, so the off switch keeps them",
              error is None and summary["lines"] and not summary["prose"])
        check("...and no provider row is recorded for a call never made",
              all(row["kind"] != ai.KIND_SUMMARY or row["outcome"] != ai.OUTCOME_DISABLED
                  for row in ai.list_requests(db_path)))

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        ai.complete = real_complete
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
