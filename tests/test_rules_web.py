"""Tests the rules engine's HTTP surface (rules_web.py) against the real app.

test_rules.py covers the engine itself with no Flask involved. This one covers what the
console can actually reach: the gates (view vs manage_rules vs issue_commands), the scope
refusal on targets, and the round trip of writing a rule through the API and having the
evaluator act on it.

Run from the repo root so `import app` resolves.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-rules-web-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@x.com"

import app
import fleet
import rules

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


client = app.app.test_client()
with client.session_transaction() as sess:
    sess["user"] = {"email": "root@x.com"}


def report(machine, temp=45.0, uptime=None):
    body = {"machine": machine, "temp": temp, "serial_number": f"SN-{machine}",
            "model": "TestModel"}
    if uptime is not None:
        body["uptime_seconds"] = uptime
    return client.post("/api/report", json=body)


# Two machines, one of them up for nine days.
report("RULEPC-1", uptime=9 * 86400)
report("RULEPC-2", uptime=3600)


print("\n-- the variable catalog --")
resp = client.get("/api/rules/variables")
check("GET /api/rules/variables 200", resp.status_code == 200)
catalog = resp.get_json()
names = {v["name"] for v in catalog["variables"]}
check("catalog carries the core variables",
      {"sys.uptime_days", "metric.cpu_temp", "disk.max_used_pct"} <= names)
check("every entry offers the operators for its kind",
      all(v["operators"] for v in catalog["variables"]))
check("catalog does NOT carry per-volume entries (they are per machine)",
      not any(n.startswith("disk.c.") for n in names))
check("operator labels are translated",
      catalog["operators"][">"] and catalog["operators"][">"] != ">")

resp = client.get("/api/rules/variables/RULEPC-1")
check("GET /api/rules/variables/<machine> 200", resp.status_code == 200)
per_machine = {v["name"]: v for v in resp.get_json()["variables"]}
check("uptime resolves for a real machine",
      per_machine["sys.uptime_days"]["known"]
      and per_machine["sys.uptime_days"]["value"] >= 8.9)
check("an unreported variable is reported as unknown, not omitted",
      per_machine["bios.version"]["known"] is False)


print("\n-- custom fields --")
resp = client.post("/api/rules/fields", json={"name": "location", "label": "Location",
                                              "kind": "text"})
check("create a field", resp.status_code == 201)

resp = client.post("/api/rules/fields", json={"name": "Bad Name", "label": "x",
                                              "kind": "text"})
check("a bad field name is refused", resp.status_code == 400)

resp = client.post("/api/rules/fields/location/values",
                   json={"machines": ["RULEPC-1", "RULEPC-2"], "value": "Branch 2"})
check("bulk set 200", resp.status_code == 200 and resp.get_json()["updated"] == 2)

resp = client.get("/api/machines/RULEPC-1/fields")
check("read a machine's fields",
      resp.status_code == 200
      and resp.get_json()["fields"][0]["value"] == "Branch 2")

resp = client.get("/api/rules/variables")
check("a new field appears in the catalog immediately",
      "field.location" in {v["name"] for v in resp.get_json()["variables"]})


print("\n-- targets --")
resp = client.post("/api/rules/targets", json={"target": {"include": [{"kind": "all"}]}})
check("resolve all", resp.status_code == 200 and resp.get_json()["count"] >= 2)

resp = client.post("/api/rules/targets", json={
    "target": {"include": [{"kind": "all"}],
               "exclude": [{"kind": "machines", "machines": ["RULEPC-1"]}]}})
check("all except one", "RULEPC-1" not in resp.get_json()["machines"])

resp = client.post("/api/rules/targets", json={
    "target": {"include": [{"kind": "field", "field": "location", "value": "Branch 2"}]}})
check("target by custom field", resp.get_json()["count"] == 2)

resp = client.post("/api/rules/targets", json={"target": {"include": []}})
check("a target with no include is refused", resp.status_code == 400)


print("\n-- preview --")
resp = client.post("/api/rules/preview", json={
    "condition_text": "sys.uptime_days > 7",
    "target": {"include": [{"kind": "all"}]}})
check("preview 200", resp.status_code == 200)
preview = resp.get_json()
check("preview finds the machine that is up 9 days", preview["tally"]["true"] == 1)
check("...and the one that is not", preview["tally"]["false"] == 1)
check("preview returns the canonical text",
      preview["condition_text"] == "sys.uptime_days > 7")
check("preview explains each machine's leaves",
      preview["results"][0]["detail"]["var"] == "sys.uptime_days")

resp = client.post("/api/rules/preview", json={"condition_text": "nosuch.var > 1"})
check("preview refuses an unknown variable", resp.status_code == 400)

resp = client.post("/api/rules/preview", json={
    "condition_text": "bios.password_set == true",
    "target": {"include": [{"kind": "all"}]}})
check("a condition over data nobody reports is unknown everywhere, not false",
      resp.get_json()["tally"]["unknown"] == 2
      and resp.get_json()["tally"]["false"] == 0)


print("\n-- rules --")
RULE = {
    "name": "Uptime nag",
    "target": {"include": [{"kind": "machines", "machines": ["RULEPC-1"]}]},
    "condition_text": "sys.uptime_days > 7",
    "actions": [{"type": "alert", "params": {"text": "{{sys.machine}} needs a restart"}}],
    "for_seconds": 0,
    "cooldown_seconds": 0,
}
resp = client.post("/api/rules", json=RULE)
check("create a rule", resp.status_code == 201)
rule_id = resp.get_json()["id"]
check("the condition was stored as an AST",
      resp.get_json()["condition"] == {"var": "sys.uptime_days", "cmp": ">", "value": 7})

resp = client.get("/api/rules")
check("list rules", resp.status_code == 200 and len(resp.get_json()["rules"]) == 1)
check("the list reports the fleet-wide switches",
      resp.get_json()["command_actions_enabled"] is False)

resp = client.put(f"/api/rules/{rule_id}", json={**RULE, "name": "Renamed"})
check("update a rule", resp.status_code == 200 and resp.get_json()["name"] == "Renamed")

resp = client.post("/api/rules", json={**RULE, "condition_text": "sys.uptime_days >"})
check("a malformed condition is refused", resp.status_code == 400)

resp = client.post("/api/rules", json={**RULE, "actions": [
    {"type": "command", "params": {"command_type": "shell_open"}}]})
check("a session-bound command is refused as a rule action", resp.status_code == 400)

resp = client.post("/api/rules", json={**RULE, "actions": [
    {"type": "webhook", "params": {"url": "http://plain.example.com/x"}}]})
check("a plain-http webhook is refused", resp.status_code == 400)

# A message rule, with the full Yes/No/Later routing.
MESSAGE_RULE = {
    **RULE,
    "name": "Restart nag",
    "actions": [{
        "type": "show_message",
        "params": {"title": "Restart required",
                   "body": "Up for {{sys.uptime_days}} days. Restart now?",
                   "preset": "yes_no_later", "timeout_seconds": 900},
        "on_response": {
            "yes": [{"type": "command", "params": {"command_type": "restart"}}],
            "later": [{"type": "snooze", "params": {"seconds": 14400}}],
            "no": [{"type": "alert", "params": {"text": "declined on {{sys.machine}}"}}],
        },
    }],
}
resp = client.post("/api/rules", json=MESSAGE_RULE)
check("create an interactive message rule", resp.status_code == 201)
message_rule_id = resp.get_json()["id"]
check("the button preset expanded server-side",
      [b["id"] for b in resp.get_json()["actions"][0]["params"]["buttons"]]
      == ["yes", "no", "later"])
check("a command cooldown floor was applied",
      resp.get_json()["cooldown_seconds"] >= 3600)

resp = client.get("/api/machines/RULEPC-1/rules")
check("the machine page can see which rules apply",
      resp.status_code == 200 and len(resp.get_json()["rules"]) == 2)
resp = client.get("/api/machines/RULEPC-2/rules")
check("...and a machine outside the target sees none",
      resp.get_json()["rules"] == [])

resp = client.put(f"/api/rules/{rule_id}/enabled", json={"enabled": False})
check("disable a rule", resp.status_code == 200 and resp.get_json()["enabled"] is False)


print("\n-- the evaluator, end to end --")
issued = []
_real_create = fleet.create_command


def fake_create(db_path, machine, command_type, params, issued_by=None, ttl_seconds=None):
    issued.append({"machine": machine, "type": command_type, "params": params,
                   "issued_by": issued_by, "ttl": ttl_seconds})
    return f"cmd-{len(issued)}"


fleet.create_command = fake_create
rules.fleet.create_command = fake_create

summary = app.evaluate_rules_once()
check("the message rule fired", summary["fired"] == 1)
check("a show_message command was queued",
      len(issued) == 1 and issued[0]["type"] == "show_message")
check("the body was templated with the real uptime",
      issued[0]["params"]["body"].startswith("Up for 9"))
check("the TTL was stretched past the dialog timeout", issued[0]["ttl"] > 900)
check("the disabled rule did not fire", summary["rules"] == 1)

# The user clicks "Later": the rule must snooze rather than re-ask.
app._on_command_result("cmd-1", "RULEPC-1", True, None,
                       json.dumps({"outcome": "later"}))
issued.clear()
app.evaluate_rules_once()
check("a snoozed machine is not asked again", not issued)

resp = client.get(f"/api/rules/{message_rule_id}/fires")
fires = resp.get_json()["fires"]
check("the fire history records the outcome",
      len(fires) == 1 and fires[0]["outcome"] == "later")

# And "Yes" issues the restart -- through the same fuses, so with commands disarmed it does
# not actually go out.
issued.clear()
app.evaluate_rules_once(now=int(time.time()) + 20000)
if issued:
    app._on_command_result(f"cmd-{len(issued)}", "RULEPC-1", True, None,
                           json.dumps({"outcome": "yes"}))
check("with command actions disarmed, Yes does not issue a restart",
      not any(c["type"] == "restart" for c in issued))

fleet.create_command = _real_create


print("\n-- gates --")
with client.session_transaction() as sess:
    sess["user"] = {"email": "nobody@x.com"}
resp = client.get("/api/rules")
check("a signed-in user with no capabilities cannot list rules", resp.status_code == 403)
resp = client.post("/api/rules", json=RULE)
check("...and cannot create one", resp.status_code == 403)

print(f"\n==== rules_web: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
