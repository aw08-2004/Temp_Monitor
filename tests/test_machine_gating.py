"""Pins the machine page's capability gating -- roadmap #23 phase F.2.

The console hides a tool a machine has reported it cannot answer. Three files are wired
together by nothing but strings, and every way that wiring can break is silent:

  1. **`data-needs-command` names a command type that does not exist.** A typo hides nothing,
     ever, on every machine, with no error anywhere -- the attribute simply never matches a
     reported list. Asserted against `fleet.ALL_COMMANDS`, which is the same catalog the
     agent's capability report is checked against.
  2. **The shared report loads after the modules that read it.** `machine-capabilities.js`
     holds the one request three cards share; a module whose <script> comes first sees no
     global and returns early, so its card is simply absent. On a Windows PC that looks
     identical to correct behaviour, which is why it would survive a review.
  3. **The two questions get swapped.** `can()` treats an unreported machine as capable and
     `claims()` treats it as not, and which one a caller wants depends on whether the feature
     predates capability reporting. Getting it backwards either hides the Terminal button on
     every Windows PC in the fleet, or puts a Wipe button on all of them. Both are one word.

Like test_sidebar_collapse.py, this asserts the joins rather than the behaviour -- there is no
browser here. What it can catch cheaply is a three-file feature going quietly dead.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "hub")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(*parts):
    with open(os.path.join(HUB, *parts), encoding="utf-8") as handle:
        return handle.read()


def main():
    template = read("templates", "machine.html")
    gate = read("static", "js", "machine-tools-gate.js")
    shared = read("static", "js", "machine-capabilities.js")
    secure = read("static", "js", "machine-secure.js")
    usage = read("static", "js", "machine-usage.js")

    print("== every gated element names a real command type ==")
    needed = re.findall(r'data-needs-command="([^"]+)"', template)
    check("the machine page gates something at all", len(needed) > 0)
    for command_type in needed:
        check(f"{command_type} is a command type this hub knows",
              command_type in fleet.ALL_COMMANDS)

    print("\n== the gate reads the attribute the template writes ==")
    check("the gate module queries by that attribute",
          "[data-needs-command]" in gate)
    check("...and reads it through the dataset name the DOM gives it",
          "needsCommand" in gate)

    print("\n== the shared report loads before anything that reads it ==")
    def position(name):
        match = re.search(r"js/%s" % re.escape(name), template)
        return match.start() if match else -1

    order = position("machine-capabilities.js")
    check("machine-capabilities.js is on the page", order >= 0)
    for name in ("machine-tools-gate.js", "machine-secure.js", "machine-usage.js"):
        where = position(name)
        check(f"{name} is on the page", where >= 0)
        # The failure this catches is invisible: a module loaded first finds no global, returns
        # early, and its card is simply absent -- which on a Windows PC is also what correct
        # behaviour looks like.
        check(f"...and loads after the shared report", where > order >= 0)

    print("\n== the two questions answer silence differently ==")
    # Asserted as text because there is no JavaScript engine here. What is being pinned is that
    # the two functions exist and disagree about an unreported machine -- one returns true when
    # the list is not an array, the other requires the list to BE an array.
    check("can() treats a machine that has not reported as capable",
          re.search(r"can\(type\)[\s\S]{0,200}!Array\.isArray", shared) is not None)
    check("claims() requires an explicit report",
          re.search(r"claims\(type\)[\s\S]{0,200}Array\.isArray\(list\) &&", shared) is not None)

    print("\n== each caller asks the question its feature needs ==")
    # A tool link predates capability reporting, so silence must keep it: `can`.
    check("the tools gate asks can()", "MachineCapabilities.can(" in gate)
    # Lock and wipe are new, so silence must withhold them: `claims`.
    check("the lock and wipe card asks claims()",
          "MachineCapabilities.claims('lock_device')" in secure)
    check("...and never can(), which would put a Wipe button on every Windows PC",
          "MachineCapabilities.can(" not in secure)
    # Usage access is a feature rather than a command, and its absence is the thing the card
    # exists to say out loud.
    check("the usage card asks after the usage_access feature",
          "hasFeature('usage_access')" in usage)

    print("\n== nothing asks the machine detail endpoint twice ==")
    for name, source in (("machine-secure.js", secure), ("machine-usage.js", usage)):
        check(f"{name} reads the shared report rather than fetching its own",
              "/api/machines/" not in source)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
