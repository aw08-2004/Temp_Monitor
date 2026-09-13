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
    body = {"name": "Test rule", "condition_text": "",
            "target": {"include": [{"kind": "all"}]},
            "actions": [{"type": "alert", "params": {"text": "hello"}}],
            "for_seconds": 0, "cooldown_seconds": 0, "refusal": ""}
    body.update(fields)
    return json.dumps(body)


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
        check("...with the canonical expression, not the model's spelling",
              draft["condition_text"] == rules.format_expression(draft["condition"]))
        check("...whose text parses again unchanged",
              rules.parse_expression(draft["condition_text"], extra)[0] is None)
        check("...and the variable it depends on is reported",
              draft["variables"] == ["disk.min_free_gb"])
        check("...keeping the English that produced it",
              draft["source_text"].startswith("alert me when"))

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
        error, _ = ai.complete(dict(CONFIG, base_url=""), [])
        check("a missing base URL is refused before any request", "base URL" in str(error))
        error, _ = ai.complete(dict(CONFIG, model=""), [])
        check("a missing model is refused before any request", "model" in str(error))

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

        print("\n== Drafts, and what a commit turns one into ==")
        fake_provider(envelope(name="Low disk", condition_text="disk.min_free_gb < 10"))
        error, draft = ai.draft_rule(db_path, CONFIG, "any drive under 10 GB", extra=extra,
                                     actor="a@x.com")
        stored = ai.save_draft(db_path, draft, actor="a@x.com", provider="openai_chat",
                               model="test-model")
        check("a draft can be stored and read back", stored and stored.get("id"))
        check("...keeping its expression", stored["condition_text"] == "disk.min_free_gb < 10")
        check("...and the English", stored["source_text"] == "any drive under 10 GB")
        check("a draft is listed for its own author",
              [d["id"] for d in ai.list_drafts(db_path, actor="a@x.com")] == [stored["id"]])
        check("...and not for anybody else",
              ai.list_drafts(db_path, actor="b@x.com") == [])

        payload = ai.rule_payload(stored)
        check("a committed draft arrives DISABLED", payload["enabled"] is False)
        check("...and records where it came from",
              payload["description"].startswith("Drafted from: any drive under 10 GB"))
        error, rule = rules.save_rule(db_path, payload, actor="a@x.com", extra=extra)
        check("the payload is one rules.save_rule accepts", error is None and rule)

        summary = ai.summarise_draft(db_path, stored)
        check("the summary is built from the expression, not from the model",
              "disk.min_free_gb < 10" in summary)
        check("...and says how many machines it reaches", "on 2 machine(s)" in summary)

        check("a draft can be deleted", ai.delete_draft(db_path, stored["id"]))
        check("...and is gone", ai.get_draft(db_path, stored["id"]) is None)

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

        print("\n== What is deliberately not built ==")
        for name, call in (
                ("the machine chat panel",
                 lambda: ai.answer_machine_question(db_path, CONFIG, "PC-01", "why slow?",
                                                    resolve_vars=lambda m: {})),
                ("natural-language fleet query",
                 lambda: ai.fleet_query(db_path, CONFIG, "which are hot?",
                                        in_scope=lambda m: True)),
                ("the fleet summary", lambda: ai.daily_summary(db_path, CONFIG))):
            try:
                call()
                check(f"{name} refuses rather than half-working", False)
            except NotImplementedError as exc:
                check(f"{name} refuses rather than half-working", "#24" in str(exc))

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
