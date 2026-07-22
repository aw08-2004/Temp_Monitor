"""Unit tests for backups.py -- the backup core, with no Flask and no network involved.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
Under pytest, conftest.py wraps `check` so a false condition fails the test properly.

The emphasis is on the ways this module can be wrong SILENTLY -- which for a backup means
"wrong until the day you need it, and then total":

  * an envelope that decrypts a file someone truncated, reordered or re-headered, so a
    half-uploaded artifact restores as a plausible-looking corrupt database,
  * a SigV4 signer that only agrees with itself (checked against AWS's own published
    vectors, because a signer verified against its own output is verified against
    nothing),
  * rotation that deletes the wrong generation, or empties the bucket outright,
  * a snapshot taken while the database is being written, and
  * a scheduler that backs up every tick, or never.

The end-to-end test runs a real backup against an in-memory destination and then
restores it through read_envelope, because "the bytes we uploaded can be turned back
into the database" is the only assertion that actually matters here.
"""
import io
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backups
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


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def error_of(fn, *args, **kwargs):
    """The message from an expected failure, or "" -- so a test can assert the operator
    is told something actionable, not just that something went wrong."""
    try:
        fn(*args, **kwargs)
    except Exception as e:
        return str(e)
    return ""


class FakeDestination:
    """An in-memory bucket. Records every call, so a test can assert on the ORDER of
    operations -- uploading before rotating is not a detail, it is what stops a rotation
    from deleting the newest generation to make room for one that then fails."""

    def __init__(self):
        self.objects = {}
        self.calls = []

    def put(self, key, fileobj, size, sha256_hex):
        data = fileobj.read()
        self.calls.append(("put", key))
        if len(data) != size:
            raise backups.BackupError(
                f"declared {size} bytes but sent {len(data)}")
        self.objects[key] = data

    def open(self, key):
        self.calls.append(("open", key))
        if key not in self.objects:
            raise backups.BackupError("no such object")

        class Response:
            def __init__(self, payload):
                self.content = payload

            def close(self):
                pass
        return Response(self.objects[key])

    def delete(self, key):
        self.calls.append(("delete", key))
        self.objects.pop(key, None)

    def list(self, prefix):
        self.calls.append(("list", prefix))
        return [{"key": k, "size": len(v)} for k, v in sorted(self.objects.items())
                if k.startswith(prefix)]


