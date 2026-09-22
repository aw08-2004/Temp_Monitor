"""HTTP-layer test for ai_web.py (roadmap #24) -- the gates on the drafter.

Wires the blueprint onto a minimal Flask app, avoiding app.py's OAuth boot -- the same
approach as test_capabilities_web / test_wake_web / test_rules_web.

**The silent failure this file exists to catch is a drafted rule committed outside its
author's scope.** A draft is written by a model out of a sentence, so nobody read the target
selector before it arrived; `{"kind": "all"}` is the shape a model reaches for first, and a
scoped operator committing one would quietly acquire standing instructions over the whole
fleet. A leak like that does not look like a bug -- the rule works, and it works on machines
the author cannot see. The second thing checked here is the provider key, which must not
appear in any response under any shape.
"""
import functools
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "hub"))
import ai
import alerts
import fleet
import permissions
import rules
import scripts
import settings
from ai_web import create_ai_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"
API_KEY = "sk-do-not-leak-me"

CONFIG = {
    "enabled": True,
    "provider": ai.PRESET_CUSTOM,
    "base_url": "http://127.0.0.1:11434",
    "model": "test-model",
    "max_tokens": 512,
    "timeout_seconds": 30,
    "allow_private_endpoint": True,
    "send_machine_names": False,
    "draft_retention_days": 7,
}


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def _rule(condition_text, target=None, actions=None):
    return {"name": "Drafted", "condition_text": condition_text,
            "target": target or {"include": [{"kind": "all"}]},
            "actions": actions or [{"type": "alert", "params": {"text": "hi"}}],
            "for_seconds": 0, "cooldown_seconds": 0}


def answer(condition_text, target=None, actions=None):
    """Stand in for the provider with one canned envelope."""
    body = dict(_rule(condition_text, target, actions), refusal="")
    return lambda config, messages, **kwargs: (None, json.dumps(body))


def answer_stages(*stages):
    """Stand in for the provider with a canned ESCALATION -- one rule object per stage."""
    body = {"rules": [_rule(*stage) for stage in stages], "refusal": ""}
    return lambda config, messages, **kwargs: (None, json.dumps(body))


# What app.py's resolver hands back, in miniature: one hot machine, one cool one, and the
# hostname as a variable -- which is what makes the redaction assertions below meaningful.
RESOLVED = {
    "PC-01": {"metric.cpu_temp": rules.Value(95.0, rules.KIND_NUMBER, 5),
              "sys.machine": rules.Value("PC-01", rules.KIND_TEXT, None)},
    "PC-02": {"metric.cpu_temp": rules.Value(41.0, rules.KIND_NUMBER, 5),
              "sys.machine": rules.Value("PC-02", rules.KIND_TEXT, None)},
}


def resolve_vars(machine):
    return dict(RESOLVED.get(machine) or {})


def question(condition_text):
    """Stand in for the provider with one canned fleet-query envelope."""
    body = {"condition_text": condition_text, "target": {"include": [{"kind": "all"}]},
            "refusal": ""}
    return lambda config, messages, **kwargs: (None, json.dumps(body))


class FakeResponse:
    """The slice of a requests response that ai.py's streamed reads touch."""

    def __init__(self, chunks, status=200):
        self.chunks, self.status_code = chunks, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=8192):
        return iter(self.chunks)


