"""bitlocker.py -- BitLocker posture and recovery-key escrow (roadmap #19).

The silent failures this file exists to catch, each of which ends with somebody reimaging a
machine they could have unlocked:

  * **A key the hub believes it holds and cannot produce.** The index rows and the encrypted
    blob are two stores, and every path that writes one has to write the other. A machine whose
    index says "escrowed" stops being asked for the key on the heartbeat -- so an index written
    without a stored password is a key that will never arrive and nobody will notice until the
    recovery screen.

  * **A key quietly overwritten or dropped.** `merge_keys` is the one function here whose
    mistakes are unrecoverable: a blob written without a password that was in it before is a
    password that now exists nowhere. So it is tested for what it must NOT do -- replace an
    id it already holds, and lose one that is not mentioned in the new submission.

  * **"We have not been told" rendered as "there is nothing to encrypt".** `support: null` and
    `support: unsupported` are different claims, and collapsing them would show a clean card
    for every PC in the fleet the day before the agent release lands.

  * **An agent filing keys it was never asked for.** `clean_key_submission` filters against the
    hub's own `escrow_wanted`, so an enrolled agent cannot grow its blob with invented ids --
    and cannot file anything at all against a protector this machine never reported.

  * **Ingest arriving on the HEARTBEAT.** A heartbeat that 500s takes the machine offline
    fleet-wide, so nothing a machine can send may raise, however malformed.

The field names asserted here are the other half of the contract the agent's
BitLockerReader/BitLockerInventoryReporter assert in C#. Drift between them is not a crash --
it is an encryption card that quietly shows nothing, and a fleet with no escrowed keys.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import bitlocker

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


def volume(mount="C:", protection="on", protectors=None, **overrides):
    payload = {
        "mount": mount,
        "device_id": r"\\?\Volume{11111111-1111-1111-1111-111111111111}",
        "protection": protection,
        "conversion": "fully_encrypted",
        "percentage": 100,
        "method": "XTS-AES 128",
        "protectors": protectors if protectors is not None else [
            {"id": "{TPM-1}", "kind": "other", "label": "TPM"},
            {"id": "{REC-1}", "kind": "recovery_password", "label": "Numerical Password"},
        ],
    }
    payload.update(overrides)
    return payload


def supported(*volumes):
    return {"support": "supported", "error": "",
            "volumes": list(volumes) if volumes else [volume()]}


def main():
    handle, db_path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        bitlocker.init_bitlocker_db(db_path)
        check("init is idempotent", bitlocker.init_bitlocker_db(db_path) is None)

        print("\n== Ingest ==")
        bitlocker.record_inventory(db_path, "PC-01", supported())
        stored = bitlocker.get_inventory(db_path, "PC-01")
        check("a supported report stores its volumes", len(stored["volumes"]) == 1)
        check("...with the protector kinds the agent sent",
              [p["kind"] for p in stored["volumes"][0]["protectors"]]
              == ["other", "recovery_password"])

        check("a machine that never reported is support: null, not unsupported",
              bitlocker.get_inventory(db_path, "NEVER-SEEN")["support"] is None)

        bitlocker.record_inventory(db_path, "PC-HOME",
                                   {"support": "unsupported", "volumes": []})
        check("unsupported is stored as itself -- it is a permanent, correct answer",
              bitlocker.get_inventory(db_path, "PC-HOME")["support"] == "unsupported")

        bitlocker.record_inventory(db_path, "PC-WEIRD", {"support": "banana", "volumes": []})
        check("an unrecognised support state becomes error, never unsupported",
              bitlocker.get_inventory(db_path, "PC-WEIRD")["support"] == "error")

        bitlocker.record_inventory(db_path, "PC-EMPTY", {"support": "supported",
                                                         "volumes": []})
        check("supported with zero volumes is an enumeration that failed, not a clean PC",
              bitlocker.get_inventory(db_path, "PC-EMPTY")["support"] == "error")

        print("\n== Ingest is non-raising: it runs on the heartbeat ==")
        for junk in (None, [], "", 7, {"support": "supported", "volumes": "nope"},
                     {"support": "supported", "volumes": [None, 3, {"mount": ""}]},
                     {"volumes": [{"mount": "D:", "protectors": [None, {"id": ""}, 5]}]}):
            try:
                bitlocker.record_inventory(db_path, "PC-JUNK", junk)
                ok = True
            except Exception:
                ok = False
            check(f"malformed payload {junk!r:.34} does not raise", ok)

        bitlocker.record_inventory(db_path, "PC-BIG", {
            "support": "supported",
            "volumes": [volume(mount=f"{i}:") for i in range(bitlocker.MAX_VOLUMES + 20)]})
        check("the volume list is capped -- this row lands in the backed-up database",
              len(bitlocker.get_inventory(db_path, "PC-BIG")["volumes"])
              <= bitlocker.MAX_VOLUMES)

        bitlocker.record_inventory(db_path, "PC-DUPE", supported(volume(protectors=[
            {"id": "{REC-1}", "kind": "recovery_password"},
            {"id": "{REC-1}", "kind": "recovery_password"},
        ])))
        check("a protector reported twice is stored once",
              len(bitlocker.get_inventory(db_path, "PC-DUPE")["volumes"][0]["protectors"]) == 1)

        bitlocker.record_inventory(db_path, "PC-UNKNOWN", supported(volume(protectors=[
            {"id": "{ODD-1}", "kind": "something_new"}])))
        check("an unrecognised protector kind is `other`, so the hub never asks it for a key",
              bitlocker.get_inventory(db_path, "PC-UNKNOWN")["volumes"][0]["protectors"][0]
              ["kind"] == "other")

        print("\n== What the heartbeat asks for ==")
        check("a recovery protector with no stored key is wanted",
              bitlocker.escrow_wanted(db_path, "PC-01") == ["{REC-1}"])
        check("a TPM protector is never wanted -- it has no transferable secret",
              "{TPM-1}" not in bitlocker.escrow_wanted(db_path, "PC-01"))
        check("a machine that never reported is asked for nothing",
              bitlocker.escrow_wanted(db_path, "NEVER-SEEN") == [])
        check("an unsupported machine is asked for nothing",
              bitlocker.escrow_wanted(db_path, "PC-HOME") == [])

        print("\n== What an agent is allowed to submit ==")
        wanted = set(bitlocker.escrow_wanted(db_path, "PC-01"))
        good = bitlocker.clean_key_submission(
            {"protector_id": "{REC-1}", "recovery_password": "1" * 48, "volume": "C:"}, wanted)
        check("a key the hub asked for is accepted", good is not None)
        check("...an id the hub did NOT ask for is refused, whatever the agent claims",
              bitlocker.clean_key_submission(
                  {"protector_id": "{MADE-UP}", "recovery_password": "x"}, wanted) is None)
        check("an empty password is refused rather than stored as a key",
              bitlocker.clean_key_submission(
                  {"protector_id": "{REC-1}", "recovery_password": ""}, wanted) is None)
        check("a non-dict submission is refused",
              bitlocker.clean_key_submission("nope", wanted) is None)

        print("\n== Merging is the one function whose mistakes are unrecoverable ==")
        existing = {"{OLD-1}": {"recovery_password": "old", "volume": "D:",
                                "escrowed_at": 100}}
        merged, added = bitlocker.merge_keys(existing, [good], now=200)
        check("a new key is added", merged["{REC-1}"]["recovery_password"] == "1" * 48)
        check("...and a key not mentioned in the submission survives",
              merged["{OLD-1}"]["recovery_password"] == "old")
        check("...and only the genuinely new one is reported as added",
              [a["protector_id"] for a in added] == ["{REC-1}"])

        collide = bitlocker.clean_key_submission(
            {"protector_id": "{REC-1}", "recovery_password": "9" * 48}, {"{REC-1}"})
        merged2, added2 = bitlocker.merge_keys(merged, [collide], now=300)
        check("a second value for an id already held NEVER overwrites the stored one",
              merged2["{REC-1}"]["recovery_password"] == "1" * 48)
        check("...and is not reported as added", added2 == [])
        check("merging nothing into an empty store is empty, not None",
              bitlocker.merge_keys(None, [], now=1) == ({}, []))

        print("\n== The index, and what it changes ==")
        bitlocker.record_escrowed(db_path, "PC-01", added, now=200)
        check("the index knows the protector", bitlocker.escrowed_ids(db_path, "PC-01")
              == {"{REC-1}"})
        check("...so the heartbeat stops asking for it",
              bitlocker.escrow_wanted(db_path, "PC-01") == [])
        merged_view = bitlocker.get_inventory(db_path, "PC-01")
        recovery = [p for p in merged_view["volumes"][0]["protectors"]
                    if p["kind"] == "recovery_password"][0]
        check("...and the card shows it as escrowed, with when", recovery["escrowed"] is True
              and recovery["escrowed_at"] == 200)
        check("a TPM protector is never marked escrowed",
              [p for p in merged_view["volumes"][0]["protectors"]
               if p["kind"] == "other"][0]["escrowed"] is False)
        check("a second record of the same protector does not duplicate the row",
              (bitlocker.record_escrowed(db_path, "PC-01", added, now=500) is None
               and bitlocker.count_keys(db_path, "PC-01") == 1))

        bitlocker.note_read(db_path, "PC-01", "{REC-1}", now=900)
        read_view = [p for p in bitlocker.get_inventory(db_path, "PC-01")["volumes"][0]
                     ["protectors"] if p["kind"] == "recovery_password"][0]
        check("a read is counted, so the card can say the key has left this hub before",
              read_view["read_count"] == 1 and read_view["last_read_at"] == 900)

        print("\n== A key outliving the volume that reported it ==")
        # The machine now reports the same volume with the recovery protector GONE -- a
        # decrypted disk, or a protector rotated away. The escrowed key must NOT vanish with
        # it: pruning on report is how one bad inventory erases the only copy of a key.
        bitlocker.record_inventory(db_path, "PC-01", supported(volume(protectors=[
            {"id": "{TPM-1}", "kind": "other"}])))
        check("a report that no longer mentions a protector does not delete its key",
              bitlocker.escrowed_ids(db_path, "PC-01") == {"{REC-1}"})
        orphans = bitlocker.get_inventory(db_path, "PC-01")["escrowed"]
        check("...and the orphaned key is surfaced rather than hidden",
              [o["protector_id"] for o in orphans] == ["{REC-1}"])

        print("\n== Machine lifecycle ==")
        bitlocker.record_inventory(db_path, "PC-BYE", supported())
        bitlocker.record_escrowed(db_path, "PC-BYE", [
            {"protector_id": "{REC-9}", "volume": "C:"}], now=100)
        bitlocker.forget_machine(db_path, "PC-BYE")
        check("a deleted machine's posture goes",
              bitlocker.get_inventory(db_path, "PC-BYE")["support"] is None)
        check("...and its escrow index with it, so a reused hostname inherits no keys",
              bitlocker.escrowed_ids(db_path, "PC-BYE") == set())

        bitlocker.record_inventory(db_path, "MERGE-OLD", supported())
        bitlocker.record_escrowed(db_path, "MERGE-OLD", [
            {"protector_id": "{REC-7}", "volume": "C:"}], now=100)
        bitlocker.rename_machine(db_path, "MERGE-OLD", "PC-NEW")
        check("a merge moves the escrow index to the survivor -- the keys are its keys",
              bitlocker.escrowed_ids(db_path, "PC-NEW") == {"{REC-7}"})
        check("...and nothing is left under the old name",
              bitlocker.escrowed_ids(db_path, "MERGE-OLD") == set())

        # A rename ONTO a name that already has posture: the survivor is the machine still
        # reporting, so its own posture and its own keys win. Asserted because the rename is
        # written as INSERT OR IGNORE, whose whole behaviour is what happens on collision --
        # and because the column list in that statement named columns no schema here has,
        # which raised OperationalError on every rename and reached `main` unnoticed.
        bitlocker.record_inventory(db_path, "KEEP-ME", supported(volume(mount="D:")))
        bitlocker.record_escrowed(db_path, "KEEP-ME", [
            {"protector_id": "{REC-KEEP}", "volume": "D:"}], now=100)
        bitlocker.record_inventory(db_path, "GO-AWAY", supported(volume(mount="C:")))
        bitlocker.record_escrowed(db_path, "GO-AWAY", [
            {"protector_id": "{REC-GONE}", "volume": "C:"}], now=100)
        bitlocker.rename_machine(db_path, "GO-AWAY", "KEEP-ME")
        survivor = bitlocker.get_inventory(db_path, "KEEP-ME")
        check("a rename onto a live name keeps the survivor's posture",
              [v["mount"] for v in survivor["volumes"]] == ["D:"])
        check("...and carries the dropped machine's keys across rather than losing them",
              bitlocker.escrowed_ids(db_path, "KEEP-ME") == {"{REC-KEEP}", "{REC-GONE}"})
        check("...leaving nothing behind under the dropped name",
              bitlocker.get_inventory(db_path, "GO-AWAY")["support"] is None and
              bitlocker.escrowed_ids(db_path, "GO-AWAY") == set())

        check("the secret id carries the machine name, so a blob cannot be read as another's",
              bitlocker.secret_id_for("PC-01") != bitlocker.secret_id_for("PC-02"))

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
