"""Regenerate the cross-implementation envelope fixture.

    python tests/make_envelope_fixture.py

The FHBK1 envelope is implemented TWICE -- backups.py here and BackupEnvelope.cs on the
agent -- because the agent has to seal its own archives before they leave the machine.
Two crypto implementations of one format drift silently, and the way you find out is a
backup that cannot be restored.

So a real artifact is checked in, sealed by the Python side, and the C# tests decrypt it.
The C# tests also seal one of their own, which tests/test_backups.py then decrypts, so the
agreement is verified in BOTH directions. Neither suite can pass by being consistently
wrong on its own.

Run this only when the format INTENTIONALLY changes; regenerating it casually would mean
a compatibility break shows up as "the fixture changed" instead of "the C# side fails".
"""
import base64
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backups

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(HERE, "fixtures")

# Deliberately awkward plaintext: crosses several chunks at the small chunk size below,
# is not a round number of chunks, and contains non-ASCII plus a NUL.
PLAINTEXT = (b"FleetHub backup envelope fixture\n"
             + bytes(range(256)) * 400
             + "café — naïve ümlaut\n".encode("utf-8")
             + b"\x00tail")

CHUNK_BYTES = 4096
MASTER_KEY_B64 = "Zm9vYmFyMDEyMzQ1Njc4OWFiY2RlZmdoaWprbG1ub3A="   # 32 bytes, fixed
MACHINE = "FIXTURE-PC"


def main():
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    master_key = backups.decode_master_key(MASTER_KEY_B64)
    machine_key = backups.derive_machine_key(master_key, MACHINE)

    written = {}
    for name, key, extra in (
        ("envelope-hub", master_key, {"kind": backups.BACKUP_HUB_DB,
                                      "source": "temp_v2.db"}),
        # The machine case is the one that matters most: it exercises key DERIVATION as
        # well as the envelope, and that is the part the agent must get bit-identical.
        ("envelope-machine", machine_key, {"kind": backups.BACKUP_MACHINE_FILES,
                                           "machine": MACHINE}),
    ):
        buffer = io.BytesIO()
        header, size, digest = backups.write_envelope(
            backups.iter_gzip(backups.iter_file(io.BytesIO(PLAINTEXT), CHUNK_BYTES)),
            buffer, key, header_extra=extra, chunk_bytes=CHUNK_BYTES)
        path = os.path.join(FIXTURE_DIR, name + backups.FILE_EXTENSION)
        with open(path, "wb") as fh:
            fh.write(buffer.getvalue())
        written[name] = {"bytes": size, "sha256": digest, "key_id": header["key_id"]}
        print(f"  wrote {path} ({size} bytes)")

    meta = {
        "_comment": [
            "Cross-implementation fixture for the FHBK1 backup envelope.",
            "Read by tests/test_backups.py AND by the agent's BackupEnvelope tests.",
            "Regenerate with: python tests/make_envelope_fixture.py -- but only when the",
            "format changes deliberately. See that script's docstring.",
        ],
        "master_key_b64": MASTER_KEY_B64,
        "machine": MACHINE,
        "machine_key_b64": base64.b64encode(machine_key).decode("ascii"),
        "chunk_bytes": CHUNK_BYTES,
        "plaintext_b64": base64.b64encode(PLAINTEXT).decode("ascii"),
        "plaintext_sha256": __import__("hashlib").sha256(PLAINTEXT).hexdigest(),
        "artifacts": written,
    }
    meta_path = os.path.join(FIXTURE_DIR, "envelope.json")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    print(f"  wrote {meta_path}")
    print(f"\nmachine key for {MACHINE}: {meta['machine_key_b64']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
