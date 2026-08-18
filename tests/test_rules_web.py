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
import permissions
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

def authored(payload):
    """The rules this TEST created. Every hub carries one it did not: the boot migration
    seeds a disabled "High temperature" rule to replace the retired built-in alerter, and it
    targets the whole fleet, so it turns up in every list and on every machine page. Counting
    raw lengths here would make these assertions a test of the migration instead."""
    return [r for r in payload["rules"] if r["name"] != app.HIGH_TEMP_RULE_NAME]


resp = client.get("/api/rules")
check("list rules", resp.status_code == 200 and len(authored(resp.get_json())) == 1)
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
      resp.status_code == 200 and len(authored(resp.get_json())) == 2)
resp = client.get("/api/machines/RULEPC-2/rules")
check("...and a machine outside the target sees none",
      authored(resp.get_json()) == [])
# The seeded rule is fleet-wide, so it DOES apply to a machine no authored rule targets --
# which is how an operator finds it on the machine page and decides whether to enable it.
check("...but the seeded temperature rule still applies, disabled",
      [r for r in resp.get_json()["rules"]
       if r["name"] == app.HIGH_TEMP_RULE_NAME and not r["enabled"]])

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


# =======================================================================================
print("\n-- the action catalog --")

catalog = client.get("/api/rules/variables").get_json()
commands = {c["name"]: c for c in catalog["commands"]}
check("the catalog carries the commands a rule may issue", len(commands) >= 10)
check("...with run_script's parameters described",
      [p["name"] for p in commands["run_script"]["params"]]
      == ["script", "shell", "timeout_seconds"])
check("...including the bounds an operator is typing against",
      next(p for p in commands["restart"]["params"] if p["name"] == "delay_seconds")["maximum"]
      == 86400)
# A command that can never work is shown with its reason rather than dropped: a missing entry
# is a support question, a greyed-out one with a reason answers it.
check("install_driver is offered but marked unavailable",
      commands["install_driver"]["available"] is False
      and commands["install_driver"]["unavailable_reason"])
check("show_message is NOT offered as a raw command (its own action type covers it)",
      "show_message" not in commands)
check("update_bios is not offered at all", "update_bios" not in commands)
check("a command with no parameters is described as taking none",
      commands["gpupdate"]["described"] is True and commands["gpupdate"]["params"] == [])

check("the catalog carries the action types",
      {a["name"] for a in catalog["action_types"]} >= {"alert", "command", "script", "snooze"})
check("...and marks show_message as not nestable",
      next(a for a in catalog["action_types"]
           if a["name"] == "show_message")["nestable"] is False)
check("the catalog carries the button presets, translated",
      catalog["button_presets"]["yes_no"]["label"] == "Yes / No")
check("...and the non-button outcomes, translated",
      {o["name"] for o in catalog["outcomes"]}
      == {"timeout", "dismissed", "no_session", "failed"}
      and all(not o["label"].startswith("rules.") for o in catalog["outcomes"]))


# =======================================================================================
print("\n-- scripts --")

SCRIPT = {"name": "clear_spooler", "label": "Clear the print queue",
          "shell": "powershell", "timeout_seconds": 900,
          "body": 'Restart-Service -Name "{{input.service_name}}" # {{sys.machine}}',
          "inputs": [{"name": "service_name", "required": True}]}

resp = client.post("/api/rules/scripts", json=SCRIPT)
check("an operator with issue_commands can save a script", resp.status_code == 200)
resp = client.get("/api/rules/scripts")
check("the list is served", resp.status_code == 200 and len(resp.get_json()["scripts"]) == 1)
check("...WITHOUT the body", "body" not in resp.get_json()["scripts"][0])
check("...and says this caller may edit", resp.get_json()["can_edit"] is True)
check("the single-script read carries the body",
      client.get("/api/rules/scripts/clear_spooler").get_json()["body"].startswith("Restart-Service"))
