"""apps.py -- the installed-app inventory (roadmap #23 phase D).

**The silent failure this file exists to catch is an emptied inventory.** The app set is
REPLACED on every report, because an app that has been uninstalled must disappear -- a stale row
is a policy target that no longer exists and a compliance answer that is quietly wrong. Replace
semantics plus a lenient parse is how one malformed heartbeat wipes a good inventory: a payload
whose `apps` is not a list at all (a truncated body, a future agent sending a different shape)
would be read as "this device has nothing installed", the table would be emptied, and the only
symptom would be a policy editor with nothing to pick from. That is exactly the mistake
wake.record_network was written to avoid, and this file holds the same line in both directions:
an EMPTY list is a real claim and is stored; an absent or non-list one leaves the last good
reading alone.

The second thing asserted is that `suspended` and `enabled` are what the DEVICE said. They are
stored so an operator can see a policy that was applied to fourteen apps and took on eleven --
`setPackagesSuspended` returns the packages it could not suspend, and a policy reported as
applied while three targets are still running is worse than none. A table written from intent
rather than from the device's report could never show that.

The third is `reported_at: None`, which is every Windows PC and every Android device on an older
agent. It must stay distinct from a device that reported having no apps -- the first means "we
have not been told" and the second does not happen.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import apps

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


def app(package, **overrides):
    payload = {"package": package, "label": package.split(".")[-1].title(),
               "version": "1.0", "system": False, "enabled": True, "suspended": False}
    payload.update(overrides)
    return payload


def report(*entries):
    return {"apps": list(entries)}


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        apps.init_apps_db(db_path)
        apps.init_apps_db(db_path)      # idempotent: called on every hub start

        print("== A device that has never reported ==")
        empty = apps.get_inventory(db_path, "PHONE-1")
        check("reads as 'we have not been told', not as 'no apps'",
              empty["reported_at"] is None and empty["apps"] == [])
        check("...and its counts are all zero rather than missing",
              empty["counts"] == {"total": 0, "user": 0, "suspended": 0, "disabled": 0})

        print("\n== Storing a report ==")
        stored = apps.record_inventory(
            db_path, "PHONE-1",
            report(app("com.zhiliaoapp.musically", label="TikTok"),
                   app("com.android.chrome", label="Chrome", system=True),
                   app("com.example.old", label="Old", enabled=False),
                   app("com.example.paused", label="Paused", suspended=True)),
            now=1000)
        check("every usable package is stored", stored == 4)
        inventory = apps.get_inventory(db_path, "PHONE-1")
        check("...and reported_at comes back", inventory["reported_at"] == 1000)
        check("the counts distinguish user apps from the rest",
              inventory["counts"] == {"total": 4, "user": 3, "suspended": 1, "disabled": 1})
        by_package = {a["package"]: a for a in inventory["apps"]}
        check("the label a person recognises is kept",
              by_package["com.zhiliaoapp.musically"]["label"] == "TikTok")
        check("...and the flags come back as booleans, not 0/1 the console has to guess at",
              by_package["com.example.paused"]["suspended"] is True
              and by_package["com.example.old"]["enabled"] is False
              and by_package["com.android.chrome"]["is_system"] is True)

        print("\n== Ordering: what a policy author reads first ==")
        order = [a["package"] for a in inventory["apps"]]
        check("user apps come before system ones",
              order.index("com.zhiliaoapp.musically") < order.index("com.android.chrome"))
        check("...and within each group, alphabetically by label",
              order[:3] == ["com.example.old", "com.example.paused",
                            "com.zhiliaoapp.musically"])
        check("system apps can be excluded outright",
              [a["package"] for a in apps.list_apps(db_path, "PHONE-1", include_system=False)]
              == ["com.example.old", "com.example.paused", "com.zhiliaoapp.musically"])

        print("\n== Replace semantics ==")
        apps.record_inventory(db_path, "PHONE-1",
                              report(app("com.zhiliaoapp.musically", label="TikTok")),
                              now=2000)
        check("an uninstalled app disappears",
              [a["package"] for a in apps.list_apps(db_path, "PHONE-1")]
              == ["com.zhiliaoapp.musically"])
        check("an EMPTY list is a real claim and is honoured",
              apps.record_inventory(db_path, "PHONE-1", {"apps": []}, now=3000) == 0
              and apps.list_apps(db_path, "PHONE-1") == [])

        print("\n== A malformed report replaces NOTHING ==")
        # The assertion the whole file is about. Every one of these would otherwise empty a
        # good inventory in a single heartbeat, and the only symptom would be a policy editor
        # with nothing in it.
        apps.record_inventory(db_path, "PHONE-2", report(app("com.example.keep")), now=4000)
        for junk, why in ((None, "not a dict"), ({}, "no apps key"),
                          ({"apps": None}, "apps is null"),
                          ({"apps": "com.example.keep"}, "apps is a string"),
                          ({"apps": {"a": 1}}, "apps is an object"),
                          ({"apps": [1, 2, 3]}, "nothing in the list is usable"),
                          ({"apps": [{"label": "no package"}]}, "no package name")):
            try:
                apps.record_inventory(db_path, "PHONE-2", junk, now=5000)
                survived = [a["package"] for a in apps.list_apps(db_path, "PHONE-2")]
            except Exception as e:                     # noqa: BLE001
                survived = [f"raised: {e}"]
            check(f"a report where {why} leaves the last good one alone",
                  survived == ["com.example.keep"])
        check("...and a machine with no name stores nothing",
              apps.record_inventory(db_path, "", report(app("com.x"))) == 0)

        print("\n== Bounds ==")
        flood = apps.record_inventory(
            db_path, "PHONE-3",
            report(*[app(f"com.example.a{i}") for i in range(apps.MAX_APPS + 50)]), now=6000)
        check("a flood of packages is capped", flood == apps.MAX_APPS)
        apps.record_inventory(db_path, "PHONE-4",
                              report(app("com.example.long", label="L" * 5000)), now=6100)
        check("a very long label is trimmed rather than stored whole",
              len(apps.list_apps(db_path, "PHONE-4")[0]["label"]) == apps.MAX_LABEL_CHARS)
        apps.record_inventory(db_path, "PHONE-5",
                              report(app("com.example.dup"), app("com.example.dup")), now=6200)
        check("a package reported twice is stored once",
              len(apps.list_apps(db_path, "PHONE-5")) == 1)
        apps.record_inventory(db_path, "PHONE-6",
                              report({"package": "com.example.bare"}), now=6300)
        bare = apps.list_apps(db_path, "PHONE-6")[0]
        check("an app with no label falls back to its package rather than to a blank row",
              bare["label"] == "com.example.bare")
        check("...and an unstated `enabled` defaults to true, not to disabled",
              bare["enabled"] is True)

        print("\n== The fleet-wide questions a policy author asks ==")
        apps.record_inventory(db_path, "PHONE-7",
                              report(app("com.zhiliaoapp.musically", label="TikTok"),
                                     app("com.android.chrome", label="Chrome", system=True)),
                              now=7000)
        apps.record_inventory(db_path, "PHONE-8",
                              report(app("com.zhiliaoapp.musically", label="TikTok")),
                              now=7100)
        # PHONE-1 reported an empty list above, so it is correctly absent here -- which is
        # the replace-semantics assertion arriving a second time, from the other side.
        check("which machines have a package",
              apps.machines_with(db_path, "com.zhiliaoapp.musically")
              == ["PHONE-7", "PHONE-8"])
        known = {p["package"]: p for p in apps.known_packages(db_path)}
        check("the fleet package list counts how many devices have each",
              known["com.zhiliaoapp.musically"]["machines"] == 2)
        check("...and carries a label, so the editor is not a list of reverse-DNS",
              known["com.zhiliaoapp.musically"]["label"] == "TikTok")
        scoped = {p["package"] for p in apps.known_packages(db_path, ["PHONE-8"])}
        check("a scope filter narrows it", scoped == {"com.zhiliaoapp.musically"})
        check("...and an empty scope means nothing, not everything",
              apps.known_packages(db_path, []) == [])

        print("\n== Lifecycle ==")
        apps.forget_machine(db_path, "PHONE-8")
        check("a deleted machine's app list goes with it",
              apps.list_apps(db_path, "PHONE-8") == [])
        apps.record_inventory(db_path, "MERGE-OLD", report(app("com.example.gone")), now=8000)
        apps.record_inventory(db_path, "MERGE-NEW", report(app("com.example.here")), now=8100)
        apps.rename_machine(db_path, "MERGE-OLD", "MERGE-NEW")
        check("a merge keeps the SURVIVOR's own list, not a union of both",
              [a["package"] for a in apps.list_apps(db_path, "MERGE-NEW")]
              == ["com.example.here"])
        check("...and the merged-away name keeps nothing",
              apps.list_apps(db_path, "MERGE-OLD") == [])
        apps.record_inventory(db_path, "MERGE-ONLY", report(app("com.example.moves")),
                              now=8200)
        apps.rename_machine(db_path, "MERGE-ONLY", "MERGE-EMPTY")
        check("...but a survivor with no list of its own inherits one",
              [a["package"] for a in apps.list_apps(db_path, "MERGE-EMPTY")]
              == ["com.example.moves"])

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
