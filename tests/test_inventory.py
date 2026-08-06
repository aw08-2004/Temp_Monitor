"""Tests the reported identity fields end to end through the hub -- Service Tag
(roadmap #6) and Manufacturer (roadmap #9): report ingest -> machine_info ->
/api/machines and /api/machines/<machine>, plus that a duplicate-serial merge backfills
each of them from the dropped row like the older identity fields.

Imports app the same way test_dedup.py does (env + cwd set before import), so it drives
the real Flask app and the real save_machine_info / merge paths rather than a stand-in.
Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-inventory-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
# Sign in as a break-glass superuser, else /api/machines 403s on the permission layer.
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app  # noqa: E402

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


client = app.app.test_client()
# /api/machines is behind login_required + the permission layer; sign in as the
# break-glass superuser declared in ALLOWED_EMAILS above.
with client.session_transaction() as sess:
    sess["user"] = {"email": "tester@example.com"}


def report(machine, **fields):
    body = {"machine": machine, "temp": 41.0}
    body.update(fields)
    return client.post("/api/report", json=body)


def machines_list():
    return {row["machine"]: row for row in client.get("/api/machines").get_json()}


def test_service_tag_round_trip():
    print("\n-- service_tag survives report -> /api/machines & /api/machines/<machine> --")
    report("SVC-1", serial_number="BIOS-SER-1", service_tag="DELL-SVC-1",
           asset_tag="ASSET-1", model="TestModel")
    row = machines_list().get("SVC-1")
    check("machine appears in the list", row is not None)
    check("/api/machines carries service_tag", row and row.get("service_tag") == "DELL-SVC-1")
    check("serial and service tag are independent fields",
          row and row.get("serial_number") == "BIOS-SER-1")

    detail = client.get("/api/machines/SVC-1").get_json()
    check("/api/machines/<machine> carries service_tag",
          detail.get("service_tag") == "DELL-SVC-1")


def test_service_tag_coalesced_not_clobbered():
    print("\n-- a later report without service_tag does not wipe a stored one (COALESCE) --")
    report("SVC-2", service_tag="SVC-KEEP")
    report("SVC-2", temp=50.0)   # a plain temp report, no identity fields
    row = machines_list().get("SVC-2")
    check("stored service_tag preserved across a bare report",
          row and row.get("service_tag") == "SVC-KEEP")


def test_missing_service_tag_is_null():
    print("\n-- an agent that reports no service_tag leaves it null, not empty-string --")
    report("SVC-3", serial_number="BIOS-SER-3")
    row = machines_list().get("SVC-3")
    check("service_tag is null when never reported", row and row.get("service_tag") is None)


def test_merge_backfills_service_tag():
    print("\n-- merge backfills service_tag from the dropped row --")
    # Survivor knows its serial but never reported a service tag; the dropped duplicate
    # carries one. The merge should lift it onto the survivor, like asset_tag/model.
    report("mergeKeep", serial_number="SER-SVC-MERGE")
    report("mergeDrop", serial_number="SER-SVC-MERGE", service_tag="SVC-FROM-DROP")
    app.merge_machines("mergeKeep", "mergeDrop")
    row = machines_list().get("mergeKeep")
    check("survivor inherited the dropped row's service_tag",
          row and row.get("service_tag") == "SVC-FROM-DROP")
    check("dropped row is gone", "mergeDrop" not in machines_list())


def test_manufacturer_round_trip():
    print("\n-- manufacturer survives report -> /api/machines & /api/machines/<machine> --")
    report("MFR-1", serial_number="BIOS-SER-M1", manufacturer="Dell Inc.",
           model="Latitude 5420")
    row = machines_list().get("MFR-1")
    check("/api/machines carries manufacturer", row and row.get("manufacturer") == "Dell Inc.")
    check("manufacturer and model are independent fields",
          row and row.get("model") == "Latitude 5420")

    detail = client.get("/api/machines/MFR-1").get_json()
    check("/api/machines/<machine> carries manufacturer",
          detail.get("manufacturer") == "Dell Inc.")


def test_manufacturer_coalesced_not_clobbered():
    print("\n-- a later report without manufacturer does not wipe a stored one (COALESCE) --")
    report("MFR-2", manufacturer="HP")
    report("MFR-2", temp=50.0)   # a plain temp report, no identity fields
    row = machines_list().get("MFR-2")
    check("stored manufacturer preserved across a bare report",
          row and row.get("manufacturer") == "HP")


def test_missing_manufacturer_is_null():
    # The distinction #9's vendor dispatch rests on: null means "no agent has told us
    # what this is", which is not the same as a machine whose BIOS is unmanageable.
    print("\n-- an older agent that reports no manufacturer leaves it null --")
    report("MFR-3", serial_number="BIOS-SER-M3")
    row = machines_list().get("MFR-3")
    check("manufacturer is null when never reported", row and row.get("manufacturer") is None)


def test_manufacturer_alone_creates_a_row():
    # save_machine_info returns early unless SOME identity field is present; manufacturer
    # has to count as one, or a report carrying nothing else would be dropped silently.
    print("\n-- a report whose only identity field is manufacturer is still stored --")
    report("MFR-4", manufacturer="LENOVO")
    row = machines_list().get("MFR-4")
    check("row written from manufacturer alone", row and row.get("manufacturer") == "LENOVO")


def test_merge_backfills_manufacturer():
    print("\n-- merge backfills manufacturer from the dropped row --")
    report("mfrKeep", serial_number="SER-MFR-MERGE")
    report("mfrDrop", serial_number="SER-MFR-MERGE", manufacturer="Dell Inc.")
    app.merge_machines("mfrKeep", "mfrDrop")
    row = machines_list().get("mfrKeep")
    check("survivor inherited the dropped row's manufacturer",
          row and row.get("manufacturer") == "Dell Inc.")
    check("dropped row is gone", "mfrDrop" not in machines_list())


def test_enrollment_is_reported_per_machine():
    """The Dashboard and the Asset Inventory qualify a machine's online/offline pill with
    whether it ever enrolled, because an agent that never did still posts telemetry and so
    reads as a perfectly healthy machine while every command, terminal session, deployment,
    backup and process report on it silently does nothing.

    The distinction is deliberately "has an enrollment", not "is online" -- a machine that
    is merely switched off is still enrolled -- and it must survive a REVOKED agent, which
    is the case where the hub still holds a row for a machine it can no longer talk to.
    """
    print("\n-- /api/machines says which machines have a fleet enrollment --")
    report("ENROLL-YES")
    report("ENROLL-NO")
    secret = "inventory-test-secret"
    agent_id, _ = app.fleet.enroll_agent(app.DB_PATH, "ENROLL-YES", secret, secret)

    rows = machines_list()
    check("an enrolled machine reports enrolled=True",
          rows.get("ENROLL-YES", {}).get("enrolled") is True)
    check("a telemetry-only machine reports enrolled=False",
          rows.get("ENROLL-NO", {}).get("enrolled") is False)
    check("...and it is a real boolean, not a truthy value the console has to guess at",
          isinstance(rows.get("ENROLL-NO", {}).get("enrolled"), bool))

    detail = client.get("/api/machines/ENROLL-NO").get_json()
    check("the single-machine endpoint agrees", detail.get("enrolled") is False)
    detail = client.get("/api/machines/ENROLL-YES").get_json()
    check("...for both answers", detail.get("enrolled") is True)

    # Revoking is how an operator says "this machine is no longer ours". The console must
    # stop claiming it can act on it, exactly as if it had never enrolled.
    app.fleet.revoke_agent(app.DB_PATH, agent_id, actor="tester@example.com")
    check("a revoked agent reads as not enrolled",
          machines_list().get("ENROLL-YES", {}).get("enrolled") is False)


if __name__ == "__main__":
    test_service_tag_round_trip()
    test_service_tag_coalesced_not_clobbered()
    test_missing_service_tag_is_null()
    test_merge_backfills_service_tag()
    test_manufacturer_round_trip()
    test_manufacturer_coalesced_not_clobbered()
    test_missing_manufacturer_is_null()
    test_manufacturer_alone_creates_a_row()
    test_merge_backfills_manufacturer()
    test_enrollment_is_reported_per_machine()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
