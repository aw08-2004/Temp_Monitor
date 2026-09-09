"""apkhost.py and the APK signing-block parser (roadmap #23 phase A).

**The silent failure this file exists to catch is a checksum derived from the wrong
certificate.** The hub reads the signing certificate out of an uploaded APK and puts its
SHA-256 into the provisioning QR. Get that wrong and the result is 43 characters of valid
base64url: it saves, it encodes, the code scans, and the setup wizard rejects the download
several minutes later on a device that has already been factory reset. Nothing between the
upload and that moment can tell you it was wrong. So the parser is pinned here against every
shape a real APK comes in, and against the shapes it must refuse rather than guess about.

**The v3 trap has its own test, because it is the one that reads correctly until it does not.**
A v3 block's signed data continues past the certificate list with a bare `minSdkVersion` and
`maxSdkVersion` -- plain uint32 values, not length-prefixed elements. A reader that keeps
walking treats `maxSdkVersion` (0x7FFFFFFF on every real APK) as the length of the next element.
An unchecked one slices past the end of the buffer, gets whatever is left, and carries on; this
one refuses. Neither is what should happen, and the fix is to stop at the certificates.

**The certificates planted here are arbitrary bytes rather than real X.509**, and that is the
point rather than a shortcut: the parser hashes the DER blob and never decodes it, so a test
that needed a real certificate would be testing a dependency this change exists to avoid. The
one real value in this file is the fleet's own signing digest, cross-checked against what
`apksigner verify --print-certs` prints.

The second failure is a row that outlives its blob. A database restored from backup without its
state directory leaves the console saying an APK is hosted and a device getting nothing after a
wipe -- so storing derives the checksum BEFORE it writes a row, and removing takes both away.
"""
import hashlib
import io
import os
import struct
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import apkhost
import provisioning

PASS = 0
FAIL = 0

#: The fleet's real signing certificate digest, as apksigner prints it, and its base64url form.
#: Not used to build a synthetic APK -- it is here so the arithmetic at the end of the parser is
#: checked against a value produced by a different tool.
REAL_HEX = "b39a161053eadc4090c52b5c5ae7d5cebd68d86e709e012043547b351285f468"
REAL_B64 = "s5oWEFPq3ECQxStcWufVzr1o2G5wngEgQ1R7NRKF9Gg"

EOCD_MAGIC = b"PK\x05\x06"
BLOCK_MAGIC = b"APK Sig Block 42"
V2 = 0x7109871A
V3 = 0xF05368C0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


# ================================
# BUILDING A SYNTHETIC APK
# ================================
# Imported by tests/test_provisioning_web.py as well, which needs one valid upload and has no
# business duplicating this. Two copies of a format description is how they disagree.
def _lp(payload):
    """One uint32 length-prefixed element, the only shape a signing block is built from."""
    return struct.pack("<I", len(payload)) + payload


def _sequence(elements):
    """A length-prefixed sequence OF length-prefixed elements."""
    return _lp(b"".join(_lp(e) for e in elements))


def signer(certificates, *, v3_tail=False):
    """One signer: signed data, then the signature and public-key fields nothing here reads.

    `v3_tail` appends the bare minSdkVersion / maxSdkVersion a v3 block carries after its
    certificate list. They are NOT length-prefixed, which is exactly why the parser must stop
    at the certificates.
    """
    signed = _sequence([])                      # digests, empty
    signed += _sequence(certificates)           # certificates
    if v3_tail:
        signed += struct.pack("<II", 26, 0x7FFFFFFF)
    signed += _sequence([])                     # additional attributes
    return _lp(signed) + _lp(b"sig") + _lp(b"key")


def block_value(signers):
    """The value half of a v2/v3 id-value pair: a sequence of signers."""
    return _lp(b"".join(_lp(s) for s in signers))


