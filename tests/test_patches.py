"""Unit tests for patches.py -- the patch core, with no Flask involved.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
Under pytest, conftest.py wraps `check` so a false condition fails the test properly.

The emphasis is on this module's specific ways of being wrong SILENTLY, which are not the
same as packages':

  * **Claiming an install that did not happen.** The whole feature turns on a run reaching
    APPLIED only when the machine stops offering the update. A command result that closed a
    target would make every patch night green, so the REBOOTING hop is asserted directly:
    a successful command must NOT be a terminal state.

  * **An approval that fails to match its update.** Case folding is load-bearing -- Windows
    Update says `KB5060842`, an operator may type `kb5060842`, and an approval that missed
    would look exactly like the patch never being offered.

  * **A maintenance window that never opens, or never closes.** The midnight spill-over is
    the case that gets written wrong: a window starting at 23:00 for four hours is open at
    01:00 the next day, on a day its own mask does not name.

  * **Losing a machine.** Dispatched twice, stuck REBOOTING forever, or retried after the
    operator cancelled.

Times are driven explicitly through `now` rather than slept, so window and backoff
behaviour is asserted at exact moments. The window tests are the exception -- they must go
through `time.localtime`, so they build their own epochs from a known local date.
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import patches
import settings

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


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def report(*specs):
    """Build an agent-shaped available-updates list. Each spec is (uid, classification)."""
    out = []
    for spec in specs:
        uid, classification = spec[0], spec[1]
        out.append({
            "uid": uid,
            "source": patches.SOURCE_WINDOWS_UPDATE,
            "kb": uid if uid.upper().startswith("KB") else "",
            "title": f"Update {uid}",
            "classification": classification,
            "reboot_required": True,
        })
    return out


def answer(db_path, machine, ok=True, output="done"):
    """Answer whatever command is queued for a machine, the way an agent would.

    Claim first: complete_command refuses a result for a command this agent did not claim,
    which is the check that stops one agent closing out another's work.
    """
    agent_id = f"agent-{machine}"
    claimed = fleet.claim_commands(db_path, agent_id, machine)
    if not claimed:
        return None
    command_id = claimed[0]["id"]
    fleet.complete_command(db_path, command_id, agent_id, ok, output)
    return command_id


def target_status(db_path, run_id, machine):
    run = patches.get_run(db_path, run_id)
    for target in run["targets"]:
        if target["machine"] == machine:
            return target["status"]
    return None


def local_epoch(year, month, day, hour, minute):
    """An epoch for a local wall-clock moment -- window logic runs in local time."""
    return int(time.mktime((year, month, day, hour, minute, 0, 0, 0, -1)))


def main():
    workdir = tempfile.mkdtemp(prefix="patch-tests-")
    db_path = os.path.join(workdir, "patches.db")
    try:
        fleet.init_fleet_db(db_path)
        patches.init_patches_db(db_path)

        # ------------------------------------------------------------- normalisation
        print("\n== Identity and normalisation ==")
        check("a uid is case-folded",
              patches.normalize_uid("KB5060842") == "kb5060842")
        check("surrounding space does not make a different update",
              patches.normalize_uid("  kb5060842 ") == "kb5060842")
        check("an empty uid is refused",
              raises(patches.PatchError, patches.normalize_uid, "  "))
        check("a uid with a space inside is refused",
              raises(patches.PatchError, patches.normalize_uid, "kb 5060842"))
        check("a bare number becomes a KB",
              patches.normalize_kb("5060842") == "KB5060842")
        check("a non-KB string is not forced into one",
              patches.normalize_kb("Firefox 141") == "")
        check("a Windows classification alias maps on",
              patches.normalize_classification("Security Updates") == patches.CLASS_SECURITY)
        check("an unrecognised classification is 'unknown', not 'other'",
              patches.normalize_classification("Klingon Updates") == patches.CLASS_UNKNOWN)
        check("winget's absent classification is 'unknown'",
              patches.normalize_classification("") == patches.CLASS_UNKNOWN)

        # ------------------------------------------------------------------ ingest
        print("\n== Inventory ingest ==")
        added, removed, kept = patches.ingest_inventory(
            db_path, "PC1",
            report(("kb5060842", "security"), ("kb5055523", "critical")), now=1000)
        check("a first report adds every update", (added, removed, kept) == (2, 0, 0))
        check("the machine's list reads back",
              len(patches.list_machine_patches(db_path, "PC1")) == 2)

        added, removed, kept = patches.ingest_inventory(
            db_path, "PC1", report(("kb5060842", "security")), now=2000)
        check("an update that stops being offered is REMOVED, not kept",
              (added, removed, kept) == (0, 1, 1))
        # The failure this catches: a merge instead of a replace. Every patch a machine
        # ever needed would stay listed, and confirm_from_inventory -- which reads exactly
        # this absence -- would never fire on anything.
        check("the removed update is gone from the machine's list",
              [p["uid"] for p in patches.list_machine_patches(db_path, "PC1")]
              == ["kb5060842"])
        check("first_seen survives a re-report",
              patches.list_machine_patches(db_path, "PC1")[0]["first_seen"] == 1000)
        check("last_seen moves on",
              patches.list_machine_patches(db_path, "PC1")[0]["last_seen"] == 2000)

        check("a malformed entry does not cost the whole report",
              len(patches.parse_report(
                  [{"uid": "kb1", "source": "windows_update", "title": "a"},
                   {"nonsense": True},
                   {"uid": "kb2", "source": "unknown_source", "title": "b"},
                   {"uid": "kb3", "source": "winget", "title": "c"}])) == 2)
        check("a duplicate uid in one report is collapsed",
              len(patches.parse_report(
                  [{"uid": "kb1", "source": "winget", "title": "a"},
                   {"uid": "KB1", "source": "winget", "title": "a again"}])) == 1)
        check("a titleless update falls back to its uid rather than being dropped",
              patches.parse_report(
                  [{"uid": "kb9", "source": "winget", "title": "  "}])[0]["title"] == "kb9")

        # -------------------------------------------------------------- fleet rollup
        print("\n== Fleet rollup and scope ==")
        patches.ingest_inventory(db_path, "PC2",
                                 report(("kb5060842", "security")), now=2000)
        rollup = {r["uid"]: r for r in patches.list_fleet_patches(db_path)}
        check("one row per update identity across the fleet",
              rollup["kb5060842"]["machines"] == 2)
        check("a scope naming one machine narrows the count",
              patches.list_fleet_patches(db_path, machines=["PC1"])[0]["machines"] == 1)
        # The failure this catches is the classic one: `if not machines` treating "a scope
        # that matched nothing" as "no scope", and showing the whole fleet to somebody
        # entitled to none of it.
        check("an EMPTY scope returns nothing, not everything",
              patches.list_fleet_patches(db_path, machines=[]) == [])
        check("an empty scope summarises as zero, not as the fleet",
              patches.compliance_summary(db_path, machines=[])["updates"] == 0)
        check("the summary counts machines and updates",
              patches.compliance_summary(db_path)["machines_with_updates"] == 2)

        # ---------------------------------------------------------------- approvals
        print("\n== Approvals ==")
        patches.set_approval(db_path, "KB5060842", patches.APPROVAL_APPROVED,
                             actor="op@x.com", title="Update kb5060842")
        # The failure this catches: an approval stored in the operator's casing, and a
        # dispatch that then finds nothing approved and quietly installs nothing.
        check("an approval typed in another case still matches",
              [p["uid"] for p in patches.approved_for_machine(db_path, "PC1")]
              == ["kb5060842"])
        check("an undecided update is not approved",
              patches.approvals_map(db_path, ["kb5055523"]) == {})

        patches.set_approval(db_path, "kb7000001", patches.APPROVAL_DECLINED,
                             actor="op@x.com")
        patches.ingest_inventory(
            db_path, "PC3",
            report(("kb7000001", "security"), ("kb7000002", "security"),
                   ("kb7000003", "driver")), now=2000)
        made = patches.apply_auto_approvals(db_path, [patches.CLASS_SECURITY], now=2000)
        check("auto-approval approves an undecided security update", made == 1)
        # The failure this catches: an auto-approval sweep that overwrites, so a KB an
        # operator deliberately declined comes back every time the sweep runs.
        check("auto-approval never overturns a human decline",
              patches.approvals_map(db_path, ["kb7000001"])["kb7000001"]
              == patches.APPROVAL_DECLINED)
        check("auto-approval leaves drivers alone",
              "kb7000003" not in patches.approvals_map(db_path, ["kb7000003"]))
        check("auto-approval with nothing enabled is a no-op",
              patches.apply_auto_approvals(db_path, [], now=2000) == 0)
        check("a classification that is not auto-approvable is ignored",
              patches.apply_auto_approvals(db_path, [patches.CLASS_DRIVER], now=2000) == 0)
        check("an approval can be returned to undecided",
              patches.clear_approval(db_path, "kb7000002") == "kb7000002"
              and patches.approvals_map(db_path, ["kb7000002"]) == {})

        # ------------------------------------------------------------------ windows
        print("\n== Maintenance windows ==")
        check("a window with no days is refused",
              raises(patches.PatchError, patches.validate_window, name="w", days_mask=0,
                     start_minute=120, duration_minutes=60, scope_kind=patches.SCOPE_ALL))
        check("a zero-length window is refused",
              raises(patches.PatchError, patches.validate_window, name="w", days_mask=1,
                     start_minute=120, duration_minutes=0, scope_kind=patches.SCOPE_ALL))
        check("a nameless window is refused",
              raises(patches.PatchError, patches.validate_window, name="  ", days_mask=1,
                     start_minute=120, duration_minutes=60, scope_kind=patches.SCOPE_ALL))
        check("a machine-scoped window with no machines is refused",
              raises(patches.PatchError, patches.validate_window, name="w", days_mask=1,
                     start_minute=120, duration_minutes=60,
                     scope_kind=patches.SCOPE_MACHINES, machines=[]))

        # 2026-08-30 is a Sunday; 2026-08-31 a Monday.
        sunday_2300 = local_epoch(2026, 8, 30, 23, 0)
        monday_0100 = local_epoch(2026, 8, 31, 1, 0)
        monday_0400 = local_epoch(2026, 8, 31, 4, 0)
        sunday_2200 = local_epoch(2026, 8, 30, 22, 0)
        sunday_window = {
            "enabled": True, "days_mask": 1 << 6,      # Sunday only
            "start_minute": 23 * 60, "duration_minutes": 4 * 60,
            "scope_kind": patches.SCOPE_ALL, "machines": [],
        }
        check("a window is open at its start",
              patches.window_is_open(sunday_window, sunday_2300))
        check("a window is shut before it starts",
              not patches.window_is_open(sunday_window, sunday_2200))
        # The failure this catches: treating days_mask as "the set of moments covered", so
        # a Sunday-night window silently stops at midnight -- an hour after it opened.
        check("a window that crosses midnight is still open on Monday morning",
              patches.window_is_open(sunday_window, monday_0100))
        check("it does shut when its duration runs out",
              not patches.window_is_open(sunday_window, monday_0400))
        check("a disabled window is never open",
              not patches.window_is_open(dict(sunday_window, enabled=False), sunday_2300))

        wid = patches.create_window(
            db_path, actor="op@x.com", name="Sunday night", days_mask=1 << 6,
            start_minute=23 * 60, duration_minutes=4 * 60,
            scope_kind=patches.SCOPE_MACHINES, machines=["PC1"])
        check("a window round-trips", patches.get_window(db_path, wid)["name"]
              == "Sunday night")
        check("a duplicate window name is refused",
              raises(patches.PatchError, patches.create_window, db_path, actor="op@x.com",
                     name="sunday night", days_mask=1, start_minute=0,
                     duration_minutes=60, scope_kind=patches.SCOPE_ALL))
        window = patches.get_window(db_path, wid)
        check("a scoped window covers its machine", patches.window_covers(window, "PC1"))
        check("a scoped window covers it whatever the casing",
              patches.window_covers(window, "pc1"))
        check("a scoped window does not cover another machine",
              not patches.window_covers(window, "PC2"))
        check("an all-scope window covers anything",
              patches.window_covers(dict(window, scope_kind=patches.SCOPE_ALL), "PC9"))

        # ------------------------------------------------------ the run lifecycle
        print("\n== Run lifecycle: staged is not installed ==")
        run_id = patches.create_run(db_path, machines=["PC1"], created_by="op@x.com",
                                    now=sunday_2200)
        check("a new run is scheduled",
              patches.get_run(db_path, run_id)["status"] == patches.RUN_SCHEDULED)

        # Outside the window: nothing may be queued. This is what a maintenance window IS.
        check("dispatch does nothing outside the window",
              patches.dispatch_once(db_path, now=sunday_2200) == 0)
        check("the target is still pending",
              target_status(db_path, run_id, "PC1") == patches.TARGET_PENDING)
        check("no command was queued",
              not fleet.list_commands(db_path, machine="PC1"))

        check("dispatch queues one command inside the window",
              patches.dispatch_once(db_path, now=sunday_2300) == 1)
        check("the target is in flight",
              target_status(db_path, run_id, "PC1") == patches.TARGET_IN_FLIGHT)
        queued = fleet.list_commands(db_path, machine="PC1")
        check("the queued command is install_patches",
              len(queued) == 1 and queued[0]["type"] == patches.COMMAND_TYPE)
        # list_commands is the console's summary and carries no params; the full row does.
        issued = fleet.get_command(db_path, queued[0]["id"])
        check("the command names the approved update",
              issued["params"]["uids"] == ["kb5060842"])
        check("the command carries the run id",
              issued["params"]["run_id"] == run_id)
        check("an item row was written for the attempt",
              [i["status"] for i in patches.list_run_items(db_path, run_id)]
              == [patches.ITEM_PENDING])

        # A second dispatch in the same window must not queue a second command.
        check("a second dispatch does not double-queue",
              patches.dispatch_once(db_path, now=sunday_2300) == 0)

        answer(db_path, "PC1", ok=True)
        patches.reconcile_once(db_path, now=sunday_2300 + 60)
        # THE test in this file. A command that came back OK means the agent staged what
        # it could; it does not mean the machine is patched. If this ever reads APPLIED,
        # every patch night is green and the feature is decorative.
        check("a successful command does NOT close the target",
              target_status(db_path, run_id, "PC1") != patches.TARGET_APPLIED)
        check("a successful command moves the target to rebooting",
              target_status(db_path, run_id, "PC1") == patches.TARGET_REBOOTING)
        check("the run is still running, not complete",
              patches.get_run(db_path, run_id)["status"] == patches.RUN_RUNNING)

        # The machine comes back, and no longer offers the update. THAT is the evidence.
        patches.ingest_inventory(db_path, "PC1", [], now=monday_0100)
        check("inventory confirms the install",
              patches.confirm_from_inventory(db_path, "PC1", now=monday_0100) == 1)
        check("the target is applied",
              target_status(db_path, run_id, "PC1") == patches.TARGET_APPLIED)
        check("the run is complete",
              patches.get_run(db_path, run_id)["status"] == patches.RUN_COMPLETE)
        check("the item row is applied",
              [i["status"] for i in patches.list_run_items(db_path, run_id)]
              == [patches.ITEM_APPLIED])
        check("the item row kept the machine's model for later scoring",
              patches.list_run_items(db_path, run_id)[0]["uid"] == "kb5060842")

        # ------------------------------------------------------------------ partial
        print("\n== Partial and failed outcomes ==")
        patches.ingest_inventory(
            db_path, "PC4", report(("kb8000001", "security"), ("kb8000002", "security")),
            now=3000)
        patches.set_approval(db_path, "kb8000001", patches.APPROVAL_APPROVED, actor="op")
        patches.set_approval(db_path, "kb8000002", patches.APPROVAL_APPROVED, actor="op")
        partial_run = patches.create_run(
            db_path, machines=["PC4"], created_by="op@x.com", emergency=True, now=3000)
        patches.dispatch_once(db_path, now=3000)
        answer(db_path, "PC4", ok=True)
        patches.reconcile_once(db_path, now=3100)
        # One of the two installed; the other is still offered.
        patches.ingest_inventory(db_path, "PC4", report(("kb8000002", "security")),
                                 now=3200)
        patches.confirm_from_inventory(db_path, "PC4", now=3200)
        check("some installed, some not is PARTIAL rather than failed",
              target_status(db_path, partial_run, "PC4") == patches.TARGET_PARTIAL)
        items = {i["uid"]: i["status"] for i in patches.list_run_items(db_path, partial_run)}
        check("the installed update is marked applied",
              items["kb8000001"] == patches.ITEM_APPLIED)
        check("the survivor is marked failed",
              items["kb8000002"] == patches.ITEM_FAILED)
        check("the survivor's row says why",
              [i["error"] for i in patches.list_run_items(db_path, partial_run)
               if i["uid"] == "kb8000002"][0] != "")

        patches.ingest_inventory(db_path, "PC5", report(("kb8000001", "security")),
                                 now=3000)
        none_run = patches.create_run(db_path, machines=["PC5"], created_by="op@x.com",
                                      emergency=True, now=3000)
        patches.dispatch_once(db_path, now=3000)
        answer(db_path, "PC5", ok=True)
        patches.reconcile_once(db_path, now=3100)
        patches.ingest_inventory(db_path, "PC5", report(("kb8000001", "security")),
                                 now=3200)
        patches.confirm_from_inventory(db_path, "PC5", now=3200)
        check("nothing installed is FAILED",
              target_status(db_path, none_run, "PC5") == patches.TARGET_FAILED)

        # ------------------------------------------------------------ nothing to do
        print("\n== Nothing to do is an outcome, not a skip ==")
        patches.ingest_inventory(db_path, "PC6", [], now=4000)
        empty_run = patches.create_run(db_path, machines=["PC6"], created_by="op@x.com",
                                       emergency=True, now=4000)
        patches.dispatch_once(db_path, now=4000)
        check("a fully patched machine resolves to nothing_to_do",
              target_status(db_path, empty_run, "PC6") == patches.TARGET_NOTHING_TO_DO)
        check("no command was queued for it",
              not [c for c in fleet.list_commands(db_path, machine="PC6")])
        # The failure this catches: claiming before resolving, so a fully patched machine
        # burns all three attempts and reports as failed.
        check("it did not spend an attempt",
              [t["attempts"] for t in patches.get_run(db_path, empty_run)["targets"]] == [0])
        check("the run is complete",
              patches.get_run(db_path, empty_run)["status"] == patches.RUN_COMPLETE)
        check("retry-failures does not re-arm nothing_to_do",
              patches.retry_failures(db_path, empty_run) == 0)

        # ---------------------------------------------------------- retry and backoff
        print("\n== Retry, backoff and giving up ==")
        patches.ingest_inventory(db_path, "PC7", report(("kb8000001", "security")),
                                 now=5000)
        retry_run = patches.create_run(
            db_path, machines=["PC7"], created_by="op@x.com", emergency=True,
            max_attempts=2, retry_backoff_seconds=100, now=5000)
        patches.dispatch_once(db_path, now=5000)
        answer(db_path, "PC7", ok=False, output="0x80070005 access denied")
        patches.reconcile_once(db_path, now=5010)
        check("a failed attempt goes back to pending, not failed",
              target_status(db_path, retry_run, "PC7") == patches.TARGET_PENDING)
        check("the failure reason is on the row",
              "access denied" in patches.get_run(db_path, retry_run)["targets"][0]["last_error"])
        check("it is not retried before its backoff elapses",
              patches.dispatch_once(db_path, now=5050) == 0)
        check("it is retried once the backoff elapses",
              patches.dispatch_once(db_path, now=5200) == 1)
        answer(db_path, "PC7", ok=False, output="still broken")
        patches.reconcile_once(db_path, now=5210)
        check("attempts exhausted is FAILED",
              target_status(db_path, retry_run, "PC7") == patches.TARGET_FAILED)
        check("retry-failures re-arms it",
              patches.retry_failures(db_path, retry_run) == 1)
        check("and resets the attempt budget",
              patches.get_run(db_path, retry_run)["targets"][0]["attempts"] == 0)

        # ------------------------------------------------------------ confirm timeout
        print("\n== A machine that never comes back ==")
        patches.ingest_inventory(db_path, "PC8", report(("kb8000001", "security")),
                                 now=6000)
        stuck_run = patches.create_run(
            db_path, machines=["PC8"], created_by="op@x.com", emergency=True,
            confirm_timeout_seconds=3600, now=6000)
        patches.dispatch_once(db_path, now=6000)
        answer(db_path, "PC8", ok=True)
        patches.reconcile_once(db_path, now=6010)
        check("it is waiting on its reboot",
              target_status(db_path, stuck_run, "PC8") == patches.TARGET_REBOOTING)
        check("it is left alone inside the confirm timeout",
              target_status(db_path, stuck_run, "PC8") == patches.TARGET_REBOOTING
              and patches.reconcile_once(db_path, now=6010 + 3000) is not None
              and target_status(db_path, stuck_run, "PC8") == patches.TARGET_REBOOTING)
        patches.reconcile_once(db_path, now=6010 + 4000)
        # The failure this catches: a target that sits REBOOTING forever, so its run never
        # completes and the progress bar stops at 39/40 with nothing to click.
        check("past the confirm timeout it is failed, not left forever",
              target_status(db_path, stuck_run, "PC8") == patches.TARGET_FAILED)
        check("its items are resolved too, not left pending",
              all(i["status"] != patches.ITEM_PENDING
                  for i in patches.list_run_items(db_path, stuck_run)))

        # ------------------------------------------------------------------- cancel
        print("\n== Cancel ==")
        patches.ingest_inventory(db_path, "PC9", report(("kb8000001", "security")),
                                 now=7000)
        patches.ingest_inventory(db_path, "PC10", report(("kb8000001", "security")),
                                 now=7000)
        cancel_run = patches.create_run(db_path, machines=["PC9", "PC10"],
                                        created_by="op@x.com", emergency=True, now=7000)
        patches.dispatch_once(db_path, now=7000)
        answer(db_path, "PC9", ok=True)
        patches.reconcile_once(db_path, now=7010)     # PC9 -> rebooting
        patches.cancel_run(db_path, cancel_run, actor="op@x.com", now=7020)
        check("the run is cancelled",
              patches.get_run(db_path, cancel_run)["status"] == patches.RUN_CANCELLED)
        # The failure this catches: telling an operator a machine was "cancelled" when the
        # patches are already on it and it is mid-restart.
        check("a machine already rebooting is NOT recalled",
              target_status(db_path, cancel_run, "PC9") == patches.TARGET_REBOOTING)
        check("a machine still in flight is cancelled",
              target_status(db_path, cancel_run, "PC10") == patches.TARGET_CANCELLED)
        check("a cancelled run dispatches nothing further",
              patches.dispatch_once(db_path, now=7100) == 0)
        # Cancelled is sticky: PC9 finishing must not resurrect the run.
        patches.ingest_inventory(db_path, "PC9", [], now=7200)
        patches.confirm_from_inventory(db_path, "PC9", now=7200)
        check("cancelled is sticky when the last machine finishes",
              patches.get_run(db_path, cancel_run)["status"] == patches.RUN_CANCELLED)

        # --------------------------------------------------------------- selections
        print("\n== Explicit selections ==")
        patches.ingest_inventory(db_path, "PC11", report(("kb9000001", "other")), now=8000)
        check("an explicit run with no uids is refused",
              raises(patches.PatchError, patches.create_run, db_path, machines=["PC11"],
                     created_by="op", selection=patches.SELECTION_EXPLICIT, uids=[]))
        explicit = patches.create_run(
            db_path, machines=["PC11"], created_by="op@x.com", emergency=True,
            selection=patches.SELECTION_EXPLICIT, uids=["KB9000001"], now=8000)
        check("an explicit run installs an unapproved update",
              patches.dispatch_once(db_path, now=8000) == 1)
        # The failure this catches: trusting the operator's list over the machine's
        # inventory, so Windows Update is asked for a KB the box does not need and answers
        # with a failure that reads exactly like a broken patch.
        check("an explicit uid the machine does not offer is filtered out",
              patches.selected_for_machine(db_path, "PC11", ["kb0000000"]) == [])
        check("a run with no machines is refused",
              raises(patches.PatchError, patches.create_run, db_path, machines=[],
                     created_by="op"))

        # --------------------------------------------------------- machine lifecycle
        print("\n== Machine lifecycle ==")
        before = len(patches.list_run_items(db_path, partial_run))
        patches.forget_machine(db_path, "PC4")
        check("a forgotten machine's available updates are dropped",
              patches.list_machine_patches(db_path, "PC4") == [])
        check("its target rows are dropped",
              not [t for t in patches.get_run(db_path, partial_run)["targets"]
                   if t["machine"] == "PC4"])
        # Kept on purpose: this is the outcome history a stability score is asked of, and
        # it names a model and an OS build rather than only a hostname.
        check("its patch OUTCOME history is kept",
              len(patches.list_run_items(db_path, partial_run)) == before)

        patches.ingest_inventory(db_path, "OLDNAME", report(("kb9000009", "other")),
                                 now=9000)
        patches.rename_machine(db_path, "OLDNAME", "NEWNAME")
        check("a renamed machine's updates follow it",
              [p["uid"] for p in patches.list_machine_patches(db_path, "NEWNAME")]
              == ["kb9000009"])
        check("and do not stay behind",
              patches.list_machine_patches(db_path, "OLDNAME") == [])

        # ------------------------------------------------------------- the taxonomy
        print("\n== Command taxonomy ==")
        check("install_patches is a known command type",
              patches.COMMAND_TYPE in fleet.ALL_COMMANDS)
        # Its params name updates resolved against one machine at one moment. Replayed a
        # week later they are updates that machine has since installed.
        check("install_patches cannot be saved as a favorite",
              raises(ValueError, fleet.create_favorite, db_path, "op@x.com", "sneaky",
                     patches.COMMAND_TYPE, {}))
        check("every classification has a place in the vocabulary",
              set(patches.AUTO_APPROVABLE) <= set(patches.CLASSIFICATIONS))
        # settings.py spells these out rather than importing this module (it imports only
        # i18n today). This is the assertion that keeps the two honest: a classification
        # made auto-approvable here and not offered there is a knob that approves nothing,
        # and the reverse is a knob that offers something apply_auto_approvals ignores.
        auto_setting = [s for s in settings.REGISTRY
                        if s.key == "patches.auto_approve_classifications"][0]
        check("the auto-approve setting offers exactly what patches allows",
              list(auto_setting.choices) == list(patches.AUTO_APPROVABLE))
        check("it ships approving nothing",
              auto_setting.default == [])
        check("running the schema init twice changes nothing",
              patches.init_patches_db(db_path) is None
              and len(patches.list_machine_patches(db_path, "NEWNAME")) == 1)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
