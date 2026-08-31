"""HTTP-layer test for patches_web.py using a minimal Flask app + test client.
Avoids app.py's Google-OAuth boot requirement by wiring the blueprint directly, exactly
like test_packages_web.py.

Like packages', this run does NOT sign every operator in as a break-glass superuser,
because the interesting contract here IS the authorization shape and it is deliberately
lopsided:

  * reading inventory and compliance needs only `view` -- a helpdesk that cannot see a
    missing security update is the failure this gate is set loose to avoid,
  * approving, scheduling and running need `manage_patches`,
  * reads are NARROWED to the caller's machines, while writes are ALL-OR-NOTHING.

So the run switches between a superuser, an operator scoped to one machine, and a viewer
who may look at two machines and change nothing. The assertions that matter most are the
ones where a wrong answer still looks like a working page: a scoped operator seeing the
whole fleet's update list, or a run quietly starting on nine of the ten machines asked
for.
"""
import functools
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import patches
import permissions
import settings
from patches_web import create_patches_blueprint
from permissions_web import create_access
from flask import Flask

PASS = 0
FAIL = 0

CURRENT_USER = "root@x.com"


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


def report(uid, classification="security"):
    return {"uid": uid, "source": patches.SOURCE_WINDOWS_UPDATE, "kb": uid,
            "title": f"Update {uid}", "classification": classification,
            "reboot_required": True}


def audit_actions(db_path):
    with fleet.get_conn(db_path) as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


