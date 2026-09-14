"""A deleted machine that reinstalls its agent and stays telemetry-only forever.

**The silent failure this file exists to catch is a 401 that does not say why.** An agent
holds one credential for the life of its install, in %ProgramData% where neither the
uninstaller nor a self-update touches it. So when an operator hard-deletes a machine
(fleet.delete_machine drops its agents row) the agent on that PC keeps presenting a token
for a row that no longer exists. It is refused, it has no way to tell "you were deleted"
from "you were revoked", and it therefore keeps the dead credential -- while the
unauthenticated /api/report ingress keeps working. The console shows the machine, with
temperatures, badged as not enrolled. Reinstalling with the right secret changes nothing,
because the agent reads agent.json, concludes it is already enrolled, and never asks.

Nothing is logged, nothing errors, and the hub looks correct from every angle. The only
thing standing between that and an operator losing an afternoon is the `reason` field these
checks assert on, so they assert on it from both ends: that a deleted agent is told
"unknown" (re-enroll), that a revoked one is told "revoked" (stay down), and that the
reason is never an oracle for which agent_ids exist.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import settings
from flask import Flask
from fleet_web import create_fleet_blueprint
from permissions_web import create_access

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


def _pass_through(view):
    return view


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        # The heartbeat route reaches into settings for the agent config version;
        # without this every 200 here would be a stack of "table missing" warnings.
        settings.init_settings_db(db_path)
        settings.invalidate()
        SECRET = "hub-enroll-secret"

        print("\n== fleet.auth_failure_reason ==")
        agent_id, token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)

        # The case the whole change is for: the operator deleted the machine, so the row is
        # gone and the agent must be told to enroll again.
        fleet.delete_machine(db_path, "PC-01")
        check("a deleted agent reads as unknown",
              fleet.auth_failure_reason(db_path, agent_id, token) == fleet.AUTH_UNKNOWN)

        # ...and the case that must NOT re-enroll, or revocation means nothing.
        revoked_id, revoked_token = fleet.enroll_agent(db_path, "PC-02", SECRET, SECRET)
        fleet.revoke_agent(db_path, revoked_id)
        check("a revoked agent reads as revoked",
              fleet.auth_failure_reason(db_path, revoked_id, revoked_token) == fleet.AUTH_REVOKED)
        check("a revoked agent still fails authentication",
              fleet.authenticate_agent(db_path, revoked_id, revoked_token) is None)

        # The reason must not become an existence oracle. An agent_id is the non-secret half
        # of the credential and appears in the audit trail, so "revoked" is only ever said to
        # a caller that also holds the token.
        check("a wrong token for a revoked agent reads as unknown, not revoked",
              fleet.auth_failure_reason(db_path, revoked_id, "not-the-token") == fleet.AUTH_UNKNOWN)
        check("an agent_id that never existed reads as unknown",
              fleet.auth_failure_reason(db_path, "0" * 32, "whatever") == fleet.AUTH_UNKNOWN)
        check("no credential at all reads as unknown",
              fleet.auth_failure_reason(db_path, None, None) == fleet.AUTH_UNKNOWN)

        print("\n== the 401 an agent actually receives ==")
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(create_fleet_blueprint(
            db_path, SECRET, _pass_through, create_access(db_path, {"operator@x.com"})))
        c = app.test_client()

        r = c.post("/api/agent/heartbeat",
                   headers={"Authorization": f"Bearer {agent_id}:{token}"})
        check("a deleted agent's heartbeat -> 401", r.status_code == 401)
        check("...carrying reason=unknown, which is what makes it re-enroll",
              r.get_json().get("reason") == fleet.AUTH_UNKNOWN)

        r = c.post("/api/agent/heartbeat",
                   headers={"Authorization": f"Bearer {revoked_id}:{revoked_token}"})
        check("a revoked agent's heartbeat -> 401", r.status_code == 401)
        check("...carrying reason=revoked, which is what keeps it down",
              r.get_json().get("reason") == fleet.AUTH_REVOKED)

        # A missing header must not blow up the reason lookup.
        r = c.post("/api/agent/heartbeat")
        check("no Authorization header -> 401 with reason=unknown",
              r.status_code == 401 and r.get_json().get("reason") == fleet.AUTH_UNKNOWN)

        # And the happy path is untouched: re-enrolling the deleted machine works, with a
        # brand-new identity rather than a resurrection of the old row.
        print("\n== re-enrollment after deletion ==")
        new_id, new_token = fleet.enroll_agent(db_path, "PC-01", SECRET, SECRET)
        check("re-enrolling issues a different agent_id", new_id != agent_id)
        check("the new identity authenticates",
              fleet.authenticate_agent(db_path, new_id, new_token) == "PC-01")
        check("the machine reads as enrolled again", fleet.is_enrolled(db_path, "PC-01"))
        check("heartbeat on the new identity -> 200",
              c.post("/api/agent/heartbeat",
                     headers={"Authorization": f"Bearer {new_id}:{new_token}"}).status_code == 200)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