check("an unknown script reads 404",
      client.get("/api/rules/scripts/ghost").status_code == 404)

resp = client.post("/api/rules/scripts", json=dict(SCRIPT, body="Write-Host {{field.owner}}"))
check("a script interpolating a custom field is refused", resp.status_code == 400)

# Saving a script silently changes what every rule using it does, WITHOUT touching those
# rules -- so the audit row is the only trace, and it has to name them.
rows = [r for r in fleet.list_audit(app.DB_PATH, action="save_script",
                                    limit=50)["entries"]]
check("saving a script is audited at security level",
      rows and rows[0]["level"] == fleet.LEVEL_SECURITY)

resp = client.post("/api/rules", json={
    "name": "Spooler fix", "target": {"include": [{"kind": "all"}], "exclude": []},
    "condition": {"var": "sys.uptime_days", "cmp": ">", "value": 1},
    "actions": [{"type": "script", "params": {"script": "clear_spooler",
                                              "inputs": {"service_name": "Spooler"}}}]})
check("a rule may reference the script", resp.status_code == 201)

resp = client.delete("/api/rules/scripts/clear_spooler")
check("deleting a script a rule uses is refused with 409", resp.status_code == 409)
check("...and the refusal names the rule", resp.get_json()["rules"] == ["Spooler fix"])
client.delete(f"/api/rules/{resp.status_code and client.get('/api/rules').get_json()['rules'][-1]['id']}")

# =======================================================================================
# THE ESCALATION PATH THIS DESIGN EXISTS TO CLOSE.
#
# A script body runs as SYSTEM, unattended, on every machine a rule targets. Writing one is
# therefore the same act as issuing a command and is gated on the same capability. Neither
# `view` (which can write a fleet FAVORITE, and that is fine because running one needs a human
# holding issue_commands) nor `manage_rules` (which may write rules but deliberately may not
# issue commands) is enough.
print("\n-- scripts: the capability gate --")

with client.session_transaction() as sess:
    sess["user"] = {"email": "root@x.com"}
for name, caps, member in (
        ("Viewers", [permissions.VIEW], "viewer@x.com"),
        ("Rule authors", [permissions.VIEW, permissions.MANAGE_RULES], "author@x.com")):
    client.post("/api/permissions/groups",
                json={"name": name, "capabilities": caps, "members": [member]})

for who, may_read_body in (("viewer@x.com", False), ("author@x.com", False)):
    with client.session_transaction() as sess:
        sess["user"] = {"email": who}
    label = who.split("@")[0]
    check(f"{label}: may LIST scripts (the rules editor needs the names)",
          client.get("/api/rules/scripts").status_code == 200)
    check(f"{label}: is told they may not edit",
          client.get("/api/rules/scripts").get_json()["can_edit"] is False)
    check(f"{label}: may NOT read a script's body",
          client.get("/api/rules/scripts/clear_spooler").status_code == 403)
    check(f"{label}: may NOT create a script",
          client.post("/api/rules/scripts", json=dict(SCRIPT, name="sneaky")).status_code == 403)
    check(f"{label}: may NOT overwrite an existing one",
          client.post("/api/rules/scripts",
                      json=dict(SCRIPT, body="whoami")).status_code == 403)
    check(f"{label}: may NOT delete one",
          client.delete("/api/rules/scripts/clear_spooler").status_code == 403)

with client.session_transaction() as sess:
    sess["user"] = {"email": "root@x.com"}
check("and the body was never actually changed",
      "Restart-Service" in client.get("/api/rules/scripts/clear_spooler").get_json()["body"])


print("\n-- gates --")
with client.session_transaction() as sess:
    sess["user"] = {"email": "nobody@x.com"}
resp = client.get("/api/rules")
check("a signed-in user with no capabilities cannot list rules", resp.status_code == 403)
resp = client.post("/api/rules", json=RULE)
check("...and cannot create one", resp.status_code == 403)

print(f"\n==== rules_web: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
