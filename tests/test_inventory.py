"""Tests the Service Tag identity field end to end through the hub (roadmap #6):
report ingest -> machine_info -> /api/machines and /api/machines/<machine>, plus that a
duplicate-serial merge backfills service_tag from the dropped row like the other
identity fields.

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


if __name__ == "__main__":
    test_service_tag_round_trip()
    test_service_tag_coalesced_not_clobbered()
    test_missing_service_tag_is_null()
    test_merge_backfills_service_tag()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