def main():
    global CURRENT_USER
    workdir = tempfile.mkdtemp(prefix="patchweb-tests-")
    log_dir = os.path.join(workdir, "logs")
    os.makedirs(log_dir)
    db_path = os.path.join(log_dir, "hub.db")
    try:
        fleet.init_fleet_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        patches.init_patches_db(db_path)
        permissions.init_permissions_db(db_path)
        permissions.invalidate()

        # An operator who may patch, but only HOSPITAL-1.
        permissions.create_group(
            db_path, name="Hospital IT",
            capabilities=[permissions.VIEW, permissions.MANAGE_PATCHES],
            machines=["HOSPITAL-1"], members=["hospital@x.com"], actor="root@x.com")
        # An operator who may look at two machines and change nothing.
        permissions.create_group(
            db_path, name="Viewers", capabilities=[permissions.VIEW],
            machines=["HOSPITAL-1", "HR-1"], members=["viewer@x.com"], actor="root@x.com")

        patches.ingest_inventory(db_path, "HOSPITAL-1", [report("kb100")], now=1000)
        patches.ingest_inventory(db_path, "HR-1", [report("kb200")], now=1000)
        patches.ingest_inventory(db_path, "FINANCE-1", [report("kb300")], now=1000)

        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(create_patches_blueprint(
            db_path, fake_login_required, create_access(db_path, {"root@x.com"})))

        @app.before_request
        def _seed_session():
            from flask import session
            session["user"] = {"email": CURRENT_USER}

        c = app.test_client()

        # ------------------------------------------------------------ reads
        print("\n== Reading is 'view', and it is scoped ==")
        CURRENT_USER = "root@x.com"
        doc = c.get("/api/patches").get_json()
        check("a superuser sees every update", len(doc["updates"]) == 3)
        check("the vocabulary is served, not hardcoded in the page",
              [k["name"] for k in doc["vocabulary"]["classifications"]]
              == list(patches.CLASSIFICATIONS))
        check("classification labels are resolved, not keys",
              doc["vocabulary"]["classifications"][0]["label"] != "security")
        check("a superuser is told they may manage", doc["can_manage"] is True)

        CURRENT_USER = "viewer@x.com"
        doc = c.get("/api/patches").get_json()
        # The failure this catches: `if not machines` collapsing "a scope" into "no scope",
        # so an operator entitled to two machines is shown the whole estate.
        check("a scoped viewer sees only their machines' updates",
              sorted(u["uid"] for u in doc["updates"]) == ["kb100", "kb200"])
        check("and the summary is scoped with it",
              doc["summary"]["machines_with_updates"] == 2)
        check("a viewer is told they may NOT manage", doc["can_manage"] is False)

        check("a viewer may read one of their machines",
              c.get("/api/patches/machine/HOSPITAL-1").status_code == 200)
        check("a viewer may not read a machine outside their scope",
              c.get("/api/patches/machine/FINANCE-1").status_code == 403)

        # ------------------------------------------------------- writes are gated
        print("\n== Writing is 'manage_patches' ==")
        r = c.post("/api/patches/approvals", json={"uid": "kb100",
                                                   "decision": "approved"})
        check("a viewer may not approve an update", r.status_code == 403)
        check("a viewer may not open a maintenance window",
              c.post("/api/patches/windows", json={
                  "name": "Nope", "days_mask": 1, "start_minute": 60,
                  "duration_minutes": 60, "scope_kind": "all"}).status_code == 403)
        check("a viewer may not start a run",
              c.post("/api/patches/runs",
                     json={"machines": ["HOSPITAL-1"]}).status_code == 403)

        CURRENT_USER = "hospital@x.com"
        r = c.post("/api/patches/approvals", json={"uid": "KB100",
                                                   "decision": "approved"})
        check("a manager may approve", r.status_code == 200)
        check("the approval is stored case-folded", r.get_json()["uid"] == "kb100")
        # Approvals are NOT machine-scoped -- an approval names an update, not a machine.
        # What it can reach is bounded at run creation, which the next block asserts.
        check("a manager may approve an update only other people's machines report",
              c.post("/api/patches/approvals",
                     json={"uid": "kb300", "decision": "declined"}).status_code == 200)
        check("approving is audited", "set_patch_approval" in audit_actions(db_path))

        check("an unusable update id is refused, not stored",
              c.post("/api/patches/approvals",
                     json={"uid": "not a uid", "decision": "approved"}).status_code == 400)
        check("an unknown decision is refused",
              c.post("/api/patches/approvals",
                     json={"uid": "kb100", "decision": "maybe"}).status_code == 400)

        # ------------------------------------------------------- runs are all-or-nothing
        print("\n== A run is all-or-nothing across its targets ==")
        r = c.post("/api/patches/runs", json={"machines": ["HOSPITAL-1", "FINANCE-1"]})
        # The failure this catches is the quiet one: dropping the out-of-scope machine and
        # starting on the rest, so somebody believes both were patched.
        check("a run naming one out-of-scope machine is refused entirely",
              r.status_code == 403)
        check("and nothing was created", not patches.list_runs(db_path))

        r = c.post("/api/patches/runs", json={"machines": []})
        check("a run naming no machine is a 400, not a 403", r.status_code == 400)

        r = c.post("/api/patches/runs", json={"machines": ["HOSPITAL-1"],
                                              "note": "September"})
        check("a run inside scope is created", r.status_code == 201)
        run_id = r.get_json()["run"]["id"]
        check("it starts scheduled",
              r.get_json()["run"]["status"] == patches.RUN_SCHEDULED)
        check("creating a run is audited at security level",
              "create_patch_run" in audit_actions(db_path))
        # Read from the map directly: ACTION_LEVELS is what fleet falls back to when a
        # caller omits `level`, and an action missing from it silently becomes security by
        # DEFAULT_AUDIT_LEVEL -- which would look identical here while leaving the row
        # unclassified. Asserting the entry exists is the point.
        check("a run is security-level in the action map",
              fleet.ACTION_LEVELS.get("create_patch_run") == fleet.LEVEL_SECURITY)
        check("an approval is notice-level in the action map",
              fleet.ACTION_LEVELS.get("set_patch_approval") == fleet.LEVEL_NOTICE)

        # ------------------------------------------------------------ windows
        print("\n== Maintenance windows ==")
        # Machine-scoped, because this operator is: a window covering `all` is a fleet-wide
        # write and is refused for them further down. That is the gap this used to have.
        r = c.post("/api/patches/windows", json={
            "name": "Sunday night", "days_mask": 1 << 6, "start_minute": 23 * 60,
            "duration_minutes": 240, "scope_kind": "machines",
            "machines": ["HOSPITAL-1"],
            "reboot_policy": patches.REBOOT_IF_REQUIRED})
        check("a manager may open a window over their own machines", r.status_code == 201)
        window_id = r.get_json()["window"]["id"]
        check("a zero-day window is refused", c.post("/api/patches/windows", json={
            "name": "Never", "days_mask": 0, "start_minute": 0, "duration_minutes": 60,
            "scope_kind": "machines", "machines": ["HOSPITAL-1"]}).status_code == 400)
        check("a duplicate window name is refused", c.post("/api/patches/windows", json={
            "name": "sunday night", "days_mask": 1, "start_minute": 0,
            "duration_minutes": 60, "scope_kind": "machines",
            "machines": ["HOSPITAL-1"]}).status_code == 400)
        check("a window can be edited",
              c.put(f"/api/patches/windows/{window_id}",
                    json={"duration_minutes": 120}).status_code == 200)
        check("the edit stuck",
              patches.get_window(db_path, window_id)["duration_minutes"] == 120)
        check("editing a window is audited",
              "update_maintenance_window" in audit_actions(db_path))
        check("deleting an unknown window is a 404",
              c.delete("/api/patches/windows/nope").status_code == 404)
        check("a window can be deleted",
              c.delete(f"/api/patches/windows/{window_id}").status_code == 200)

        # ------------------------------------------------------------ run controls
        print("\n== Cancel and retry ==")
        check("cancelling an unknown run is a 404",
              c.post("/api/patches/runs/nope/cancel").status_code == 404)
        r = c.post(f"/api/patches/runs/{run_id}/cancel")
        check("a run can be cancelled", r.status_code == 200)
        # "cancelled" and "cancelled 8 of 10" are different facts; the response says which.
        check("the response says how many targets were actually recalled",
              r.get_json()["recalled"] == 1)
        check("cancelling is audited", "cancel_patch_run" in audit_actions(db_path))
        check("retrying an unknown run is a 404",
              c.post("/api/patches/runs/nope/retry").status_code == 404)
        check("a cancelled run's targets can be re-armed",
              c.post(f"/api/patches/runs/{run_id}/retry").get_json()["rearmed"] == 1)

        # ------------------------------------------------------------ run reads
        print("\n== Run reads are narrowed, not refused ==")
        CURRENT_USER = "root@x.com"
        r = c.post("/api/patches/runs",
                   json={"machines": ["HOSPITAL-1", "FINANCE-1"]})
        mixed_id = r.get_json()["run"]["id"]
        CURRENT_USER = "viewer@x.com"
        doc = c.get(f"/api/patches/runs/{mixed_id}").get_json()
        # Narrowed rather than 403'd: an operator entitled to most of a run should see the
        # part that concerns them, not a locked door.
        check("a scoped viewer sees a run's in-scope targets only",
              [t["machine"] for t in doc["run"]["targets"]] == ["HOSPITAL-1"])
        check("a viewer may still read the run list",
              c.get("/api/patches/runs").status_code == 200)
        check("reading an unknown run is a 404",
              c.get("/api/patches/runs/nope").status_code == 404)

        # -------------------------------------------------- cross-scope writes
        print("\n== A scoped operator cannot reach outside their scope ==")
        # A run the scoped operator could never have created: it targets FINANCE-1, which is
        # not in their group. They can discover it exists (see the narrowed list below) but
        # must not be able to act on it.
        CURRENT_USER = "root@x.com"
        wide = c.post("/api/patches/runs",
                      json={"machines": ["HOSPITAL-1", "FINANCE-1"]}).get_json()["run"]["id"]

        CURRENT_USER = "hospital@x.com"
        # The failure this catches: enumerate run ids fleet-wide, then cancel or force a
        # reboot-retry on a machine you cannot see anywhere else in the product.
        check("a scoped operator cannot cancel a run reaching outside their scope",
              c.post(f"/api/patches/runs/{wide}/cancel").status_code == 403)
        check("...nor retry it",
              c.post(f"/api/patches/runs/{wide}/retry").status_code == 403)
        check("the run really was left alone",
              patches.get_run(db_path, wide)["status"] != patches.RUN_CANCELLED)
        check("a run wholly inside their scope is still theirs to cancel",
              c.post(f"/api/patches/runs/{run_id}/cancel").status_code == 200)
        check("cancelling an unknown run is still a 404, not a 403",
              c.post("/api/patches/runs/nope/cancel").status_code == 404)

        # The list is the enumeration surface the above depends on, and the module docstring
        # promises reads are narrowed.
        listed = [r["id"] for r in c.get("/api/patches/runs").get_json()["runs"]]
        check("a scoped operator's run list omits runs that miss their machines entirely",
              all(patches.get_run(db_path, r)["targets"] for r in listed))
        CURRENT_USER = "root@x.com"
        finance_only = c.post("/api/patches/runs",
                              json={"machines": ["FINANCE-1"]}).get_json()["run"]["id"]
        CURRENT_USER = "hospital@x.com"
        check("a run touching none of their machines is not listed at all",
              finance_only not in
              [r["id"] for r in c.get("/api/patches/runs").get_json()["runs"]])
        check("a run touching one of their machines still is",
              wide in [r["id"] for r in c.get("/api/patches/runs").get_json()["runs"]])

        print("\n== Reading windows is narrowed too ==")
        CURRENT_USER = "root@x.com"
        c.post("/api/patches/windows", json={
            "name": "Hospital only", "days_mask": 1, "start_minute": 300,
            "duration_minutes": 60, "scope_kind": "machines",
            "machines": ["HOSPITAL-1"]})
        c.post("/api/patches/windows", json={
            "name": "Finance only", "days_mask": 1, "start_minute": 360,
            "duration_minutes": 60, "scope_kind": "machines",
            "machines": ["FINANCE-1"]})
        c.post("/api/patches/windows", json={
            "name": "Mixed", "days_mask": 1, "start_minute": 420,
            "duration_minutes": 60, "scope_kind": "machines",
            "machines": ["HOSPITAL-1", "FINANCE-1", "HR-1"]})
        c.post("/api/patches/windows", json={
            "name": "Whole fleet", "days_mask": 1, "start_minute": 480,
            "duration_minutes": 60, "scope_kind": "all"})

        CURRENT_USER = "hospital@x.com"
        seen = {w["name"]: w for w in c.get("/api/patches/windows").get_json()["windows"]}
        check("a window naming only their machine is visible", "Hospital only" in seen)
        check("a window naming none of their machines is hidden", "Finance only" not in seen)
        # The leak that is easy to miss: the window IS theirs to see, but the row would
        # otherwise carry two hostnames they have no grant over.
        check("a mixed window is visible", "Mixed" in seen)
        check("...with the other machines' hostnames stripped out",
              seen["Mixed"]["machines"] == ["HOSPITAL-1"])
        # An all-scope window governs their machines' patching and names no hostnames, so
        # there is nothing to leak and hiding it would misinform them about when they patch.
        check("a fleet-wide window stays visible", "Whole fleet" in seen)
        check("...and still names nobody", seen["Whole fleet"]["machines"] == [])

        CURRENT_USER = "root@x.com"
        allw = {w["name"]: w for w in c.get("/api/patches/windows").get_json()["windows"]}
        check("an unrestricted operator sees every window", "Finance only" in allw)
        check("...with every hostname intact",
              allw["Mixed"]["machines"] == ["HOSPITAL-1", "FINANCE-1", "HR-1"])
        CURRENT_USER = "hospital@x.com"

        print("\n== Maintenance windows are a fleet-wide write ==")
        # A window decides when patches install and when machines reboot. A scoped operator
        # creating one that covers `all` would be reaching every machine in the estate.
        check("a scoped operator cannot open a window covering every machine",
              c.post("/api/patches/windows", json={
                  "name": "Everything", "days_mask": 1, "start_minute": 60,
                  "duration_minutes": 60, "scope_kind": "all"}).status_code == 403)
        check("...nor one naming a machine outside their scope",
              c.post("/api/patches/windows", json={
                  "name": "Sneaky", "days_mask": 1, "start_minute": 60,
                  "duration_minutes": 60, "scope_kind": "machines",
                  "machines": ["HOSPITAL-1", "FINANCE-1"]}).status_code == 403)
        mine = c.post("/api/patches/windows", json={
            "name": "Mine", "days_mask": 1, "start_minute": 60, "duration_minutes": 60,
            "scope_kind": "machines", "machines": ["HOSPITAL-1"]})
        check("a window naming only their own machines is allowed", mine.status_code == 201)
        mine_id = mine.get_json()["window"]["id"]
        check("...and they may edit it",
              c.put(f"/api/patches/windows/{mine_id}",
                    json={"duration_minutes": 90}).status_code == 200)
        check("but not widen it to the whole fleet",
              c.put(f"/api/patches/windows/{mine_id}",
                    json={"scope_kind": "all", "machines": []}).status_code == 403)

        CURRENT_USER = "root@x.com"
        fleet_window = c.post("/api/patches/windows", json={
            "name": "Fleet wide", "days_mask": 1, "start_minute": 120,
            "duration_minutes": 60, "scope_kind": "all"}).get_json()["window"]["id"]
        CURRENT_USER = "hospital@x.com"
        # The failure this catches: narrowing somebody else's fleet-wide window down to your
        # own machines -- an edit you could not have made from scratch, which would silently
        # stop every other machine patching.
        check("a scoped operator cannot narrow a fleet-wide window to their own machines",
              c.put(f"/api/patches/windows/{fleet_window}",
                    json={"scope_kind": "machines",
                          "machines": ["HOSPITAL-1"]}).status_code == 403)
        check("...nor delete it",
              c.delete(f"/api/patches/windows/{fleet_window}").status_code == 403)
        check("the fleet-wide window survived",
              patches.get_window(db_path, fleet_window) is not None)
        CURRENT_USER = "root@x.com"
        check("an unrestricted operator may still delete it",
              c.delete(f"/api/patches/windows/{fleet_window}").status_code == 200)

        # ------------------------------------------------------------ CSRF shape
        print("\n== CSRF shape ==")
        # Every write reads its body with get_json(silent=True), so a cross-site form post
        # (which cannot set Content-Type: application/json) arrives as an empty body and
        # is refused on validation rather than acted on. Same contract as fleet_web.
        CURRENT_USER = "hospital@x.com"
        r = c.post("/api/patches/approvals", data="uid=kb100&decision=approved",
                   content_type="application/x-www-form-urlencoded")
        check("a form-encoded approval is not honoured", r.status_code == 400)
        r = c.post("/api/patches/runs", data="machines=HOSPITAL-1",
                   content_type="application/x-www-form-urlencoded")
        check("a form-encoded run is not honoured", r.status_code == 400)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
