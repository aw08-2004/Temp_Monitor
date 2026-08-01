"""firmware.py -- the BIOS flash model (roadmap #9, `update_bios`).

What this feature can get wrong is not "a page renders oddly", it is a machine that does
not start again. So the tests are weighted towards the refusals and the state machine:

  * a payload is refused onto hardware it does not name, and the refusal is a NAMED target
    rather than a dropped one;
  * the agent's "it worked" is NOT success -- only the machine coming back on the new
    version is, and a machine coming back on some third version is its own outcome;
  * an image is handed over exactly once, so a redelivered command cannot flash twice;
  * a target that has been handed the image cannot be cancelled, because the firmware may
    already be written.

What none of this covers is a real flash: no vendor tool runs here. That is the same
first-contact gap the LDAP and TURN work had, and the agent's own tests cover the call
shapes on their side.
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import bios
import firmware
import fleet

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


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def facts(manufacturer="Dell", model="Latitude 5540", version="1.20.0",
          support=bios.SUPPORT_SUPPORTED):
    return ({"manufacturer": manufacturer, "model": model},
            {"support": support, "bios_version": version})


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        bios.init_bios_db(db_path)
        firmware.init_firmware_db(db_path)
        firmware.init_firmware_db(db_path)   # idempotent, like every other init_*_db

        print("\n== A payload has to say what it fits and what it installs ==")
        for missing, why in (
            ({"vendor": ""}, "no vendor"),
            ({"models": []}, "no models"),
            ({"to_version": ""}, "no target version"),
            ({"name": "  "}, "no name"),
            ({"sha256": "not-a-digest"}, "a bad digest"),
        ):
            args = {"name": "Latitude 5540 BIOS 1.29.0", "vendor": "Dell",
                    "models": ["Latitude 5540"], "to_version": "1.29.0",
                    "sha256": DIGEST_A}
            args.update(missing)
            try:
                firmware.validate_payload(**args)
                ok = False
            except firmware.PayloadRejected:
                ok = True
            # The model list and the version are the two that matter most: without models
            # nothing can be refused, and without a version nothing can be confirmed.
            check(f"refused: {why}", ok)

        payload_id = firmware.create_payload(
            db_path, name="Latitude 5540 BIOS 1.29.0", vendor="Dell",
            models=["Latitude 5540", "Latitude 5550"], to_version="1.29.0",
            sha256=DIGEST_A, size_bytes=42 * 1024 * 1024, filename="L5540_1.29.0.exe",
            install_args="/s /f", created_by="op@x.com")
        payload = firmware.get_payload(db_path, payload_id)
        check("payload round-trips its models", payload["models"] == ["Latitude 5540",
                                                                     "Latitude 5550"])
        check("payload_for_blob finds the image",
              firmware.payload_for_blob(db_path, DIGEST_A) == payload_id)
        check("and does not invent one for a digest nobody uploaded",
              firmware.payload_for_blob(db_path, DIGEST_B) is None)

        print("\n== Preconditions, which are refusals rather than warnings ==")
        info, inventory = facts()
        check("a matching machine passes",
              firmware.check_machine(payload, info, inventory) is None)
        check("wrong manufacturer is refused",
              "HP" in (firmware.check_machine(payload, *facts(manufacturer="HP")[:1],
                                              inventory) or ""))
        reason = firmware.check_machine(payload, {"manufacturer": "Dell",
                                                  "model": "OptiPlex 7010"}, inventory)
        check("wrong model is refused, and the reason names the model",
              reason is not None and "OptiPlex 7010" in reason)
        # Null manufacturer is "no agent has told us", not a match. This is the one feature
        # where proceeding on an unknown is unacceptable.
        check("an unreported manufacturer is refused, not assumed",
              firmware.check_machine(payload, {"manufacturer": "", "model": "Latitude 5540"},
                                     inventory) is not None)
        check("a machine with no manageable firmware is refused",
              firmware.check_machine(payload, info,
                                     {"support": bios.SUPPORT_UNSUPPORTED}) is not None)
        # A no-op flash could never be confirmed -- the completion signal IS the version --
        # so it would sit REBOOTING until it timed out as a failure.
        check("a machine already on the target version is refused",
              firmware.check_machine(payload, info,
                                     {"support": bios.SUPPORT_SUPPORTED,
                                      "bios_version": "1.29.0"}) is not None)
        check("version comparison is trimmed and casefolded",
              firmware.check_machine(payload, info,
                                     {"support": bios.SUPPORT_SUPPORTED,
                                      "bios_version": " 1.29.0 "}) is not None)

        print("\n== A mixed fleet: fitting machines proceed, the rest are NAMED ==")
        machine_facts = {
            "PC-FIT": facts(),
            "PC-WRONG-MODEL": facts(model="OptiPlex 7010"),
            "PC-HP": facts(manufacturer="HP"),
        }
        job_id, targets = firmware.create_job(
            db_path, payload_id=payload_id,
            machines=["PC-FIT", "PC-WRONG-MODEL", "PC-HP", "PC-GHOST"],
            created_by="op@x.com", machine_facts=machine_facts)
        by_machine = {t["machine"]: t for t in targets}
        check("the fitting machine is queued",
              by_machine["PC-FIT"]["status"] == firmware.TARGET_PENDING)
        check("the wrong model is refused, not dropped",
              by_machine["PC-WRONG-MODEL"]["status"] == firmware.TARGET_REFUSED)
        check("the wrong vendor is refused, not dropped",
              by_machine["PC-HP"]["status"] == firmware.TARGET_REFUSED)
        check("a machine that does not exist is refused with a reason",
              by_machine["PC-GHOST"]["status"] == firmware.TARGET_REFUSED
              and by_machine["PC-GHOST"]["error"])
        check("every refusal carries a reason an operator can act on",
              all(t["error"] for t in targets
                  if t["status"] == firmware.TARGET_REFUSED))
        check("the from_version is captured at queue time",
              by_machine["PC-FIT"]["from_version"] == "1.20.0")
        job = firmware.get_job(db_path, job_id)
        check("the job is scheduled while one target is still pending",
              job["status"] == firmware.JOB_SCHEDULED)

        print("\n== The window gates dispatch, and an offline machine keeps its turn ==")
        future = int(time.time()) + 3600
        later_id, _ = firmware.create_job(
            db_path, payload_id=payload_id, machines=["PC-FIT"], created_by="op@x.com",
            window_start=future, machine_facts=machine_facts)
        check("nothing dispatches before the window opens",
              firmware.dispatch_once(db_path) == 1)  # only the un-windowed job's target
        check("the windowed job is untouched",
              firmware.get_job(db_path, later_id)["targets"][0]["status"]
              == firmware.TARGET_PENDING)
        try:
            firmware.create_job(db_path, payload_id=payload_id, machines=["PC-FIT"],
                                created_by="op@x.com",
                                window_end=int(time.time()) - 60,
                                machine_facts=machine_facts)
            ok = False
        except firmware.PayloadRejected:
            ok = True
        check("a window that has already closed is refused at creation", ok)

        offline_id, _ = firmware.create_job(
            db_path, payload_id=payload_id, machines=["PC-FIT"], created_by="op@x.com",
            machine_facts=machine_facts)
        check("a machine that is not online is skipped, not dispatched to",
              firmware.dispatch_once(db_path, online_machines=set()) == 0)
        # Left PENDING rather than failed: this feature has no retry, so a machine that was
        # merely switched off must not be recorded as a failure nobody dares re-run.
        check("and it stays pending, so it goes out when the PC reappears",
              firmware.get_job(db_path, offline_id)["targets"][0]["status"]
              == firmware.TARGET_PENDING)
        check("once online it dispatches",
              firmware.dispatch_once(db_path, online_machines={"PC-FIT"}) == 1)

        print("\n== The image is handed over exactly once ==")
        target_id = firmware.get_job(db_path, job_id)["targets"][0]["id"]
        target = firmware.get_target(db_path, target_id)
        check("dispatch queued a real update_bios command", bool(target["command_id"]))
        params = fleet.get_command(db_path, target["command_id"])["params"]
        # The image URL, its digest and the BIOS setup password are all absent: params are
        # audited verbatim, so two of those would be a credential and a download link in
        # the audit log inside the database that is itself backed up.
        check("the command carries an opaque id and nothing else",
              params == {"update_id": target_id})
        check("the first fetch claims it", firmware.start_target(db_path, target_id) is True)
        check("a redelivered command cannot fetch it again",
              firmware.start_target(db_path, target_id) is False)
        check("and the target is now flashing",
              firmware.get_target(db_path, target_id)["status"]
              == firmware.TARGET_FLASHING)

        print("\n== The agent's success is not success ==")
        firmware.ingest_result(db_path, target_id, {"ok": True})
        check("a good report only reaches REBOOTING",
              firmware.get_target(db_path, target_id)["status"]
              == firmware.TARGET_REBOOTING)
        check("the job is still running, not complete",
              firmware.get_job(db_path, job_id)["status"] == firmware.JOB_RUNNING)
        check("a machine still reporting its OLD version is left alone",
              firmware.confirm_from_inventory(db_path, "PC-FIT", "1.20.0") == 0)
        check("...and stays REBOOTING",
              firmware.get_target(db_path, target_id)["status"]
              == firmware.TARGET_REBOOTING)
        check("the new version confirms it",
              firmware.confirm_from_inventory(db_path, "PC-FIT", " 1.29.0 ") == 1)
        confirmed = firmware.get_target(db_path, target_id)
        check("...as APPLIED", confirmed["status"] == firmware.TARGET_APPLIED)
        check("...recording what the machine actually said",
              confirmed["observed_version"] == "1.29.0")
        check("a second report does not reopen a terminal target",
              firmware.confirm_from_inventory(db_path, "PC-FIT", "1.29.0") == 0)

        print("\n== A third version is its own outcome, never 'applied' ==")
        third_id, _ = firmware.create_job(db_path, payload_id=payload_id,
                                          machines=["PC-FIT"], created_by="op@x.com",
                                          machine_facts=machine_facts)
        firmware.dispatch_once(db_path)
        t3 = firmware.get_job(db_path, third_id)["targets"][0]["id"]
        firmware.start_target(db_path, t3)
        firmware.ingest_result(db_path, t3, {"ok": True})
        firmware.confirm_from_inventory(db_path, "PC-FIT", "1.31.0")
        row = firmware.get_target(db_path, t3)
        check("a version that is neither -> UNKNOWN", row["status"] == firmware.TARGET_UNKNOWN)
        check("...and says what it saw", "1.31.0" in row["error"])

        print("\n== Failures the agent DOES know about ==")
        fail_id, _ = firmware.create_job(db_path, payload_id=payload_id,
                                         machines=["PC-FIT"], created_by="op@x.com",
                                         machine_facts=machine_facts)
        firmware.dispatch_once(db_path)
        tf = firmware.get_job(db_path, fail_id)["targets"][0]["id"]
        firmware.start_target(db_path, tf)
        firmware.ingest_result(db_path, tf, {"ok": False, "error": "running on battery"})
        check("a reported failure is terminal at once",
              firmware.get_target(db_path, tf)["status"] == firmware.TARGET_FAILED)
        check("...with the machine's own reason",
              "battery" in firmware.get_target(db_path, tf)["error"])
        check("the job completes once every target is terminal",
              firmware.get_job(db_path, fail_id)["status"] == firmware.JOB_COMPLETE)

        unsup_id, _ = firmware.create_job(db_path, payload_id=payload_id,
                                          machines=["PC-FIT"], created_by="op@x.com",
                                          machine_facts=machine_facts)
        firmware.dispatch_once(db_path)
        tu = firmware.get_job(db_path, unsup_id)["targets"][0]["id"]
        firmware.start_target(db_path, tu)
        firmware.ingest_result(db_path, tu, {"unsupported": True})
        # Kept distinct from `failed` for the same reason bios keeps `unsupported` distinct
        # from `error`: one is a fact about the hardware, the other is something going wrong.
        check("'this hardware cannot be flashed' is REFUSED, not failed",
              firmware.get_target(db_path, tu)["status"] == firmware.TARGET_REFUSED)

        print("\n== Nothing a machine can send raises ==")
        junk_id, _ = firmware.create_job(db_path, payload_id=payload_id,
                                         machines=["PC-FIT"], created_by="op@x.com",
                                         machine_facts=machine_facts)
        firmware.dispatch_once(db_path)
        tj = firmware.get_job(db_path, junk_id)["targets"][0]["id"]
        firmware.start_target(db_path, tj)
        for junk in (None, [], "ok", {"ok": "yes please", "error": object()}, {}):
            try:
                firmware.ingest_result(db_path, tj, junk)
                raised = False
            except Exception:
                raised = True
            check(f"ingest_result survives {type(junk).__name__}", not raised)
        check("ingest_result on an unknown id answers None rather than raising",
              firmware.ingest_result(db_path, "nope", {"ok": True}) is None)
        check("confirm_from_inventory ignores an empty version",
              firmware.confirm_from_inventory(db_path, "PC-FIT", "") == 0)

        print("\n== Cancel is honest about what it can stop ==")
        cancel_id, _ = firmware.create_job(db_path, payload_id=payload_id,
                                           machines=["PC-FIT"], created_by="op@x.com",
                                           machine_facts=machine_facts)
        tc = firmware.get_job(db_path, cancel_id)["targets"][0]["id"]
        ok, status = firmware.cancel_target(db_path, tc, actor="op@x.com")
        check("a pending target cancels", ok and status == firmware.TARGET_CANCELLED)

        held_id, _ = firmware.create_job(db_path, payload_id=payload_id,
                                         machines=["PC-FIT"], created_by="op@x.com",
                                         machine_facts=machine_facts)
        firmware.dispatch_once(db_path)
        th = firmware.get_job(db_path, held_id)["targets"][0]["id"]
        firmware.start_target(db_path, th)
        ok, status = firmware.cancel_target(db_path, th)
        # Claiming is at-most-once and there is no back-channel; the firmware may already
        # be written. A row reading "cancelled" over that would be worse than no cancel.
        check("a target that already holds the image cannot be recalled",
              not ok and status == firmware.TARGET_FLASHING)
        cancelled, left = firmware.cancel_job(db_path, held_id)
        check("cancelling its job reports it as still flashing", left == 1)
        check("...and the job is NOT marked cancelled while that machine is being written",
              firmware.get_job(db_path, held_id)["status"] != firmware.JOB_CANCELLED)

        print("\n== Nobody is left waiting forever ==")
        now = int(time.time())
        check("a flash that never reported is failed after its timeout",
              firmware.expire_stale(db_path, now=now + 10_000, flashing_timeout=3600,
                                    confirm_timeout=86400) >= 1)
        swept = firmware.get_target(db_path, th)
        check("...and the message says the machine may be mid-flash",
              swept["status"] == firmware.TARGET_FAILED and "mid-flash" in swept["error"])

        stale_id, _ = firmware.create_job(db_path, payload_id=payload_id,
                                          machines=["PC-FIT"], created_by="op@x.com",
                                          machine_facts=machine_facts)
        firmware.dispatch_once(db_path)
        ts = firmware.get_job(db_path, stale_id)["targets"][0]["id"]
        firmware.start_target(db_path, ts)
        firmware.ingest_result(db_path, ts, {"ok": True})
        firmware.expire_stale(db_path, now=now + 200_000, confirm_timeout=86400)
        never = firmware.get_target(db_path, ts)
        check("a staged flash that never came back is failed, not left rebooting",
              never["status"] == firmware.TARGET_FAILED)
        check("...and says the machine never reported the new version",
              "new BIOS version" in never["error"])
        check("expiring a target completes its job",
              firmware.get_job(db_path, stale_id)["status"] == firmware.JOB_COMPLETE)

        print("\n== A command that dies without a result still retires its target ==")
        # The gap this covers: dispatch queues a command to a machine that was online a
        # second ago, and it then sleeps. fleet expires the COMMAND, but nothing was
        # telling the target -- so it sat in_flight forever and its job could never
        # complete. Neither timeout catches it: both count from a fetch that never happened.
        inflight = firmware.get_job(db_path, offline_id)["targets"][0]
        check("the earlier dispatch is still sitting in_flight",
              inflight["status"] == firmware.TARGET_IN_FLIGHT)
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE commands SET status = ? WHERE id = ?",
                         (fleet.STATUS_EXPIRED, inflight["command_id"]))
        check("reconcile_once retires it", firmware.reconcile_once(db_path) == 1)
        retired = firmware.get_target(db_path, inflight["id"])
        check("...as FAILED, saying nothing was flashed",
              retired["status"] == firmware.TARGET_FAILED
              and "nothing was flashed" in retired["error"])
        check("...and its job can now complete",
              firmware.get_job(db_path, offline_id)["status"] == firmware.JOB_COMPLETE)

        print("\n== An image in use cannot be deleted out from under a flash ==")
        open_id, _ = firmware.create_job(db_path, payload_id=payload_id,
                                         machines=["PC-FIT"], created_by="op@x.com",
                                         machine_facts=machine_facts)
        try:
            firmware.delete_payload(db_path, payload_id)
            refused = False
        except firmware.PayloadRejected:
            refused = True
        check("delete is refused while a job is open", refused)
        firmware.cancel_job(db_path, open_id)
        # ...including one whose window has not opened yet, which is the case that would
        # otherwise bite: it looks idle and is not.
        firmware.cancel_job(db_path, later_id)
        check("and allowed once nothing is in flight",
              firmware.delete_payload(db_path, payload_id) is True)
        check("deleting an unknown image is False, not an exception",
              firmware.delete_payload(db_path, "nope") is False)

        print("\n== Machine lifecycle ==")
        second = firmware.create_payload(db_path, name="P2", vendor="Dell",
                                         models=["Latitude 5540"], to_version="2.0.0",
                                         sha256=DIGEST_B, created_by="op@x.com")
        gone_id, _ = firmware.create_job(db_path, payload_id=second,
                                         machines=["PC-FIT", "PC-BYE"],
                                         created_by="op@x.com",
                                         machine_facts={"PC-FIT": facts(),
                                                        "PC-BYE": facts()})
        firmware.forget_machine(db_path, "PC-BYE")
        remaining = firmware.get_job(db_path, gone_id)["targets"]
        check("a deleted machine's target goes",
              [t["machine"] for t in remaining] == ["PC-FIT"])

        merge_id, _ = firmware.create_job(db_path, payload_id=second,
                                          machines=["MERGE-OLD"], created_by="op@x.com",
                                          machine_facts={"MERGE-OLD": facts()})
        firmware.rename_machine(db_path, "MERGE-OLD", "PC-FIT")
        check("a merge carries the target to the survivor",
              firmware.get_job(db_path, merge_id)["targets"][0]["machine"] == "PC-FIT")
        # Both rows describe one physical machine, and it only needs flashing once.
        firmware.rename_machine(db_path, "PC-FIT", "PC-FIT")   # no-op, must not raise
        collide_id, _ = firmware.create_job(
            db_path, payload_id=second, machines=["PC-FIT", "DUPE"],
            created_by="op@x.com",
            machine_facts={"PC-FIT": facts(), "DUPE": facts()})
        firmware.rename_machine(db_path, "DUPE", "PC-FIT")
        check("a merge collision drops the duplicate rather than raising",
              len(firmware.get_job(db_path, collide_id)["targets"]) == 1)

        print("\n== The audit trail records what was aimed where ==")
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                "SELECT action, detail_json FROM audit_log "
                "WHERE action LIKE 'create_firmware%'"
            ).fetchall()]
        check("creating a payload and a job are both audited",
              {r["action"] for r in rows} == {"create_firmware_payload",
                                              "create_firmware_job"})
        job_rows = [r for r in rows if r["action"] == "create_firmware_job"]
        check("and the job row names the refused machines and why",
              any("PC-WRONG-MODEL" in (r["detail_json"] or "") for r in job_rows))

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
