"""policy.py -- which apps a managed device may run (roadmap #23 phase D).

**The silent failure this file exists to catch is a device that stays blocked.** Everything
about the resolver is arranged so that removing a machine from a policy, disabling a policy, or
deleting one outright actually LIFTS what it applied -- and the way that fails is not an error.
It is a phone whose camera does not open, on a machine page showing no policy at all, because
the hub decided there was nothing to say and sent nothing. So the assertions here are mostly
about the empty document: a machine no policy covers resolves to `blocked: []`, not to an
absent answer, and that distinction is the release valve for the whole feature.

Three more things are held here because each is a decision that reads as arbitrary until it
bites:

  * **Allowlists INTERSECT, blocklists union.** Each allowlist says "only these may run", so two
    of them both applying leaves what both permit. A union would let a second allowlist quietly
    widen the first, which is the opposite of what an allowlist is for.
  * **An allowlist with no inventory yields NOTHING, not everything.** Getting that backwards
    would suspend every app on a device the hub has not finished learning about -- which is
    exactly the freshly-enrolled device, and the single most destructive thing this feature
    could do.
  * **The protected set wins over every policy**, including an allowlist that simply did not
    mention the launcher. The agent enforces its own copy independently; this one exists so the
    console never shows an operator a policy it already knows will be partly refused.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import policy

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


def installed(*packages):
    return [{"package": p, "label": p.split(".")[-1], "suspended": False} for p in packages]


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        policy.init_policy_db(db_path)
        policy.init_policy_db(db_path)      # idempotent: called on every hub start

        print("== The protected set ==")
        for package in ("net.arkeanos.fleethub.agent", "com.android.settings",
                        "com.sec.android.app.launcher", "com.android.dialer",
                        "com.android.systemui", "com.android.emergency"):
            check(f"{package} can never be blocked", policy.is_protected(package))
        check("an ordinary app is not protected",
              not policy.is_protected("com.zhiliaoapp.musically"))
        check("...and neither is an empty name", not policy.is_protected(""))

        print("\n== Authoring ==")
        try:
            policy.create_policy(db_path, name="", mode="block", packages=["com.x"],
                                 fleet_wide=True)
            check("a policy with no name is refused", False)
        except policy.PolicyRejected:
            check("a policy with no name is refused", True)
        for mode, why in (("sideways", "an unknown mode"), ("", "no mode")):
            try:
                policy.create_policy(db_path, name="n", mode=mode, packages=["com.x"],
                                     fleet_wide=True)
                check(f"a policy with {why} is refused", False)
            except policy.PolicyRejected:
                check(f"a policy with {why} is refused", True)
        try:
            policy.create_policy(db_path, name="n", mode="block", packages=[],
                                 fleet_wide=True)
            check("a BLOCK policy with no packages is refused", False)
        except policy.PolicyRejected as e:
            check("a BLOCK policy with no packages is refused", True)
            check("...because it would do nothing, which is what the message says",
                  "do nothing" in str(e))
        # ...while an allowlist with none is a real, if drastic, statement.
        allow_nothing = policy.create_policy(db_path, name="lockdown", mode="allow",
                                             packages=[], fleet_wide=True, enabled=False)
        check("an ALLOW policy with no packages is a real statement and is stored",
              policy.get_policy(db_path, allow_nothing) is not None)
        try:
            policy.create_policy(db_path, name="n", mode="block", packages=["com.x"],
                                 machines=[], fleet_wide=False)
            check("a policy that is neither fleet-wide nor targeted is refused", False)
        except policy.PolicyRejected:
            check("a policy that is neither fleet-wide nor targeted is refused", True)

        parts = policy.validate("n", "block", ["com.x", "com.android.settings", "com.x"],
                                ["PHONE-1"], False)
        check("a protected package is dropped rather than refusing the whole policy",
              parts["packages"] == ["com.x"])
        check("...and the caller is told which, so nothing is dropped silently",
              parts["dropped"] == ["com.android.settings"])

        print("\n== Resolution: blocklists union ==")
        policy.delete_policy(db_path, allow_nothing)
        # The two policies overlap on com.tiktok on purpose, so the union is also a dedup
        # test; com.targeted.only is named by the TARGETED policy alone, which is what makes
        # the PHONE-2 assertion below mean something.
        first = policy.create_policy(db_path, name="social", mode="block",
                                     packages=["com.tiktok", "com.targeted.only"],
                                     machines=["PHONE-1"])
        second = policy.create_policy(db_path, name="games", mode="block",
                                      packages=["com.game", "com.tiktok"], fleet_wide=True)
        document = policy.resolve_for(db_path, "PHONE-1")
        check("both policies contribute",
              document["blocked"] == ["com.game", "com.targeted.only", "com.tiktok"])
        check("...and the same app named twice is one entry",
              document["blocked"].count("com.tiktok") == 1)
        check("the document says which policies produced it",
              {p["id"] for p in document["policies"]} == {first, second})

        check("a machine the targeted policy does not name gets only the fleet-wide one",
              policy.resolve_for(db_path, "PHONE-2")["blocked"] == ["com.game", "com.tiktok"])

        print("\n== The empty document is the release valve ==")
        policy.delete_policy(db_path, second)
        policy.update_policy(db_path, first, name="social", mode="block",
                             packages=["com.tiktok"], machines=["PHONE-9"], fleet_wide=False)
        empty = policy.resolve_for(db_path, "PHONE-1")
        check("a machine no policy covers resolves to an EMPTY list, not to nothing",
              empty["blocked"] == [])
        check("...and still has a version, so the agent can tell it apart from silence",
              bool(empty["version"]))
        check("a disabled policy stops applying",
              policy.update_policy(db_path, first, name="social", mode="block",
                                   packages=["com.tiktok"], machines=["PHONE-9"],
                                   fleet_wide=False, enabled=False)
              and policy.resolve_for(db_path, "PHONE-9")["blocked"] == [])

        print("\n== Allowlists ==")
        for policy_id in [p["id"] for p in policy.list_policies(db_path)]:
            policy.delete_policy(db_path, policy_id)
        device = installed("com.keep", "com.drop", "com.other", "com.android.settings")
        allow = policy.create_policy(db_path, name="kiosk", mode="allow",
                                     packages=["com.keep"], machines=["PHONE-1"])
        resolved = policy.resolve_for(db_path, "PHONE-1", device)
        check("everything not allowed is blocked",
              resolved["blocked"] == ["com.drop", "com.other"])
        check("...and the protected app survives even though the allowlist omitted it",
              "com.android.settings" not in resolved["blocked"])

        # The assertion that keeps a freshly enrolled device usable.
        check("an allowlist with NO inventory blocks nothing, rather than everything",
              policy.resolve_for(db_path, "PHONE-1")["blocked"] == [])

        second_allow = policy.create_policy(db_path, name="narrower", mode="allow",
                                            packages=["com.keep", "com.other"],
                                            machines=["PHONE-1"])
        intersected = policy.resolve_for(db_path, "PHONE-1", device)
        check("two allowlists INTERSECT rather than union",
              intersected["blocked"] == ["com.drop", "com.other"])

        policy.update_policy(db_path, second_allow, name="narrower", mode="allow",
                             packages=["com.other"], machines=["PHONE-1"], fleet_wide=False)
        both = policy.resolve_for(db_path, "PHONE-1", device)
        check("...so two allowlists with nothing in common block everything unprotected",
              both["blocked"] == ["com.drop", "com.keep", "com.other"])

        policy.delete_policy(db_path, second_allow)
        block_too = policy.create_policy(db_path, name="also blocked", mode="block",
                                         packages=["com.keep"], machines=["PHONE-1"])
        mixed = policy.resolve_for(db_path, "PHONE-1", device)
        check("a blocklist still bites on an app an allowlist permitted",
              "com.keep" in mixed["blocked"])
        policy.delete_policy(db_path, block_too)

        print("\n== The version ==")
        check("the same content produces the same version",
              policy.document_version(["a", "b"]) == policy.document_version(["b", "a"]))
        check("...and different content does not",
              policy.document_version(["a"]) != policy.document_version(["a", "b"]))
        check("an empty document has a version of its own",
              policy.document_version([]) != policy.document_version(["a"]))

        print("\n== What the device said it did ==")
        check("a state report is stored",
              policy.record_state(db_path, "PHONE-1", {
                  "version": "abc", "applied_at": 1000,
                  "failed": ["com.drop"], "error": ""}, now=1100))
        state = policy.get_state(db_path, "PHONE-1")
        check("...with the packages it could NOT suspend",
              state["failed"] == ["com.drop"] and state["version"] == "abc")
        check("a later report replaces it",
              policy.record_state(db_path, "PHONE-1", {"version": "def", "failed": []},
                                  now=1200)
              and policy.get_state(db_path, "PHONE-1")["version"] == "def")
        for junk in (None, "x", [], {"failed": "com.x"}):
            try:
                policy.record_state(db_path, "PHONE-3", junk)
                ok = True
            except Exception as e:                     # noqa: BLE001
                ok = False
                print(f"       raised on {junk!r}: {e}")
            check(f"a malformed state report is survivable: {junk!r}", ok)
        check("a device that never reported has no state",
              policy.get_state(db_path, "PHONE-4") is None)

        print("\n== Compliance: intent vs claim vs inventory ==")
        suspended = [dict(a, suspended=a["package"] == "com.drop") for a in device]
        report = policy.compliance(db_path, "PHONE-1", suspended)
        check("it names what the policy asks for", "com.drop" in report["blocked"])
        check("...counts only what is actually INSTALLED as enforceable",
              report["counts"]["installed"] == len(
                  [p for p in report["blocked"] if p in {a['package'] for a in suspended}]))
        check("...and counts what the device's own inventory says is suspended",
              report["counts"]["enforced"] == 1)
        check("a version mismatch reads as 'not caught up yet', not as an error",
              report["current"] is False)
        policy.record_state(db_path, "PHONE-1", {"version": report["version"], "failed": []},
                            now=1300)
        check("...and matches once the device reports the current version",
              policy.compliance(db_path, "PHONE-1", suspended)["current"] is True)

        print("\n== Lifecycle ==")
        policy.create_policy(db_path, name="targeted", mode="block", packages=["com.x"],
                             machines=["GONE-1", "STAYS-1"])
        policy.forget_machine(db_path, "GONE-1")
        check("a deleted machine is dropped from every policy",
              policy.resolve_for(db_path, "GONE-1")["blocked"] == [])
        check("...and the other target is untouched",
              policy.resolve_for(db_path, "STAYS-1")["blocked"] == ["com.x"])
        policy.rename_machine(db_path, "STAYS-1", "MERGED-1")
        check("a merge carries the policy to the survivor",
              policy.resolve_for(db_path, "MERGED-1")["blocked"] == ["com.x"])
        check("...and the merged-away name is no longer covered",
              policy.resolve_for(db_path, "STAYS-1")["blocked"] == [])

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
