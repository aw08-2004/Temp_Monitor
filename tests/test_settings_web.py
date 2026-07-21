"""HTTP-layer test for settings_web.py using a minimal Flask app + test client.
Avoids app.py's Google-OAuth boot requirement by wiring the blueprint directly,
exactly like test_fleet_web.py.
"""
import functools
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet
import settings
from settings_web import create_settings_blueprint
from permissions_web import create_access
from flask import Flask

PASS = 0
FAIL = 0

CURRENT_USER = "operator@x.com"


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


def audit_rows(db_path, action=None):
    with fleet.get_conn(db_path) as conn:
        if action:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def find_field(doc, key):
    for section in doc["sections"]:
        for field in section["fields"]:
            if field["key"] == key:
                return field
    return None


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        settings.init_settings_db(db_path)
        settings.invalidate()
        fleet.init_fleet_db(db_path)      # for audit_log

        app = Flask(__name__)
        app.secret_key = "test"
        # This module is about the settings endpoints, not about authorization, so both
        # operators it signs in as are break-glass superusers and the permission layer
        # is a pass-through. manage_settings refusals are covered in
        # test_permissions_web.py.
        app.register_blueprint(create_settings_blueprint(
            db_path, fake_login_required,
            create_access(db_path, {"operator@x.com", "ann@x.com"})))

        @app.before_request
        def _seed_session():
            from flask import session
            session["user"] = {"email": CURRENT_USER}

        c = app.test_client()

        print("\n== GET /api/settings ==")
        r = c.get("/api/settings")
        check("GET 200", r.status_code == 200)
        doc = r.get_json()
        check("all sections present in order",
              [s["name"] for s in doc["sections"]]
              == ["computer", "hub", "data", "metrics", "fleet", "deploy"])
        check("every registry key is served",
              sorted(f["key"] for s in doc["sections"] for f in s["fields"]) ==
              sorted(settings.BY_KEY))
        field = find_field(doc, "data.retention_days")
        check("field carries its current value", field["value"] == 30)
        check("field carries its default", field["default"] == 30)
        check("untouched field is marked default", field["is_default"] is True)

        print("\n== POST /api/settings: valid ==")
        r = c.post("/api/settings", json={"updates": {"data.retention_days": 45}})
        check("valid POST 200", r.status_code == 200)
        check("value persisted", settings.get(db_path, "data.retention_days") == 45)
        check("response carries the refreshed schema",
              find_field(r.get_json()["settings"], "data.retention_days")["value"] == 45)
        check("changed field no longer marked default",
              find_field(r.get_json()["settings"], "data.retention_days")["is_default"] is False)

        print("\n== POST /api/settings: validation ==")
        r = c.post("/api/settings", json={"updates": {"hub.overheat_threshold": 9999}})
        check("out-of-range POST 400", r.status_code == 400)
        check("error message names the field",
              "Overheat threshold" in r.get_json()["error"])
        check("nothing persisted on rejection",
              settings.get(db_path, "hub.overheat_threshold") == 85)

        # The whole batch must be rejected, not partly applied.
        r = c.post("/api/settings", json={"updates": {
            "data.retention_days": 90,
            "hub.overheat_threshold": 9999,
        }})
        check("mixed batch 400", r.status_code == 400)
        check("the valid field in a rejected batch is NOT applied",
              settings.get(db_path, "data.retention_days") == 45)

        r = c.post("/api/settings", json={"updates": {"hub.nope": 1}})
        check("unknown key 400", r.status_code == 400)
        r = c.post("/api/settings", json={"updates": "not-an-object"})
        check("non-object updates 400", r.status_code == 400)

        print("\n== CSRF: JSON content type is required ==")
        # get_json(silent=True) yields None for a form post, so `updates` is missing and
        # the request is rejected. This is what stops a cross-site form POST from
        # flipping hub.auto_update on a signed-in operator.
        r = c.post("/api/settings", data={"updates": '{"data.retention_days": 1}'})
        check("form-encoded POST rejected", r.status_code == 400)
        check("form-encoded POST changed nothing",
              settings.get(db_path, "data.retention_days") == 45)
        r = c.post("/api/settings", data="{}", content_type="text/plain")
        check("text/plain POST rejected", r.status_code == 400)

        print("\n== Audit trail ==")
        before = len(audit_rows(db_path, "settings.update"))
        CURRENT_USER = "ann@x.com"
        c.post("/api/settings", json={"updates": {"hub.low_load_threshold": 55}})
        rows = audit_rows(db_path, "settings.update")
        check("a change writes one audit row", len(rows) == before + 1)
        last = rows[-1]
        check("audit records the operator", last["actor"] == "ann@x.com")
        check("audit targets the key", last["target"] == "hub.low_load_threshold")
        detail = json.loads(last["detail_json"])
        check("audit records the old value", detail["from"] == 40)
        check("audit records the new value", detail["to"] == 55)
        CURRENT_USER = "operator@x.com"

        # A save that changes nothing must not fill the log with noise.
        before = len(audit_rows(db_path, "settings.update"))
        c.post("/api/settings", json={"updates": {"hub.low_load_threshold": 55}})
        check("a no-op save writes no audit row",
              len(audit_rows(db_path, "settings.update")) == before)

        print("\n== POST /api/settings/reset ==")
        r = c.post("/api/settings/reset", json={"keys": ["data.retention_days"]})
        check("reset 200", r.status_code == 200)
        check("reset reports the key", r.get_json()["keys"] == ["data.retention_days"])
        check("value back to default", settings.get(db_path, "data.retention_days") == 30)
        check("schema marks it default again",
              find_field(r.get_json()["settings"], "data.retention_days")["is_default"] is True)
        check("reset is audited", len(audit_rows(db_path, "settings.reset")) == 1)

        r = c.post("/api/settings/reset", json={"keys": ["data.retention_days"]})
        check("resetting an already-default key reports nothing removed",
              r.get_json()["keys"] == [])
        r = c.post("/api/settings/reset", json={"keys": "nope"})
        check("non-list keys 400", r.status_code == 400)

        print("\n== Tri-state bool over the wire ==")
        c.post("/api/settings", json={"updates": {"hub.auto_update": True}})
        check("explicit true persists", settings.get(db_path, "hub.auto_update") is True)
        c.post("/api/settings", json={"updates": {"hub.auto_update": None}})
        check("null persists as unset (follow .env)",
              settings.get(db_path, "hub.auto_update") is None)
        check("unset reads as default in the schema",
              find_field(c.get("/api/settings").get_json(),
                         "hub.auto_update")["is_default"] is True)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