def synth_apk(blocks, *, leading_size=None, truncate=0, pair_length=None, zip64=False,
              omit_block=False):
    """A real one-entry ZIP with a signing block spliced in before its central directory.

    `blocks` maps a block id to its value. The knobs are the malformed shapes: a leading size
    that disagrees with the trailing one, a truncated block, a crafted pair length, a ZIP64
    end-of-central-directory offset, and no block at all.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"not really a manifest")
    data = bytearray(buffer.getvalue())

    eocd = data.rfind(EOCD_MAGIC)
    cd_offset = struct.unpack_from("<I", data, eocd + 16)[0]

    if zip64:
        struct.pack_into("<I", data, eocd + 16, 0xFFFFFFFF)
        return bytes(data)
    if omit_block:
        return bytes(data)

    pairs = b""
    for block_id, value in blocks.items():
        length = pair_length if pair_length is not None else len(value) + 4
        pairs += struct.pack("<QI", length, block_id) + value

    size = len(pairs) + 8 + len(BLOCK_MAGIC)
    block = (struct.pack("<Q", size if leading_size is None else leading_size)
             + pairs + struct.pack("<Q", size) + BLOCK_MAGIC)
    if truncate:
        block = block[:-truncate]

    data[cd_offset:cd_offset] = block
    # The EOCD's pointer has to move with it, or the archive is no longer a ZIP.
    struct.pack_into("<I", data, eocd + len(block) + 16, cd_offset + len(block))
    return bytes(data)


def refused(fn, *a, **k):
    """True when the parser refuses with its own type. **Its own type specifically** -- a bare
    `except Exception` would pass here while the upload route answered 500."""
    try:
        fn(*a, **k)
    except provisioning.ProvisioningIncomplete:
        return True
    return False


def main():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    root = tempfile.mkdtemp(prefix="apkhost-")
    try:
        apkhost.init_apkhost_db(db_path)
        apkhost.init_apkhost_db(db_path)
        check("init_apkhost_db can run twice", True)

        cert = b"pretend-certificate-bytes"
        other = b"a-different-certificate"
        expected = provisioning.checksum_from_hex(hashlib.sha256(cert).hexdigest())

        print("== The arithmetic at the end of the parser ==")
        # Cross-checked against a value a different tool produced. The synthetic APKs below
        # prove the walk finds the right bytes; this proves what happens to them afterwards.
        check("the fleet's own digest converts to the checksum apksigner implies",
              provisioning.checksum_from_hex(REAL_HEX) == REAL_B64)

        print("\n== Reading the certificate out of a signed APK ==")
        v2_only = synth_apk({V2: block_value([signer([cert])])})
        check("a v2 block yields the certificate's checksum",
              provisioning.signing_certificate_checksum(v2_only) == expected)
        check("...which is 43 unpadded characters, like every other checksum here",
              len(expected) == 43 and "=" not in expected)

        # The trap. A v3 block does not end at its certificate list.
        v3_only = synth_apk({V3: block_value([signer([cert], v3_tail=True)])})
        check("a v3 block yields the same, past its bare minSdk/maxSdk fields",
              provisioning.signing_certificate_checksum(v3_only) == expected)

        both = synth_apk({V2: block_value([signer([cert])]),
                          V3: block_value([signer([cert], v3_tail=True)])})
        check("v2 and v3 agreeing is the ordinary case",
              provisioning.signing_certificate_checksum(both) == expected)

        # Only the FIRST certificate is the signer's; the rest are a lineage.
        chained = synth_apk({V2: block_value([signer([cert, other])])})
        check("only the first certificate of the signer counts",
              provisioning.signing_certificate_checksum(chained) == expected)

        print("\n== ...and refusing rather than guessing ==")
        disagreeing = synth_apk({V2: block_value([signer([cert])]),
                                 V3: block_value([signer([other], v3_tail=True)])})
        check("v2 and v3 naming different certificates is refused",
              refused(provisioning.signing_certificate_checksum, disagreeing))

        two = synth_apk({V2: block_value([signer([cert]), signer([other])])})
        check("two signers are refused, because the QR carries one checksum",
              refused(provisioning.signing_certificate_checksum, two))

        no_block = synth_apk({}, omit_block=True)
        check("an APK with no signing block is refused",
              refused(provisioning.signing_certificate_checksum, no_block))
        try:
            provisioning.signing_certificate_checksum(no_block)
        except provisioning.ProvisioningIncomplete as e:
            # The real cause named, because "invalid APK" sends somebody to look at the wrong
            # thing. An APK without a signing block is almost always a v1-only one.
            check("...naming the v1 signature, which is the actual cause",
                  "v1" in str(e) and "apksigner" in str(e))

        empty_block = synth_apk({})
        check("a signing block with no v2 or v3 entry is refused",
              refused(provisioning.signing_certificate_checksum, empty_block))

        print("\n== Malformed input becomes a sentence, never a stack trace ==")
        cases = {
            "not a ZIP at all": b"this is not an archive",
            "an empty file": b"",
            "a leading size that disagrees with the trailing one":
                synth_apk({V2: block_value([signer([cert])])}, leading_size=99),
            "a truncated block":
                synth_apk({V2: block_value([signer([cert])])}, truncate=4),
            "a crafted pair length":
                synth_apk({V2: block_value([signer([cert])])},
                          pair_length=0xFFFFFFFFFFFFFFFF),
            "a ZIP64 central directory": synth_apk({}, zip64=True),
            "a certificate length past the end of the buffer":
                synth_apk({V2: _lp(_lp(_lp(_lp(struct.pack("<I", 0x7FFFFFFF))))) }),
        }
        for why, data in cases.items():
            check(f"{why} is refused with a reason",
                  refused(provisioning.signing_certificate_checksum, data))

        print("\n== Storing ==")
        record = apkhost.store_upload(db_path, root, io.BytesIO(v2_only), 10 * 1024 * 1024,
                                      file_name="agent.apk", actor="admin@x.com", now=1000)
        check("the record carries the derived checksum", record["checksum"] == expected)
        check("...the file's own digest, which is a different thing entirely",
              record["sha256"] != expected and len(record["sha256"]) == 64)
        check("...its size", record["size_bytes"] == len(v2_only))
        check("...and who uploaded it", record["uploaded_by"] == "admin@x.com")
        check("the blob is on disk", os.path.exists(apkhost.blob_path(root, record["sha256"])))
        check("a token was minted", len(record["token"]) == 43)

        print("\n== An APK the hub cannot read is never recorded ==")
        # The ordering that matters: the checksum is derived before the row is written, so a
        # refusal leaves the hub hosting exactly what it hosted before.
        before = apkhost.get_hosted(db_path)
        check("uploading an unreadable APK is refused",
              refused(apkhost.store_upload, db_path, root, io.BytesIO(no_block),
                      10 * 1024 * 1024, file_name="bad.apk", actor="admin@x.com"))
        check("...and the previously hosted APK is untouched",
              apkhost.get_hosted(db_path) == before)

        print("\n== Replacing ==")
        old_sha = record["sha256"]
        old_token = record["token"]
        replacement = apkhost.store_upload(db_path, root, io.BytesIO(v3_only),
                                           10 * 1024 * 1024, file_name="newer.apk",
                                           actor="admin@x.com", now=2000)
        check("there is still exactly one hosted APK",
              apkhost.get_hosted(db_path)["sha256"] == replacement["sha256"])
        check("...the old blob is gone", not os.path.exists(apkhost.blob_path(root, old_sha)))
        # The revocation this whole token design exists for.
        check("...and the old token no longer resolves",
              apkhost.hosted_by_token(db_path, old_token) is None)
        check("the new token does",
              apkhost.hosted_by_token(db_path, replacement["token"])["sha256"]
              == replacement["sha256"])

        print("\n== The token is looked up, never joined to a path ==")
        for bad in ("", "short", "../../etc/passwd", "a" * 42, "a" * 44, "a/b",
                    replacement["token"][:-1] + "!"):
            check(f"a token of {bad!r} resolves to nothing",
                  apkhost.hosted_by_token(db_path, bad) is None)
        check("a well-formed token that was never issued resolves to nothing",
              apkhost.hosted_by_token(db_path, "b" * 43) is None)

        print("\n== The URL ==")
        url = apkhost.download_url("https://fleet.example.com", replacement["token"])
        check("it is absolute and carries the token",
              url.startswith("https://fleet.example.com/provisioning/apk/")
              and replacement["token"] in url)
        check("...under one stable prefix, so a proxy can rate-limit it in one rule",
              "/provisioning/apk/" in url)
        check("a hub with no public address produces no URL rather than a relative one",
              apkhost.download_url("", replacement["token"]) == "")
        check("a trailing slash on the hub URL does not double up",
              apkhost.download_url("https://fleet.example.com/", replacement["token"]) == url)

        print("\n== Removing ==")
        sha = replacement["sha256"]
        check("removing says it removed something", apkhost.remove_hosted(db_path, root))
        check("...nothing is hosted afterwards", apkhost.get_hosted(db_path) is None)
        check("...the blob is gone", not os.path.exists(apkhost.blob_path(root, sha)))
        check("...the token stops resolving",
              apkhost.hosted_by_token(db_path, replacement["token"]) is None)
        check("removing again says there was nothing to remove",
              apkhost.remove_hosted(db_path, root) is False)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
        for base, _, names in os.walk(root, topdown=False):
            for name in names:
                try:
                    os.remove(os.path.join(base, name))
                except OSError:
                    pass
            try:
                os.rmdir(base)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