def main():
    global CURRENT_USER
    real_complete = ai.complete
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    env_path, saved_key = None, None
    try:
        # fleet first: permissions.create_group writes an audit row, and audit_log is
        # fleet's table.
        fleet.init_fleet_db(db_path)
        rules.init_rules_db(db_path)
        scripts.init_scripts_db(db_path)
        ai.init_ai_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, ad_ou TEXT, "
                     "ad_dn TEXT)")
        conn.executemany("INSERT INTO machine_info VALUES (?,?,?)",
                         [("PC-01", "OU=Sales,DC=corp", ""),
                          ("PC-02", "OU=Lab,DC=corp", "")])
        conn.commit()
        conn.close()

        # The .env a key saved from the console is written to, and the key this hub "booted"
        # with. Both belong to this test and are put back in the finally below.
        env_fd, env_path = tempfile.mkstemp(suffix=".env")
        os.close(env_fd)
        saved_key = os.environ.get("AI_API_KEY")
        os.environ["AI_API_KEY"] = API_KEY

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        # A rule author who can see ONE machine. The whole point of the scope assertions.
        permissions.create_group(
            db_path, "Sales rules",
            capabilities=[permissions.VIEW, permissions.MANAGE_RULES],
            machines=["PC-01"], members=["scoped@x.com"])
        # Somebody who may look but not write.
        permissions.create_group(
            db_path, "Read only", capabilities=[permissions.VIEW],
            machines=["PC-01", "PC-02"], members=["viewer@x.com"])
        settings.invalidate()

        app.register_blueprint(create_ai_blueprint(
            db_path, fake_login_required, access,
            resolve_vars,
            lambda: {"max_targets_per_tick": 50, "command_cooldown_floor_seconds": 3600},
            lambda: dict(CONFIG),
            # A lookup, the way app.py passes it: a key saved through /api/ai/key has to be
            # visible to the very next request, which a string captured here never would be.
            api_key=lambda: os.environ.get("AI_API_KEY", ""),
            env_path=env_path))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()

        print("== Status never carries the key ==")
        r = c.get("/api/ai/status")
        body = r.get_json()
        check("status is readable with `view`", r.status_code == 200)
        check("...and reports the model it is pointed at", body.get("model") == "test-model")
        check("...and the key appears nowhere in the response",
              API_KEY not in json.dumps(body))
        check("...not even as a field name", "api_key" not in body)

        CURRENT_USER = "viewer@x.com"
        r = c.get("/api/ai/status")
        check("a read-only operator can still see whether the feature exists",
              r.status_code == 200)
        check("...with no key in that answer either", API_KEY not in r.get_data(as_text=True))

        print("\n== Drafting is manage_rules, not view ==")
        ai.complete = answer("disk.min_free_gb < 10")
        r = c.post("/api/ai/rules/draft", json={"text": "any drive under 10 GB"})
        check("`view` alone cannot draft a rule", r.status_code == 403)

        CURRENT_USER = "super@x.com"
        r = c.post("/api/ai/rules/draft", json={"text": "any drive under 10 GB"})
        body = r.get_json()
        check("a rule author can draft", r.status_code == 201)
        draft_id = (body.get("draft") or {}).get("id")
        check("...and gets a draft id back", bool(draft_id))
        drafted = body.get("rules") or []
        check("...with the canonical expression to preview",
              len(drafted) == 1 and drafted[0]["condition_text"] == "disk.min_free_gb < 10")
        check("...and a summary built from it, not from the model",
              "disk.min_free_gb < 10" in drafted[0]["summary"])
        check("...naming how many machines it would reach",
              "2 machine(s)" in drafted[0]["summary"])
        check("...and the target, because the console previews against it",
              drafted[0]["target"] == {"include": [{"kind": "all"}], "exclude": []})

        print("\n== A draft belongs to the person who wrote it ==")
        CURRENT_USER = "scoped@x.com"
        r = c.get(f"/api/ai/rules/drafts/{draft_id}")
        check("another operator cannot read somebody else's draft", r.status_code == 404)
        r = c.get("/api/ai/rules/drafts")
        check("...and it is not in their listing", r.get_json()["drafts"] == [])
        r = c.post(f"/api/ai/rules/drafts/{draft_id}/commit", json={})
        check("...nor commit it", r.status_code == 404)

        print("\n== A scoped author cannot commit a fleet-wide draft ==")
        ai.complete = answer("disk.min_free_gb < 10")
        r = c.post("/api/ai/rules/draft", json={"text": "any drive under 10 GB"})
        check("a scoped author can draft", r.status_code == 201)
        scoped_id = r.get_json()["draft"]["id"]
        check("...and the summary counts only what they can see",
              "1 machine(s)" in r.get_json()["rules"][0]["summary"])
        r = c.post(f"/api/ai/rules/drafts/{scoped_id}/commit", json={})
        body = r.get_json()
        check("committing a target that reaches outside their scope is refused",
              r.status_code == 400)
        check("...naming the machine they may not touch",
              "PC-02" in body.get("error", ""))
        check("...and no rule was created", rules.list_rules(db_path) == [])

        print("\n== A draft inside scope commits ==")
        ai.complete = answer("disk.min_free_gb < 10",
                             target={"include": [{"kind": "machines",
                                                  "machines": ["PC-01"]}]})
        r = c.post("/api/ai/rules/draft", json={"text": "PC-01 only, under 10 GB"})
        in_scope_id = r.get_json()["draft"]["id"]
        r = c.post(f"/api/ai/rules/drafts/{in_scope_id}/commit", json={})
        body = r.get_json()
        check("a draft naming only machines they can see commits", r.status_code == 201)
        rule = body.get("rule") or {}
        check("...arriving DISABLED rather than live", rule.get("enabled") in (0, False))
        # Persisted, not merely checked at save time: a dynamic target saved by a scoped
        # author must not grow past them later. rules._decode_rule surfaces it as
        # `author_scope`.
        check("...with the author's scope stamped on it for evaluation time",
              rule.get("author_scope") == ["PC-01"])
        check("...and the English that produced it kept",
              body.get("source_text") == "PC-01 only, under 10 GB")
        check("...and the draft consumed", ai.get_draft(db_path, in_scope_id) is None)

        before_rules = len(rules.list_rules(db_path))
        print("\n== An escalation commits one stage at a time ==")
        # The failure: committing the warning stage threw the enforcement stage away with the
        # draft row, so an operator who created the gentle rule lost the one that acts and had
        # nothing in the console to say a second rule had ever been drafted.
        CURRENT_USER = "super@x.com"
        ai.complete = answer_stages(("sys.uptime_days > 5",),
                                    ("sys.uptime_days > 10", None,
                                     [{"type": "command",
                                       "params": {"command_type": "restart", "params": {}}}]))
        r = c.post("/api/ai/rules/draft", json={"text": "warn at 5 days, restart at 10"})
        body = r.get_json()
        staged_id = body["draft"]["id"]
        check("one sentence drafts both stages", len(body["rules"]) == 2)
        check("...each numbered so the escalation reads in order",
              body["rules"][0]["summary"].startswith("Stage 1 of 2"))
        check("...and each carrying the index commit takes",
              [entry["index"] for entry in body["rules"]] == [0, 1])

        r = c.post(f"/api/ai/rules/drafts/{staged_id}/commit", json={"index": 99})
        check("an index the draft does not have is a 400, not a stray commit",
              r.status_code == 400)
        check("...and nothing was created", len(rules.list_rules(db_path)) == before_rules)

        r = c.post(f"/api/ai/rules/drafts/{staged_id}/commit", json={"index": 1})
        body = r.get_json()
        check("the stage an operator picked is the one created", r.status_code == 201
              and body["rule"]["condition_text"] == "sys.uptime_days > 10")
        check("...arriving DISABLED like any drafted rule",
              body["rule"]["enabled"] in (0, False))
        check("...described by the whole sentence, not half of it",
              body["rule"]["description"].startswith("Drafted from: warn at 5 days"))
        check("...and the other stage survives the commit",
              [entry["condition_text"] for entry in body["remaining"]]
              == ["sys.uptime_days > 5"])
        check("...still readable as a draft", ai.get_draft(db_path, staged_id) is not None)
        check("...renumbered from what is actually left",
              [entry["index"] for entry in body["remaining"]] == [0])

        r = c.post(f"/api/ai/rules/drafts/{staged_id}/commit", json={"index": 0})
        body = r.get_json()
        check("committing the last stage creates it too", r.status_code == 201
              and body["rule"]["condition_text"] == "sys.uptime_days > 5")
        check("...and only then is the draft consumed",
              body["remaining"] == [] and ai.get_draft(db_path, staged_id) is None)

        print("\n== A draft written before staging still commits ==")
        # A row already in `ai_drafts` when the hub was upgraded: the rule at the top level,
        # no `rules` key, and no index in the request either. Both defaults have to hold or
        # somebody's unfinished sentence turns into a 400 the morning after an upgrade.
        CURRENT_USER = "super@x.com"
        _err, legacy_condition = rules.parse_expression("disk.min_free_gb < 10", {})
        legacy = ai.save_draft(db_path, {
            "name": "Old draft", "condition": legacy_condition,
            "condition_text": "disk.min_free_gb < 10",
            "target": {"include": [{"kind": "all"}], "exclude": []},
            "actions": [{"type": "alert", "params": {"text": "hi"}}],
            "for_seconds": 0, "cooldown_seconds": 0, "source_text": "an old sentence",
        }, actor="super@x.com")
        r = c.get(f"/api/ai/rules/drafts/{legacy['id']}")
        check("an old draft still renders as one rule",
              r.status_code == 200 and len(r.get_json()["rules"]) == 1)
        before_legacy = len(rules.list_rules(db_path))
        r = c.post(f"/api/ai/rules/drafts/{legacy['id']}/commit", json={})
        body = r.get_json()
        check("...and commits with no index given", r.status_code == 201
              and body["rule"]["condition_text"] == "disk.min_free_gb < 10")
        check("...creating exactly one rule",
              len(rules.list_rules(db_path)) == before_legacy + 1)
        check("...and consuming the draft", body["remaining"] == []
              and ai.get_draft(db_path, legacy["id"]) is None)

        print("\n== A model's invented variable is refused with the parser's words ==")
        ai.complete = answer("cpu.temp_c > 90")
        r = c.post("/api/ai/rules/draft", json={"text": "hot CPUs"})
        check("a draft naming a variable that does not exist is a 400", r.status_code == 400)
        check("...and the operator is told which name",
              "cpu.temp_c" in r.get_json().get("error", ""))

        print("\n== Refining, and deleting ==")
        ai.complete = answer("disk.min_free_gb < 10",
                             target={"include": [{"kind": "machines",
                                                  "machines": ["PC-01"]}]})
        r = c.post("/api/ai/rules/draft", json={"text": "under 10 GB"})
        rid = r.get_json()["draft"]["id"]
        ai.complete = answer("disk.min_free_gb < 5",
                             target={"include": [{"kind": "machines",
                                                  "machines": ["PC-01"]}]})
        r = c.post(f"/api/ai/rules/drafts/{rid}/refine", json={"text": "make it 5"})
        body = r.get_json()
        check("a refinement keeps the same draft id", body["draft"]["id"] == rid)
        check("...and carries the change through",
              body["rules"][0]["condition_text"] == "disk.min_free_gb < 5")
        check("...accumulating the English rather than replacing it",
              body["draft"]["source_text"].startswith("under 10 GB")
              and "make it 5" in body["draft"]["source_text"])
        r = c.delete(f"/api/ai/rules/drafts/{rid}")
        check("a draft can be deleted by its author", r.status_code == 200)
        check("...and is gone", ai.get_draft(db_path, rid) is None)

        print("\n== Asking ABOUT a machine is capability AND scope ==")
        CURRENT_USER = "scoped@x.com"
        ai.complete = lambda config, messages, **kwargs: (None, "It is at 95 C.")
        r = c.post("/api/ai/machines/PC-02/ask", json={"text": "why slow?"})
        check("the machine route checks SCOPE, not just the capability",
              r.status_code == 403)
        r = c.post("/api/ai/machines/PC-01/ask", json={"text": "why slow?"})
        body = r.get_json()
        check("...and answers for a machine they can see", r.status_code == 200)
        check("...carrying the readings the answer was built from, not prose alone",
              any(row["name"] == "metric.cpu_temp" for row in body["snapshot"]))
        check("...with the hostname withheld, the setting being off",
              body["sent_machine_name"] is False
              and any(row["name"] == "sys.machine" and row["withheld"]
                      for row in body["snapshot"]))
        check("no route leaks the provider key", API_KEY not in json.dumps(body))

        print("\n== A fleet query answers about the caller's machines only ==")
        CURRENT_USER = "viewer@x.com"
        ai.complete = question("metric.cpu_temp > 90")
        r = c.post("/api/ai/query", json={"text": "which are hot?"})
        body = r.get_json()
        check("a viewer may ask -- this is reading the fleet, not authoring a rule",
              r.status_code == 200)
        check("...and is answered by the evaluator",
              [row["machine"] for row in body["results"]] == ["PC-01"])
        check("...over both machines they can see", body["targeted"] == 2)

        CURRENT_USER = "scoped@x.com"
        ai.complete = question("metric.cpu_temp > 90")
        r = c.post("/api/ai/query", json={"text": "which are hot?"})
        body = r.get_json()
        check("an operator scoped to one machine is answered about one machine",
              body["targeted"] == 1 and body["tally"]["false"] == 0)

        print("\n== The summary is scoped, and survives a provider that is not there ==")
        alerts.init_alerts_db(db_path)
        alerts.upsert_rule(db_path, "PC-01", 1, "Hot", "over 90")
        alerts.upsert_rule(db_path, "PC-02", 1, "Hot", "over 90")
        CURRENT_USER = "scoped@x.com"
        ai.complete = lambda config, messages, **kwargs: (None, "One alert is open.")
        r = c.post("/api/ai/summary", json={"window_days": 1})
        body = r.get_json()
        check("a scoped operator gets a scoped count, not a fleet census",
              r.status_code == 200 and body["figures"]["raised"] == 1)
        check("...marked as scoped", body["figures"]["scoped"] is True)
        r = c.get("/api/ai/summary")
        check("the summary is a POST, so a third party's page cannot spend a provider on it",
              r.status_code == 405)
        ai.complete = lambda config, messages, **kwargs: (
            "the AI provider could not be reached", None)
        r = c.post("/api/ai/summary", json={"window_days": 1})
        body = r.get_json()
        check("a provider that will not answer still returns the figures",
              r.status_code == 200 and body["lines"] and not body["prose"])
        check("...and names what is missing", "could not be reached" in body["prose_error"])

        print("\n== The model list, and who may refresh it ==")
        CURRENT_USER = "scoped@x.com"
        r = c.get("/api/ai/models")
        body = r.get_json()
        check("a rule author can read the cached model list", r.status_code == 200)
        check("...which is empty until somebody refreshes it", body["models"] == [])
        check("...and says they may NOT refresh it, holding no manage_settings",
              body["can_refresh"] is False)
        r = c.post("/api/ai/models/refresh", json={})
        check("refreshing is manage_settings, not manage_rules", r.status_code == 403)

        CURRENT_USER = "super@x.com"
        real_get = ai.requests.get
        try:
            ai.requests.get = lambda *a, **k: FakeResponse(
                [json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}]}).encode()])
            r = c.post("/api/ai/models/refresh", json={})
            check("an admin can refresh", r.status_code == 200)
            check("...and gets the provider's list back, sorted",
                  r.get_json()["models"] == ["a-model", "b-model"])
            r = c.get("/api/ai/models")
            check("...which is then cached for everyone",
                  r.get_json()["models"] == ["a-model", "b-model"])

            # A provider that will not answer is THEIR fault, not this hub's misconfiguration,
            # and the two need different fixes -- so they get different status codes.
            ai.requests.get = lambda *a, **k: FakeResponse([b""], status=500)
            r = c.post("/api/ai/models/refresh", json={})
            check("a provider that fails answers 502, not 400", r.status_code == 502)
        finally:
            ai.requests.get = real_get

        def env_text():
            with open(env_path, encoding="utf-8") as handle:
                return handle.read()

        print()
        print("== The API key is set from Settings, and never comes back out ==")
        CURRENT_USER = "scoped@x.com"
        r = c.post("/api/ai/key", json={"key": "sk-scoped-attempt"})
        check("setting the key is manage_settings, not manage_rules", r.status_code == 403)
        check("...and nothing was written", "sk-scoped-attempt" not in env_text())

        CURRENT_USER = "super@x.com"
        r = c.get("/api/ai/status")
        check("status says this hub can write the key",
              r.get_json().get("can_write_key") is True)

        r = c.post("/api/ai/key", json={"key": "  sk-new-key-123  "})
        body = r.get_json()
        check("an admin can set the key", r.status_code == 200)
        check("...and the answer says a key is set", body.get("has_api_key") is True)
        check("...without the key in it", "sk-new-key-123" not in json.dumps(body))
        check("the key is written to .env, trimmed",
              "AI_API_KEY=sk-new-key-123" in env_text())
        check("...and to the live environment, so no restart is needed",
              os.environ.get("AI_API_KEY") == "sk-new-key-123")
        r = c.get("/api/ai/status")
        check("status still never carries it",
              "sk-new-key-123" not in r.get_data(as_text=True))
        rows = fleet.list_audit(db_path, action="ai_api_key_set", limit=5)["entries"]
        check("setting the key is audited", bool(rows))
        check("...and the audit row does not contain the key",
              "sk-new-key-123" not in json.dumps(rows))

        # The injection this route exists to refuse. The key is one line of a file that also
        # holds ALLOWED_EMAILS, so a line break would write a second line into the perimeter.
        injected = "sk-x" + chr(10) + "ALLOWED_EMAILS=attacker@evil.example"
        r = c.post("/api/ai/key", json={"key": injected})
        check("a key containing a line break is refused", r.status_code == 400)
        check("...and .env gained no second line",
              "attacker@evil.example" not in env_text())
        check("...and the previous key is untouched",
              os.environ.get("AI_API_KEY") == "sk-new-key-123")
        r = c.post("/api/ai/key", json={"key": "k" * 600})
        check("an absurdly long key is refused", r.status_code == 400)

        r = c.post("/api/ai/key", json={"key": ""})
        check("an empty key removes it",
              r.status_code == 200 and r.get_json().get("has_api_key") is False)
        check("...from .env entirely, not left as an empty assignment",
              "AI_API_KEY" not in env_text())
        check("...and from the live environment", "AI_API_KEY" not in os.environ)
        rows = fleet.list_audit(db_path, action="ai_api_key_cleared", limit=5)["entries"]
        check("removing the key is audited too", bool(rows))

        # Back the way the rest of this file expects the hub to be.
        os.environ["AI_API_KEY"] = API_KEY

        print("\n== With the feature switched off ==")
        CURRENT_USER = "super@x.com"
        ai.complete = real_complete
        try:
            CONFIG["enabled"] = False
            r = c.get("/api/ai/status")
            body = r.get_json()
            check("status says plainly that it is off",
                  body.get("enabled") is False and body.get("ready") is False)
            r = c.post("/api/ai/rules/draft", json={"text": "anything"})
            check("drafting is refused while it is off", r.status_code == 400)
            check("...in words that say where to switch it on",
                  "switched off" in r.get_json().get("error", ""))
        finally:
            CONFIG["enabled"] = True

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        ai.complete = real_complete
        if env_path:
            try:
                os.remove(env_path)
            except OSError:
                pass
        if saved_key is None:
            os.environ.pop("AI_API_KEY", None)
        else:
            os.environ["AI_API_KEY"] = saved_key
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