def seed_database(db_path, rows=200):
    """A database with enough content that gzip has something to do and VACUUM INTO is
    not trivially a no-op."""
    fleet.init_fleet_db(db_path)
    backups.init_backups_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS readings "
                     "(id INTEGER PRIMARY KEY, machine TEXT, temp REAL, ts INTEGER)")
        conn.executemany(
            "INSERT INTO readings(machine, temp, ts) VALUES (?, ?, ?)",
            [(f"PC-{i % 7}", 40.0 + (i % 30), 1_700_000_000 + i) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def sqlite_facts(path):
    """(row count, integrity verdict), with the connection definitively CLOSED.

    `with sqlite3.connect(...)` commits but does not close, and on Windows an open handle
    makes the file undeletable -- which is how a leaked test connection turns into a
    confusing PermissionError three assertions later.
    """
    conn = sqlite3.connect(path)
    try:
        return (conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
                conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def main():
    workdir = tempfile.mkdtemp(prefix="backup-tests-")
    log_dir = os.path.join(workdir, "logs")
    os.makedirs(log_dir)
    db_path = os.path.join(log_dir, "temp_v2.db")
    saved_env = os.environ.get(backups.MASTER_KEY_ENV)
    try:
        seed_database(db_path)

        # ============================================================
        print("\n== SigV4, against AWS's published vectors ==")
        # ============================================================
        # From "Examples of how to derive a signing key for Signature Version 4" in the
        # AWS docs. If this drifts, every S3 destination starts returning 403.
        aws_secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
        derived = backups._signing_key(aws_secret, "20120215", "us-east-1", "iam")
        check("signing key matches the AWS documented derivation",
              derived.hex() ==
              "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d")

        # aws-sig-v4-test-suite, "get-vanilla": the canonical request is fed in verbatim
        # so this checks the string-to-sign and signature stages independently of how
        # sigv4_headers happens to build headers today.
        canonical = "\n".join([
            "GET", "/", "",
            "host:example.amazonaws.com", "x-amz-date:20150830T123600Z", "",
            "host;x-amz-date",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        ])
        signature, scope = backups.sigv4_signature(
            canonical, "20150830T123600Z", "us-east-1", "service", aws_secret)
        check("get-vanilla signature matches the AWS test suite",
              signature ==
              "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")
        check("credential scope is date/region/service/aws4_request",
              scope == "20150830/us-east-1/service/aws4_request")

        check("path encoding handles spaces and reserved characters",
              backups.encode_path("/my bucket/a+b") == "/my%20bucket/a%2Bb")
        check("path encoding keeps separators", "/" in backups.encode_path("/a/b/c"))
        check("empty path encodes to /", backups.encode_path("") == "/")
        check("canonical query sorts by encoded name",
              backups._canonical_query("b=2&a=1") == "a=1&b=2")

        # The encode-once contract. S3 signs the path as it appears in the URL, so if the
        # URL builder and the signer both encode, a "%20" becomes "%2520" and every
        # request 403s with SignatureDoesNotMatch -- a failure that only shows up against
        # a real bucket, and only for keys containing an unsafe character.
        s3 = backups.S3Destination(
            {"endpoint": "https://s3.example.com", "bucket": "my bucket",
             "region": "eu-west-1", "prefix": "", "path_style": True},
            {"access_key_id": "AKID", "secret_access_key": aws_secret})
        url = s3._url("hub-db/a b.fhb")
        check("the URL encodes the key once", "/my%20bucket/hub-db/a%20b.fhb" in url)
        check("the URL does NOT double-encode", "%2520" not in url)
        signed_path = backups.sigv4_headers(
            "PUT", url, "eu-west-1", "AKID", aws_secret, "x", now=1440938160)
        check("the signer does not re-encode what it signs",
              "%2520" not in signed_path["Authorization"] and
              signed_path["host"] == "s3.example.com")
        virtual = backups.S3Destination(
            {"endpoint": "https://s3.example.com", "bucket": "b", "region": "r",
             "prefix": "", "path_style": False},
            {"access_key_id": "A", "secret_access_key": "S"})
        check("virtual-hosted style puts the bucket in the hostname",
              virtual._url("k.fhb") == "https://b.s3.example.com/k.fhb")
        check("path style puts the bucket in the path",
              s3._url("k.fhb") == "https://s3.example.com/my%20bucket/k.fhb")

        headers = backups.sigv4_headers(
            "PUT", "https://s3.example.com/bucket/key", "eu-west-1", "AKID",
            aws_secret, "abc123", now=1440938160)
        check("signed headers include the payload hash header",
              "x-amz-content-sha256" in headers["Authorization"])
        check("authorization names the algorithm and credential",
              headers["Authorization"].startswith(
                  "AWS4-HMAC-SHA256 Credential=AKID/20150830/eu-west-1/s3/"))

        presigned = backups.sigv4_presign(
            "PUT", "https://s3.example.com/bucket/machines/PC-1/f.fhb", "eu-west-1",
            "AKID", aws_secret, 900, now=1440938160)
        check("presigned URL carries a signature", "X-Amz-Signature=" in presigned)
        check("presigned URL carries its expiry", "X-Amz-Expires=900" in presigned)
        check("presigned URL keeps the object path",
              "/bucket/machines/PC-1/f.fhb?" in presigned)

        # ============================================================
        print("\n== Master key ==")
        # ============================================================
        os.environ.pop(backups.MASTER_KEY_ENV, None)
        env_path = os.path.join(workdir, ".env")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("HUB_URL=https://hub.example.com")      # deliberately no trailing \n

        key_b64, created = backups.ensure_master_key(env_path)
        check("first call creates a key", created is True)
        with open(env_path, "r", encoding="utf-8") as fh:
            env_text = fh.read()
        check("key is appended to .env on its own line",
              f"\n{backups.MASTER_KEY_ENV}={key_b64}\n" in env_text)
        check("the pre-existing .env line survives intact",
              env_text.startswith("HUB_URL=https://hub.example.com\n"))
        check("no BOM is written", not env_text.startswith("﻿"))

        again, created_again = backups.ensure_master_key(env_path)
        check("second call is a no-op", created_again is False and again == key_b64)
        check("only one key line exists",
              env_text.count(backups.MASTER_KEY_ENV) == 1)

        master_key = backups.load_master_key()
        check("loaded key is 32 bytes", len(master_key) == backups.MASTER_KEY_BYTES)
        check("key id is stable", backups.key_id(master_key) == backups.key_id(master_key))
        check("key id differs for a different key",
              backups.key_id(master_key) !=
              backups.key_id(backups.decode_master_key(backups.generate_master_key())))
        check("a truncated key is rejected by length, not by base64",
              "32 bytes" in error_of(backups.decode_master_key, "aGVsbG8="))
        check("non-base64 is named as such",
              "base64" in error_of(backups.decode_master_key, "not!base64!"))

        # ============================================================
        print("\n== Envelope ==")
        # ============================================================
        plaintext = b"".join(struct.pack(">I", i) for i in range(120_000))
        sealed = io.BytesIO()
        header, written, digest = backups.write_envelope(
            backups.iter_gzip(backups.iter_file(io.BytesIO(plaintext), 8192)),
            sealed, master_key, header_extra={"kind": "hub_db", "source": "temp_v2.db"},
            chunk_bytes=8192)
        raw = sealed.getvalue()

        check("artifact starts with the versioned magic", raw.startswith(backups.MAGIC))
        check("reported length matches the bytes written", written == len(raw))
        check("header records the key id",
              header["key_id"] == backups.key_id(master_key))
        check("header extras survive", header["kind"] == "hub_db")
        check("multiple chunks were written", len(raw) > 8192 * 2)

        read_header, chunks = backups.read_envelope(io.BytesIO(raw), master_key)
        restored = b"".join(backups.iter_gunzip(chunks))
        check("round trip restores the plaintext exactly", restored == plaintext)
        check("read header matches written header", read_header["kind"] == "hub_db")
        check("compression actually compressed", written < len(plaintext))

        def consume(data, key=master_key):
            _, gen = backups.read_envelope(io.BytesIO(data), key)
            return b"".join(gen)

        other_key = backups.decode_master_key(backups.generate_master_key())
        check("a different master key is refused by key id, not by a garbled result",
              "different master key" in error_of(consume, raw, other_key))

        check("truncating the tail is detected",
              raises(ValueError, consume, raw[:-64]))
        # Cutting at a chunk boundary is the nastier case: every remaining chunk is
        # individually valid and authenticates fine. Only the missing final flag catches it.
        head_len = struct.unpack(">I", raw[len(backups.MAGIC):len(backups.MAGIC) + 4])[0]
        first_chunk_at = len(backups.MAGIC) + 4 + head_len
        first_len = struct.unpack(">I", raw[first_chunk_at:first_chunk_at + 4])[0]
        clean_cut = raw[:first_chunk_at + 5 + first_len]
        check("truncation at an exact chunk boundary is still detected",
              "final chunk" in error_of(consume, clean_cut))

        tampered = bytearray(raw)
        tampered[first_chunk_at + 5 + 10] ^= 0x01
        check("flipping a ciphertext bit fails authentication",
              "authentication" in error_of(consume, bytes(tampered)))

        # Re-headering: claim no compression, keeping the wrapped key intact. The AAD
        # binds sha256(header), so every chunk must now fail.
        rewritten = bytearray(raw)
        body = raw[len(backups.MAGIC) + 4:len(backups.MAGIC) + 4 + head_len]
        swapped = body.replace(b'"gzip"', b'"none"')
        check("header tamper keeps the same length (test is meaningful)",
              len(swapped) == len(body))
        rewritten[len(backups.MAGIC) + 4:len(backups.MAGIC) + 4 + head_len] = swapped
        check("editing the header invalidates every chunk",
              raises(ValueError, consume, bytes(rewritten)))

        check("a non-FleetHub file is rejected by magic",
              "bad magic" in error_of(consume, b"SQLite format 3\x00" + b"\x00" * 200))

        empty = io.BytesIO()
        backups.write_envelope(iter([]), empty, master_key)
        check("an empty payload round trips", consume(empty.getvalue()) == b"")

        # ============================================================
        print("\n== Secret store ==")
        # ============================================================
        backups.store_secret(log_dir, master_key, "dest-1",
                             {"access_key_id": "AKID", "secret_access_key": "shh"})
        loaded = backups.load_secret(log_dir, master_key, "dest-1")
        check("secret round trips", loaded["secret_access_key"] == "shh")
        check("has_secret sees it", backups.has_secret(log_dir, "dest-1"))

        with open(backups.secrets_path(log_dir), "r", encoding="utf-8") as fh:
            on_disk = fh.read()
        check("the plaintext secret is NOT on disk", "shh" not in on_disk)

        check("a secret cannot be read under another destination's id",
              raises(ValueError, backups.load_secret, log_dir, master_key, "dest-2"))
        check("a different master key cannot read it",
              "different master key" in
              error_of(backups.load_secret, log_dir, other_key, "dest-1"))

        # Moving the blob to another destination id must fail -- the id is the AAD.
        import json as _json
        with open(backups.secrets_path(log_dir), "r", encoding="utf-8") as fh:
            store = _json.load(fh)
        store["dest-moved"] = store["dest-1"]
        with open(backups.secrets_path(log_dir), "w", encoding="utf-8") as fh:
            _json.dump(store, fh)
        check("a secret copied to another destination id will not decrypt",
              raises(ValueError, backups.load_secret, log_dir, master_key, "dest-moved"))

        backups.delete_secret(log_dir, "dest-1")
        check("delete removes it", not backups.has_secret(log_dir, "dest-1"))

        # ============================================================
        print("\n== Destination validation & CRUD ==")
        # ============================================================
        good_s3 = {"endpoint": "https://s3.example.com", "bucket": "backups",
                   "region": "eu-west-1", "prefix": "hub-a", "path_style": True}
        clean = backups.validate_destination(backups.KIND_S3, good_s3)
        check("valid S3 config is accepted", clean["bucket"] == "backups")
        check("unknown config keys are dropped",
              "nonsense" not in backups.validate_destination(
                  backups.KIND_S3, dict(good_s3, nonsense="x")))
        check("region defaults when omitted",
              backups.validate_destination(
                  backups.KIND_S3, dict(good_s3, region=""))["region"] == "us-east-1")
        check("plain http to a remote host is refused",
              "https" in error_of(backups.validate_destination, backups.KIND_S3,
                                  dict(good_s3, endpoint="http://s3.example.com")))
        check("plain http to localhost is allowed (MinIO in a lab)",
              backups.validate_destination(
                  backups.KIND_S3,
                  dict(good_s3, endpoint="http://localhost:9000"))["endpoint"]
              == "http://localhost:9000")
        check("a missing bucket is refused",
              raises(ValueError, backups.validate_destination, backups.KIND_S3,
                     dict(good_s3, bucket="")))
        check("a prefix with traversal characters is refused",
              raises(ValueError, backups.validate_destination, backups.KIND_S3,
                     dict(good_s3, prefix="../../etc")))
        check("an unknown kind is refused",
              raises(ValueError, backups.validate_destination, "ftp", {}))
        check("webdav needs a base URL",
              raises(ValueError, backups.validate_destination, backups.KIND_WEBDAV, {}))

        check("partial S3 credentials are refused",
              raises(ValueError, backups.validate_secret, backups.KIND_S3,
                     {"access_key_id": "AKID"}))
        check("empty credentials mean 'unchanged', not an error",
              backups.validate_secret(backups.KIND_S3, {}) is None)

        dest_id = backups.create_destination(
            db_path, log_dir, master_key, name="Offsite S3", kind=backups.KIND_S3,
            config=good_s3, secret={"access_key_id": "AKID", "secret_access_key": "shh"},
            actor="root@x.com")
        check("destination created", backups.get_destination(db_path, dest_id) is not None)
        check("credentials stored alongside it", backups.has_secret(log_dir, dest_id))
        check("a duplicate name is refused case-insensitively",
              raises(ValueError, backups.create_destination, db_path, log_dir, master_key,
                     name="offsite s3", kind=backups.KIND_S3, config=good_s3,
                     secret={"access_key_id": "A", "secret_access_key": "B"},
                     actor="root@x.com"))
        check("creating without credentials is refused",
              raises(ValueError, backups.create_destination, db_path, log_dir, master_key,
                     name="No creds", kind=backups.KIND_S3, config=good_s3, secret={},
                     actor="root@x.com"))

        listed = backups.list_destinations(db_path, log_dir)
        check("list reports credential presence without the credential",
              listed[0]["has_credentials"] is True and "secret" not in str(listed[0]))

        backups.update_destination(db_path, log_dir, master_key, dest_id,
                                   name="Offsite", secret={}, actor="root@x.com")
        check("update with an empty secret keeps the stored one",
              backups.load_secret(log_dir, master_key,
                                  dest_id)["secret_access_key"] == "shh")
        check("update renamed it",
              backups.get_destination(db_path, dest_id)["name"] == "Offsite")

        check("destination CRUD is audited",
              {"backup_destination_create", "backup_destination_update"} <=
              set(r["action"] for r in
                  fleet.get_conn(db_path).execute("SELECT action FROM audit_log")))

        # ============================================================
        print("\n== Keys and rotation ==")
        # ============================================================
        key_a = backups.object_key("hub-a", backups.BACKUP_HUB_DB, "x.fhb")
        check("hub keys nest under the prefix", key_a == "hub-a/hub-db/x.fhb")
        check("machine keys get a per-machine folder",
              backups.object_key("hub-a", backups.BACKUP_MACHINE_FILES, "x.fhb",
                                 machine="PC-1") == "hub-a/machines/PC-1/x.fhb")
        check("an empty prefix does not produce a leading slash",
              backups.object_key("", backups.BACKUP_HUB_DB, "x.fhb") == "hub-db/x.fhb")
        check("folder_key is the folder those live in",
              backups.folder_key("hub-a", backups.BACKUP_HUB_DB) == "hub-a/hub-db")

        early = backups.artifact_name(backups.BACKUP_HUB_DB, 1_700_000_000)
        later = backups.artifact_name(backups.BACKUP_HUB_DB, 1_700_090_000)
        check("artifact names sort chronologically as strings", early < later)
        check("artifact name carries the extension", later.endswith(".gz.fhb"))

        fake = FakeDestination()
        for stamp in range(1, 8):
            name = backups.artifact_name(backups.BACKUP_HUB_DB, 1_700_000_000 + stamp * 86400)
            fake.objects[backups.object_key("p", backups.BACKUP_HUB_DB, name)] = b"x"
        # Something that is not ours, in the same folder. Rotation must leave it alone.
        fake.objects["p/hub-db/notes.txt"] = b"hands off"

        removed = backups.rotate(fake, "p", backups.BACKUP_HUB_DB, keep=3)
        remaining = sorted(k for k in fake.objects if k.endswith(".fhb"))
        check("rotation removed the excess", len(removed) == 4)
        check("rotation kept exactly `keep` generations", len(remaining) == 3)
        check("rotation kept the NEWEST generations",
              remaining[-1].endswith(backups.artifact_name(
                  backups.BACKUP_HUB_DB, 1_700_000_000 + 7 * 86400)))
        check("rotation ignores files it did not write",
              "p/hub-db/notes.txt" in fake.objects)
        check("keep=0 is refused rather than emptying the bucket",
              raises(ValueError, backups.rotate, fake, "p", backups.BACKUP_HUB_DB, keep=0))

        # ============================================================
        print("\n== Snapshot ==")
        # ============================================================
        snap = os.path.join(workdir, "snap.db")
        size = backups.snapshot_database(db_path, snap)
        check("snapshot has a size", size > 0)
        check("snapshot has no -wal sidecar", not os.path.exists(snap + "-wal"))
        rows, integrity = sqlite_facts(snap)
        check("snapshot carries the data", rows == 200)
        check("snapshot passes an integrity check", integrity == "ok")
        # VACUUM INTO refuses an existing target; snapshot_database clears it first, so a
        # second run in the same place must work rather than raise.
        check("re-snapshotting over an existing file works",
              backups.snapshot_database(db_path, snap) > 0)

        # ============================================================
        print("\n== End to end: backup, then restore ==")
        # ============================================================
        bucket = FakeDestination()
        real_build = backups.build_client
        backups.build_client = lambda record, secret: bucket
        try:
            run = backups.backup_hub_database(
                db_path, log_dir, dest_id, keep=2, trigger=backups.TRIGGER_MANUAL,
                actor="root@x.com", now=1_700_100_000, hub_version="1.28.0")
            check("run succeeded", run["status"] == backups.RUN_SUCCEEDED)
            check("run recorded the object key",
                  run["object_key"].startswith("hub-a/hub-db/"))
            check("run recorded both sizes",
                  run["source_bytes"] > 0 and run["stored_bytes"] > 0)
            check("run recorded the artifact digest",
                  len(run["artifact_sha256"] or "") == 64)
            check("the object actually landed", run["object_key"] in bucket.objects)
            check("upload happened before rotation",
                  [c[0] for c in bucket.calls].index("put") <
                  [c[0] for c in bucket.calls].index("list"))

            # The assertion the whole feature exists for.
            uploaded = bucket.objects[run["object_key"]]
            header, chunks = backups.read_envelope(io.BytesIO(uploaded), master_key)
            check("uploaded artifact names the hub version that wrote it",
                  header["hub_version"] == "1.28.0")
            restored_path = os.path.join(workdir, "restored.db")
            with open(restored_path, "wb") as out:
                for block in backups.iter_gunzip(chunks):
                    out.write(block)
            restored_rows, restored_integrity = sqlite_facts(restored_path)
            check("restored database opens and is intact", restored_integrity == "ok")
            check("restored database has every row", restored_rows == 200)

            check("no plaintext snapshot is left behind",
                  not os.path.exists(os.path.join(log_dir, "backup-work",
                                                  f"snapshot-{run['id']}.db")))
            check("no artifact temp file is left behind",
                  [] == [f for f in os.listdir(os.path.join(log_dir, "backup-work"))])

            # Two more runs, so rotation has something to do at keep=2.
            backups.backup_hub_database(db_path, log_dir, dest_id, keep=2,
                                        now=1_700_200_000)
            backups.backup_hub_database(db_path, log_dir, dest_id, keep=2,
                                        now=1_700_300_000)
            check("rotation held the bucket at `keep` generations",
                  len([k for k in bucket.objects if k.endswith(".fhb")]) == 2)

            # ============================================================
            print("\n== Failure is recorded, not raised ==")
            # ============================================================
            class BrokenDestination(FakeDestination):
                def put(self, key, fileobj, size, sha256_hex):
                    raise backups.BackupError("HTTP 403 -- SignatureDoesNotMatch")

            broken = BrokenDestination()
            backups.build_client = lambda record, secret: broken
            failed = backups.backup_hub_database(db_path, log_dir, dest_id, keep=2,
                                                 now=1_700_400_000)
            check("a provider failure lands as a failed run, not an exception",
                  failed["status"] == backups.RUN_FAILED)
            check("the provider's own message is kept verbatim",
                  "SignatureDoesNotMatch" in failed["error"])
            check("a failed run still cleans up its temp files",
                  [] == os.listdir(os.path.join(log_dir, "backup-work")))
            check("failure is audited",
                  "backup_hub_db_failed" in
                  [r["action"] for r in fleet.get_conn(db_path).execute(
                      "SELECT action FROM audit_log")])

            missing = backups.backup_hub_database(db_path, log_dir, "no-such-id",
                                                  now=1_700_410_000)
            check("an unknown destination fails the run cleanly",
                  missing["status"] == backups.RUN_FAILED and
                  "no longer exists" in missing["error"])

            # ============================================================
            print("\n== Scheduler ==")
            # ============================================================
            backups.build_client = lambda record, secret: bucket
            backups.set_state(db_path, backups.LAST_ATTEMPT_STATE_KEY, 1_700_500_000)

            check("disabled means no run",
                  backups.tick(db_path, log_dir, enabled=False,
                               destination_id=dest_id, interval_hours=24, keep=2,
                               now=1_700_600_000) is None)
            check("no destination means no run",
                  backups.tick(db_path, log_dir, enabled=True, destination_id="",
                               interval_hours=24, keep=2, now=1_700_600_000) is None)
            check("not yet due means no run",
                  backups.tick(db_path, log_dir, enabled=True, destination_id=dest_id,
                               interval_hours=24, keep=2,
                               now=1_700_500_000 + 23 * 3600) is None)

            due = backups.tick(db_path, log_dir, enabled=True, destination_id=dest_id,
                               interval_hours=24, keep=2,
                               now=1_700_500_000 + 24 * 3600)
            check("due means a run", due is not None and
                  due["status"] == backups.RUN_SUCCEEDED)
            check("a scheduled run is labelled as such",
                  due["trigger"] == backups.TRIGGER_SCHEDULE)
            check("the next tick right after is not due again",
                  backups.tick(db_path, log_dir, enabled=True, destination_id=dest_id,
                               interval_hours=24, keep=2,
                               now=1_700_500_000 + 24 * 3600 + 60) is None)

            # A failing destination must still push the clock forward, or a broken
            # endpoint gets hammered once per tick forever.
            backups.build_client = lambda record, secret: broken
            attempt_before = backups.get_state(db_path, backups.LAST_ATTEMPT_STATE_KEY)
            failing_tick = backups.tick(db_path, log_dir, enabled=True,
                                        destination_id=dest_id, interval_hours=1, keep=2,
                                        now=int(attempt_before) + 3600)
            check("a failing scheduled run still happened",
                  failing_tick["status"] == backups.RUN_FAILED)
            check("a failure advances the attempt clock (no retry storm)",
                  backups.tick(db_path, log_dir, enabled=True, destination_id=dest_id,
                               interval_hours=1, keep=2,
                               now=int(attempt_before) + 3660) is None)

            # ============================================================
            print("\n== Concurrency ==")
            # ============================================================
            backups._RUN_LOCK.acquire()
            try:
                check("backup_in_progress reports the lock",
                      backups.backup_in_progress() is True)
                busy = backups.backup_hub_database(db_path, log_dir, dest_id,
                                                   now=1_700_900_000)
                check("a second concurrent backup returns None", busy is None)
                runs_now = backups.list_runs(db_path, limit=200)
                check("and leaves no run row behind",
                      not any(r["started_at"] == 1_700_900_000 for r in runs_now))
            finally:
                backups._RUN_LOCK.release()
        finally:
            backups.build_client = real_build

        # ============================================================
        print("\n== Run history ==")
        # ============================================================
        runs = backups.list_runs(db_path, limit=5)
        check("runs come back newest first",
              all(runs[i]["started_at"] >= runs[i + 1]["started_at"]
                  for i in range(len(runs) - 1)))
        check("runs resolve their destination name",
              runs[0]["destination_name"] == "Offsite")

        backups.delete_destination(db_path, log_dir, dest_id, actor="root@x.com")
        check("deleting a destination removes its stored credentials",
              not backups.has_secret(log_dir, dest_id))
        surviving = backups.list_runs(db_path, limit=5)
        check("run history survives its destination being deleted", len(surviving) > 0)
        check("an orphaned run reports no destination name",
              surviving[0]["destination_name"] is None)

        backups.prune_runs(db_path, keep=2)
        check("prune_runs caps the history",
              len(backups.list_runs(db_path, limit=100)) == 2)

    finally:
        if saved_env is None:
            os.environ.pop(backups.MASTER_KEY_ENV, None)
        else:
            os.environ[backups.MASTER_KEY_ENV] = saved_env
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


def test_backups():
    main()


if __name__ == "__main__":
    sys.exit(main())
