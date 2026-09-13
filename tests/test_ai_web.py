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
    "provider": ai.PROVIDER_OPENAI_CHAT,
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


def answer(condition_text, target=None, actions=None):
    """Stand in for the provider with one canned envelope."""
    body = {"name": "Drafted", "condition_text": condition_text,
            "target": target or {"include": [{"kind": "all"}]},
            "actions": actions or [{"type": "alert", "params": {"text": "hi"}}],
            "for_seconds": 0, "cooldown_seconds": 0, "refusal": ""}
    return lambda config, messages, **kwargs: (None, json.dumps(body))


def main():
    global CURRENT_USER
    real_complete = ai.complete
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
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
            lambda machine: {},
            lambda: {"max_targets_per_tick": 50, "command_cooldown_floor_seconds": 3600},
            lambda: dict(CONFIG),
            api_key=API_KEY))

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
        check("...with the canonical expression to preview",
              body.get("condition_text") == "disk.min_free_gb < 10")
        check("...and a summary built from it, not from the model",
              "disk.min_free_gb < 10" in body.get("summary", ""))
        check("...naming how many machines it would reach",
              "2 machine(s)" in body.get("summary", ""))

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
              "1 machine(s)" in r.get_json()["summary"])
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
        check("...and carries the change through", body["condition_text"]
              == "disk.min_free_gb < 5")
        check("...accumulating the English rather than replacing it",
              body["draft"]["source_text"].startswith("under 10 GB")
              and "make it 5" in body["draft"]["source_text"])
        r = c.delete(f"/api/ai/rules/drafts/{rid}")
        check("a draft can be deleted by its author", r.status_code == 200)
        check("...and is gone", ai.get_draft(db_path, rid) is None)

        print("\n== The two routes that are not built ==")
        CURRENT_USER = "scoped@x.com"
        r = c.post("/api/ai/machines/PC-02/ask", json={"text": "why slow?"})
        check("the machine route checks SCOPE, not just the capability",
              r.status_code == 403)
        r = c.post("/api/ai/machines/PC-01/ask", json={"text": "why slow?"})
        check("...and answers 501 for a machine they can see, not 404 or 500",
              r.status_code == 501)
        r = c.post("/api/ai/query", json={"text": "which are hot?"})
        check("fleet query answers 501 rather than half-working", r.status_code == 501)

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
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
