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
import base64
import io
import json
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


def audit_actions(db_path):
    with fleet.get_conn(db_path) as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_log")]


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


def command_params(db_path, machine, command_type):
    """The params of the newest queued command of a type. fleet.list_commands
    deliberately omits params_json (it feeds a list view), so read it directly."""
    with fleet.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT params_json FROM commands WHERE machine = ? AND type = ? "
            "ORDER BY created_at DESC LIMIT 1", (machine, command_type)).fetchone()
    return json.loads(row["params_json"]) if row else None


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
        print("\n== Cross-implementation envelope fixture ==")
        # ============================================================
        # The FHBK1 format has two implementations: this one and the agent's
        # BackupEnvelope.cs. Each suite decrypting only its own output would pass happily
        # if both were wrong in the same way, so the fixture is exchanged BOTH ways --
        # the C# tests read the artifacts generated here, and this reads the one they
        # write. Neither can drift alone.
        fixture_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        fixture_meta = os.path.join(fixture_dir, "envelope.json")
        if os.path.exists(fixture_meta):
            with open(fixture_meta, "r", encoding="utf-8") as fh:
                fx = json.load(fh)
            fx_master = backups.decode_master_key(fx["master_key_b64"])
            fx_plain = base64.b64decode(fx["plaintext_b64"])

            for name in ("envelope-hub", "envelope-machine"):
                path = os.path.join(fixture_dir, name + backups.FILE_EXTENSION)
                with open(path, "rb") as fh:
                    _, chunks = backups.read_envelope(fh, fx_master)
                    check(f"{name} fixture still round-trips",
                          b"".join(backups.iter_gunzip(chunks)) == fx_plain)

            check("the fixture's machine key matches this module's derivation",
                  base64.b64decode(fx["machine_key_b64"])
                  == backups.derive_machine_key(fx_master, fx["machine"]))

            # Written by the agent's BackupEnvelopeTests. Absent until `dotnet test` has
            # run, which is normal on a Python-only check-out -- reported rather than
            # failed, because a missing file here means "not run", not "broken".
            from_agent = os.path.join(fixture_dir, "from-agent.fhb")
            if os.path.exists(from_agent):
                with open(from_agent, "rb") as fh:
                    header, chunks = backups.read_envelope(fh, fx_master)
                    body = b"".join(backups.iter_gunzip(chunks))
                check("an artifact sealed BY THE AGENT decrypts here",
                      body.startswith(b"sealed by the agent"))
                check("...and names the machine it was sealed for",
                      header["machine"] == fx["machine"])
                check("...and was written by the agent's implementation",
                      header.get("written_by") == "agent-tests")
            else:
                print("  [--] from-agent.fhb absent; run `dotnet test` in agent/ to "
                      "verify the C# writer")

            # THE restore contract: a machine archive is tar inside the envelope, so
            # restore_backup.py can unpack it with stdlib tarfile and no hub, no agent.
            # A C#-only assertion cannot catch a tar Python refuses to read.
            agent_archive = os.path.join(fixture_dir, "from-agent-archive.fhb")
            if os.path.exists(agent_archive):
                import tarfile
                with open(agent_archive, "rb") as fh:
                    _, chunks = backups.read_envelope(fh, fx_master)
                    tar_bytes = io.BytesIO(b"".join(backups.iter_gunzip(chunks)))
                with tarfile.open(fileobj=tar_bytes, mode="r:") as tar:
                    names = tar.getnames()
                    manifest_member = tar.extractfile("manifest.json")
                    manifest_json = json.loads(manifest_member.read().decode("utf-8"))
                check("an agent-written ARCHIVE unpacks with stdlib tarfile",
                      len(names) == 4)
                check("...with manifest.json first, so it can be listed without unpacking",
                      names[0] == "manifest.json")
                check("...and the manifest describes the files inside",
                      len(manifest_json["files"]) == 3)
                check("...stored under portable names (no drive colon, no backslash)",
                      all(":" not in n and "\\" not in n for n in names[1:]))
        else:
            print("  [--] tests/fixtures absent; run tests/make_envelope_fixture.py")

        # ============================================================
        print("\n== Per-machine key derivation ==")
        # ============================================================
        # The blast-radius rule for roadmap #1b: an agent holds the key it encrypts with,
        # so that key must NOT be the one that also opens the hub database backup.
        pc1 = backups.derive_machine_key(master_key, "PC-1")
        pc2 = backups.derive_machine_key(master_key, "PC-2")
        check("a derived key is 32 bytes", len(pc1) == 32)
        check("a derived key is NOT the master key", pc1 != master_key)
        check("different machines get different keys", pc1 != pc2)
        check("derivation is deterministic",
              pc1 == backups.derive_machine_key(master_key, "PC-1"))
        check("machine names are matched case-insensitively",
              pc1 == backups.derive_machine_key(master_key, "pc-1"))
        check("a different master key derives differently",
              pc1 != backups.derive_machine_key(other_key, "PC-1"))
        check("machine_key_for uses the configured master key",
              backups.machine_key_for("PC-1") == pc1)

        # A machine archive must open with the MASTER key alone -- restore_backup.py is
        # never told which machine a file came from, it reads that from the header.
        machine_artifact = io.BytesIO()
        backups.write_envelope(
            backups.iter_gzip(backups.iter_file(io.BytesIO(b"bob's documents"))),
            machine_artifact, pc1,
            header_extra={"kind": backups.BACKUP_MACHINE_FILES, "machine": "PC-1"})
        header, chunks = backups.read_envelope(io.BytesIO(machine_artifact.getvalue()),
                                               master_key)
        check("the master key opens a machine archive by re-deriving",
              b"".join(backups.iter_gunzip(chunks)) == b"bob's documents")
        check("the header names the machine it was sealed for",
              header["machine"] == "PC-1")
        header2, chunks2 = backups.read_envelope(
            io.BytesIO(machine_artifact.getvalue()), pc1)
        check("...and the derived key still opens it directly",
              b"".join(backups.iter_gunzip(chunks2)) == b"bob's documents")
        check("PC-2's key does NOT open PC-1's archive",
              raises(ValueError, backups.read_envelope,
                     io.BytesIO(machine_artifact.getvalue()), pc2))

        # ============================================================
        print("\n== Per-machine file backup config ==")
        # ============================================================
        absent = backups.get_machine_config(db_path, "PC-1")
        check("a machine with no row follows the fleet", absent["enabled"] is None)
        check("...and has no extra paths", absent["include"] == [])
        check("...and is not listed as an exception",
              backups.list_machine_configs(db_path) == [])

        fleet_defaults = dict(fleet_enabled=True, fleet_destination=dest_id,
                              fleet_include=["%Desktop%"], fleet_exclude=["*.tmp"])
        eff = backups.effective_file_config(db_path, "PC-1", **fleet_defaults)
        check("an unconfigured machine inherits the fleet policy",
              eff["enabled"] is True and eff["include"] == ["%Desktop%"])
        check("...and the fleet destination", eff["destination_id"] == dest_id)
        check("nothing is marked overridden", eff["overridden"] == {
            "enabled": False, "destination_id": False})

        backups.set_machine_config(db_path, "PC-1", include=["%Users%\\Projects"],
                                   exclude=["*.iso"], actor="root@x.com")
        eff = backups.effective_file_config(db_path, "PC-1", **fleet_defaults)
        check("per-machine paths are ADDED to the fleet list, not replacing it",
              eff["include"] == ["%Desktop%", "%Users%\\Projects"])
        check("...and the same for excludes",
              eff["exclude"] == ["*.tmp", "*.iso"])
        check("a machine with overrides IS listed as an exception",
              [c["machine"] for c in backups.list_machine_configs(db_path)] == ["PC-1"])

        # A per-machine entry duplicating a fleet one must not double the walk.
        backups.set_machine_config(db_path, "PC-1", include=["%Desktop%"],
                                   actor="root@x.com")
        check("a duplicate of a fleet path collapses to one",
              backups.effective_file_config(
                  db_path, "PC-1", **fleet_defaults)["include"] == ["%Desktop%"])

        backups.set_machine_config(db_path, "PC-1", enabled=False, actor="root@x.com")
        eff = backups.effective_file_config(db_path, "PC-1", **fleet_defaults)
        check("a machine can opt out of a fleet-enabled policy", eff["enabled"] is False)
        check("...and that reads as an override", eff["overridden"]["enabled"] is True)
        check("toggling enabled leaves the path lists alone",
              backups.get_machine_config(db_path, "PC-1")["include"] == ["%Desktop%"])

        check("a bad path pattern is refused at the machine level",
              raises(ValueError, backups.set_machine_config, db_path, "PC-1",
                     include=["%Nonsense%"], actor="root@x.com"))
        check("a destination that does not exist is refused",
              raises(ValueError, backups.set_machine_config, db_path, "PC-1",
                     destination_id="no-such", actor="root@x.com"))
        check("machine config changes are audited",
              "backup_machine_config" in audit_actions(db_path))

        print("\n-- reported profiles --")
        vectors_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "backup_path_vectors.json")
        with open(vectors_path, "r", encoding="utf-8") as fh:
            sample_profiles = json.load(fh)["profiles"]
        check("profiles are recorded",
              backups.record_profiles(db_path, "PC-1", sample_profiles) is True)
        stored = backups.get_machine_config(db_path, "PC-1")["profiles"]
        check("...and read back", [u["name"] for u in stored["users"]][:2]
              == ["bob", "carol"])
        check("recording profiles does NOT disturb the operator's overrides",
              backups.get_machine_config(db_path, "PC-1")["include"] == ["%Desktop%"])
        check("an empty profile payload is ignored rather than stored",
              backups.record_profiles(db_path, "PC-9", {}) is False)
        check("...and creates no row for that machine",
              backups.get_machine_config(db_path, "PC-9")["profiles"] is None)

        # Reporting profiles CREATES a row, which must not make a machine look like it
        # has been deliberately configured -- otherwise the "machines with their own
        # settings" list is every machine in the fleet, and the handful that really are
        # exceptions are invisible in it.
        backups.record_profiles(db_path, "PC-REPORTED", sample_profiles)
        listed = [c["machine"] for c in backups.list_machine_configs(db_path)]
        check("a machine that only reported profiles is NOT an exception",
              "PC-REPORTED" not in listed)
        check("...but a machine with a real override still is", "PC-1" in listed)
        check("its row does exist when asked for all of them",
              "PC-REPORTED" in [c["machine"] for c in
                                backups.list_machine_configs(db_path,
                                                             overrides_only=False)])
        check("has_overrides is False for a profile-only row",
              backups.has_overrides(
                  backups.get_machine_config(db_path, "PC-REPORTED")) is False)

        # Agent-supplied and lands in the database, so it is capped.
        huge = {"users": [{"name": f"u{i}", "path": f"C:\\Users\\u{i}", "folders": {}}
                          for i in range(500)]}
        backups.record_profiles(db_path, "PC-CAP", huge)
        check("an implausible profile list is truncated, not stored whole",
              len(backups.get_machine_config(db_path, "PC-CAP")["profiles"]["users"])
              == 64)

        print("\n-- machine lifecycle --")
        backups.set_machine_config(db_path, "OLD-PC", include=["%Users%\\Legacy"],
                                   actor="root@x.com")
        backups.rename_machine(db_path, "OLD-PC", "NEW-PC")
        check("a renamed machine keeps its backup config",
              backups.get_machine_config(db_path, "NEW-PC")["include"]
              == ["%Users%\\Legacy"])
        check("...and the old name is gone",
              backups.get_machine_config(db_path, "OLD-PC")["include"] == [])

        # A merge into a machine that already has its own config must not clobber it.
        backups.set_machine_config(db_path, "MERGE-SRC", include=["%Users%\\Src"],
                                   actor="root@x.com")
        backups.rename_machine(db_path, "MERGE-SRC", "NEW-PC")
        check("a merge does not overwrite the survivor's own config",
              backups.get_machine_config(db_path, "NEW-PC")["include"]
              == ["%Users%\\Legacy"])
        check("...and drops the merged-away row",
              backups.get_machine_config(db_path, "MERGE-SRC")["include"] == [])

        backups.forget_machine(db_path, "NEW-PC")
        check("a deleted machine's config is dropped",
              backups.get_machine_config(db_path, "NEW-PC")["include"] == [])

        # ============================================================
        print("\n== File-backup chains and the manifest ==")
        # ============================================================
        # The dangerous property of incrementals: one is useless without its full. These
        # checks exist because "rotation deleted the base of a chain" is a failure nobody
        # notices until a restore, and by then the data is gone.
        chain_db = os.path.join(workdir, "chains.db")
        fleet.init_fleet_db(chain_db)
        backups.init_backups_db(chain_db)

        plan = backups.plan_next_run(chain_db, "PC-A", full_every=3)
        check("the first run of a machine is a full", plan["full"] is True)
        check("...at sequence 0", plan["sequence"] == 0)

        def record(machine, plan, files, key_suffix=""):
            return backups.record_file_set(
                chain_db, run_id=uuid_hex(), machine=machine,
                chain_id=plan["chain_id"], sequence=plan["sequence"],
                object_key=f"p/machines/{machine}/{plan['chain_id']}-{plan['sequence']}{key_suffix}.fhb",
                stored_bytes=1000 + plan["sequence"], files=files)

        record("PC-A", plan, [
            {"path": "C:\\Users\\bob\\a.txt", "size": 10, "mtime": 1, "sha256": "aa"},
            {"path": "C:\\Users\\bob\\b.txt", "size": 20, "mtime": 1, "sha256": "bb"},
        ])
        plan2 = backups.plan_next_run(chain_db, "PC-A", full_every=3)
        check("the second run extends the same chain",
              plan2["chain_id"] == plan["chain_id"] and plan2["full"] is False)
        check("...at sequence 1", plan2["sequence"] == 1)

        # b.txt changes, c.txt appears, a.txt is untouched (so absent from the increment).
        record("PC-A", plan2, [
            {"path": "C:\\Users\\bob\\b.txt", "size": 25, "mtime": 2, "sha256": "bb2"},
            {"path": "C:\\Users\\bob\\c.txt", "size": 30, "mtime": 2, "sha256": "cc"},
        ])
        manifest = {m["path"]: m for m in backups.current_manifest(chain_db, "PC-A")}
        check("the manifest carries files from across the chain", len(manifest) == 3)
        check("an unchanged file resolves to the FULL's archive",
              manifest["C:\\Users\\bob\\a.txt"]["sequence"] == 0)
        check("a changed file resolves to the NEWEST version",
              manifest["C:\\Users\\bob\\b.txt"]["sha256"] == "bb2")
        check("...from the incremental that holds it",
              manifest["C:\\Users\\bob\\b.txt"]["sequence"] == 1)
        check("the manifest names the archive each file lives in",
              all(m["object_key"] for m in manifest.values()))

        plan3 = backups.plan_next_run(chain_db, "PC-A", full_every=3)
        record("PC-A", plan3, [
            {"path": "C:\\Users\\bob\\a.txt", "deleted": True},
        ])
        manifest = {m["path"]: m for m in backups.current_manifest(chain_db, "PC-A")}
        check("a deleted file drops out of the current manifest",
              "C:\\Users\\bob\\a.txt" not in manifest)
        check("...without disturbing the others", len(manifest) == 2)

        plan4 = backups.plan_next_run(chain_db, "PC-A", full_every=3)
        check("a chain at full_every forces a new full", plan4["full"] is True)
        check("...under a NEW chain id", plan4["chain_id"] != plan["chain_id"])
        check("...at sequence 0 again", plan4["sequence"] == 0)

        check("an incremental whose full was never recorded is REFUSED",
              raises(ValueError, backups.record_file_set, chain_db,
                     run_id=uuid_hex(), machine="PC-A", chain_id="orphan-chain",
                     sequence=1, object_key="p/x.fhb", stored_bytes=1, files=[]))

        print("\n-- chain-aware rotation --")
        rot_db = os.path.join(workdir, "rotate.db")
        fleet.init_fleet_db(rot_db)
        backups.init_backups_db(rot_db)
        bucket2 = FakeDestination()
        chain_ids = []
        for c in range(4):                       # four chains, each a full + 2 increments
            chain_id = None
            for seq in range(3):
                p = (backups.plan_next_run(rot_db, "PC-B", full_every=3)
                     if seq == 0 else {"chain_id": chain_id, "sequence": seq, "full": False})
                chain_id = p["chain_id"]
                key = f"p/machines/PC-B/{chain_id}-{seq}.fhb"
                bucket2.objects[key] = b"x"
                backups.record_file_set(rot_db, run_id=uuid_hex(), machine="PC-B",
                                        chain_id=chain_id, sequence=seq, object_key=key,
                                        stored_bytes=1,
                                        files=[{"path": f"C:\\f{c}-{seq}.txt",
                                                "sha256": f"h{c}{seq}"}])
            chain_ids.append(chain_id)

        check("four chains exist", len(backups.machine_chains(rot_db, "PC-B")) == 4)
        removed = backups.rotate_chains(bucket2, "p", "PC-B", keep_chains=2, db_path=rot_db)
        check("rotation removed two WHOLE chains", len(removed) == 6)
        surviving = backups.machine_chains(rot_db, "PC-B")
        check("two chains survive", len(surviving) == 2)
        check("every surviving chain still has its full",
              all(c["complete"] for c in surviving))
        check("every surviving chain kept all three archives",
              all(len(c["sets"]) == 3 for c in surviving))
        check("the NEWEST chains are the ones kept",
              {c["chain_id"] for c in surviving} == set(chain_ids[2:]))
        check("the deleted chains' objects are gone from the bucket",
              not any(chain_ids[0] in k for k in bucket2.objects))
        check("the deleted chains' manifest rows are gone too",
              all(chain_ids[0] not in m["object_key"]
                  for m in backups.current_manifest(rot_db, "PC-B")))
        check("keep_chains=0 is refused rather than deleting everything",
              raises(ValueError, backups.rotate_chains, bucket2, "p", "PC-B", 0, rot_db))
        check("rotation with nothing to do is a no-op",
              backups.rotate_chains(bucket2, "p", "PC-B", 5, rot_db) == [])

        print("\n-- rotation survives a delete that fails halfway --")
        # The bug this replaced: every object was deleted and THEN every row, so a failure
        # in between left the manifest promising archives that no longer existed. What
        # makes the fix work is the ORDER -- newest sequence first, the full last -- so a
        # partial delete leaves a shorter but still-restorable chain rather than orphaned
        # incrementals.
        part_db = os.path.join(workdir, "rotate-partial.db")
        fleet.init_fleet_db(part_db)
        backups.init_backups_db(part_db)

        class FlakyDestination(FakeDestination):
            """Refuses to delete one specific key, like a bucket policy change mid-pass."""

            def __init__(self, doomed_key):
                super().__init__()
                self.doomed_key = doomed_key

            def delete(self, key):
                if key == self.doomed_key:
                    raise backups.BackupError("access denied")
                super().delete(key)

        part_chains = []
        for c in range(3):
            chain_id = None
            for seq in range(3):
                p = (backups.plan_next_run(part_db, "PC-P", full_every=3)
                     if seq == 0 else {"chain_id": chain_id, "sequence": seq, "full": False})
                chain_id = p["chain_id"]
                key = f"p/machines/PC-P/{chain_id}-{seq}.fhb"
                backups.record_file_set(part_db, run_id=uuid_hex(), machine="PC-P",
                                        chain_id=chain_id, sequence=seq, object_key=key,
                                        stored_bytes=1,
                                        files=[{"path": f"C:\\p{c}-{seq}.txt",
                                                "sha256": f"h{c}{seq}"}])
            part_chains.append(chain_id)

        oldest = part_chains[0]
        flaky = FlakyDestination(f"p/machines/PC-P/{oldest}-1.fhb")
        for c in part_chains:
            for seq in range(3):
                flaky.objects[f"p/machines/PC-P/{c}-{seq}.fhb"] = b"x"

        check("a delete failure still propagates to the caller",
              raises(backups.BackupError, backups.rotate_chains,
                     flaky, "p", "PC-P", 2, part_db))
        deleted_order = [k for verb, k in flaky.calls if verb == "delete"]
        check("archives are deleted newest sequence first, the full LAST",
              deleted_order[0].endswith("-2.fhb"))
        check("the full of the failed chain was NOT deleted",
              f"p/machines/PC-P/{oldest}-0.fhb" in flaky.objects)
        survivors = {c["chain_id"]: c for c in backups.machine_chains(part_db, "PC-P")}
        check("the partly-deleted chain is still listed", oldest in survivors)
        check("...as a SHORTER chain matching what storage actually holds",
              len(survivors[oldest]["sets"]) == 2)
        check("...and still restorable, because its full survived",
              survivors[oldest]["complete"] is True)
        check("no manifest row points at an archive that was deleted",
              all(m["object_key"] in flaky.objects
                  for m in backups.current_manifest(part_db, "PC-P")))
        # The chain is still over the limit, so the next pass finishes the job -- which is
        # why no list of pending deletions has to be kept anywhere.
        flaky.doomed_key = None
        backups.rotate_chains(flaky, "p", "PC-P", 2, part_db)
        check("a later pass finishes what the failed one started",
              len(backups.machine_chains(part_db, "PC-P")) == 2)

        print("\n-- run lifecycle --")
        run_plan = backups.plan_next_run(rot_db, "PC-C", full_every=7)
        run_id = backups.start_file_run(rot_db, "PC-C", dest_id, run_plan,
                                        backups.TRIGGER_SCHEDULE, "scheduler",
                                        1_800_000_000)
        opened = backups.get_run(rot_db, run_id)
        check("a machine run opens as running",
              opened["status"] == backups.RUN_RUNNING)
        check("...recording its chain and sequence",
              opened["chain_id"] == run_plan["chain_id"] and opened["sequence"] == 0)
        check("...and is machine-scoped", opened["machine"] == "PC-C")
        check("it is due immediately when never run",
              backups.files_due_at(rot_db, "PC-NEVER", 24) == 0)
        check("...and not due again right after an attempt",
              backups.files_due_at(rot_db, "PC-C", 24) == 1_800_000_000 + 24 * 3600)

        backups.complete_file_run(rot_db, run_id, object_key="p/x.fhb",
                                  stored_bytes=500, file_count=42)
        done = backups.get_run(rot_db, run_id)
        check("completing marks it succeeded", done["status"] == backups.RUN_SUCCEEDED)
        check("...with the file count", done["file_count"] == 42)

        # A machine that goes offline mid-backup must not sit `running` forever: because
        # due-ness anchors on the last ATTEMPT, that machine would silently never be
        # backed up again.
        stuck = backups.start_file_run(rot_db, "PC-D", dest_id, run_plan,
                                       backups.TRIGGER_SCHEDULE, "scheduler",
                                       1_800_000_000)
        check("a fresh run is not expired",
              backups.expire_stale_file_runs(rot_db, now=1_800_000_100) == 0)
        check("a run older than the limit IS expired",
              backups.expire_stale_file_runs(
                  rot_db, now=1_800_000_000 + 25 * 3600) == 1)
        check("...and says why",
              "never reported" in backups.get_run(rot_db, stuck)["error"])

        # ============================================================
        print("\n== Per-PC scheduler, end to end ==")
        # ============================================================
        sched_db = os.path.join(workdir, "sched.db")
        sched_log = os.path.join(workdir, "schedlogs")
        os.makedirs(sched_log, exist_ok=True)
        fleet.init_fleet_db(sched_db)
        backups.init_backups_db(sched_db)
        sched_dest = backups.create_destination(
            sched_db, sched_log, master_key, name="Files", kind=backups.KIND_S3,
            config=good_s3, secret={"access_key_id": "AKID", "secret_access_key": "shh"},
            actor="root@x.com")

        policy = dict(fleet_enabled=True, fleet_destination=sched_dest,
                      fleet_include=["%Desktop%"], fleet_exclude=["*.tmp"],
                      interval_hours=24, full_every=3,
                      limits={"max_file_mb": 2048, "max_set_gb": 100, "use_vss": True})

        check("a machine with the fleet policy off is not dispatched",
              backups.files_dispatch_once(
                  sched_db, sched_log, machines=["PC-A"], now=1_900_000_000,
                  **dict(policy, fleet_enabled=False)) == 0)
        backups.set_machine_config(sched_db, "PC-OUT", enabled=False, actor="root@x.com")
        check("a machine that opted out is not dispatched",
              backups.files_dispatch_once(sched_db, sched_log, machines=["PC-OUT"],
                                          now=1_900_000_000, **policy) == 0)

        dispatched = backups.files_dispatch_once(
            sched_db, sched_log, machines=["PC-A", "PC-B"], now=1_900_000_000, **policy)
        check("due machines are dispatched", dispatched == 2)

        queued = fleet.list_commands(sched_db, machine="PC-A")
        check("a backup_files command was queued",
              any(cmd["type"] == "backup_files" for cmd in queued))
        params = command_params(sched_db, "PC-A", "backup_files")
        check("the command carries the resolved include list",
              params["include"] == ["%Desktop%"])
        check("...and the first run is a full",
              params["full"] is True and params["sequence"] == 0)
        check("...and the machine's own object key",
              params["object_key"].startswith("hub-a/machines/PC-A/"))
        check("...and a pre-signed S3 upload URL, not a credential",
              params["upload"]["kind"] == "s3"
              and "X-Amz-Signature=" in params["upload"]["url"])
        check("the upload URL is scoped to THIS machine's object",
              "/machines/PC-A/" in params["upload"]["url"])
        check("the S3 secret key never appears in the params",
              "shh" not in json.dumps(params))
        # Without this the agent has nowhere to POST its manifest: an S3 pre-signed URL
        # carries no run id, so it cannot be recovered from the upload target.
        check("the params carry the run id the agent reports against",
              params["run_id"] == [r for r in backups.list_runs(
                  sched_db, limit=10, kind=backups.BACKUP_MACHINE_FILES)
                  if r["machine"] == "PC-A"][0]["id"])

        # THE blast-radius property: the agent gets a derived key, never the master.
        agent_key = base64.b64decode(params["encryption"]["key"])
        check("the agent is given a DERIVED key",
              agent_key == backups.derive_machine_key(master_key, "PC-A"))
        check("...which is not the master key", agent_key != master_key)
        check("...and does not open another machine's archive",
              agent_key != backups.derive_machine_key(master_key, "PC-B"))

        check("a second pass does not re-dispatch a machine already running",
              backups.files_dispatch_once(sched_db, sched_log,
                                          machines=["PC-A"], now=1_900_000_060,
                                          **policy) == 0)

        run = [r for r in backups.list_runs(sched_db, limit=10,
                                            kind=backups.BACKUP_MACHINE_FILES)
               if r["machine"] == "PC-A"][0]
        check("the run row is open before the command is queued",
              run["status"] == backups.RUN_RUNNING)
        check("...and remembers the command carrying it", run["command_id"])

        files_bucket = FakeDestination()
        saved_build = backups.build_client
        backups.build_client = lambda record, secret: files_bucket
        finished = backups.ingest_file_result(sched_db, sched_log, run["id"], {
            "stored_bytes": 4096,
            "files": [
                {"path": "C:\\Users\\bob\\Desktop\\a.txt", "size": 10, "mtime": 1,
                 "sha256": "aa"},
                {"path": "C:\\Users\\bob\\Desktop\\b.txt", "size": 20, "mtime": 1,
                 "sha256": "bb"},
            ],
        }, keep_chains=2)
        check("reporting a result closes the run",
              finished["status"] == backups.RUN_SUCCEEDED)
        check("...with the file count", finished["file_count"] == 2)
        check("the manifest is populated",
              len(backups.current_manifest(sched_db, "PC-A")) == 2)
        check("the archive is recorded under the key the HUB minted",
              backups.machine_chains(sched_db, "PC-A")[0]["sets"][0]["object_key"]
              == run["object_key"])
        check("the machine is now due later, not immediately",
              backups.files_due_at(sched_db, "PC-A", 24) > 1_900_000_000)
        check("the ingest is audited", "backup_files" in audit_actions(sched_db))

        check("a repeat report is ignored rather than double-recording",
              backups.ingest_file_result(sched_db, sched_log, run["id"],
                                         {"stored_bytes": 1, "files": []},
                                         keep_chains=2)["file_count"] == 2)

        # An agent that reports a failure must close the run, or the machine is never
        # due again and silently stops being backed up.
        run_b = [r for r in backups.list_runs(sched_db, limit=10,
                                              kind=backups.BACKUP_MACHINE_FILES)
                 if r["machine"] == "PC-B"][0]
        failed_run = backups.ingest_file_result(
            sched_db, sched_log, run_b["id"], {"error": "VSS snapshot failed"},
            keep_chains=2)
        check("an agent-reported failure closes the run",
              failed_run["status"] == backups.RUN_FAILED)
        check("...keeping the agent's own words", failed_run["error"] == "VSS snapshot failed")
        check("...and records no archive",
              backups.machine_chains(sched_db, "PC-B") == [])
        backups.build_client = saved_build

        # ============================================================
        print("\n== Offline machines: catch up rather than lose a night ==")
        # ============================================================
        # The property under test is not "offline machines are skipped" -- it is that
        # skipping them costs nothing. Dispatching to a machine that cannot answer used
        # to stamp a run row, and because files_due_at anchors on the newest attempt that
        # PUSHED THE NEXT REAL BACKUP OUT BY A FULL INTERVAL. A laptop closed at 03:00
        # lost that night and the following one.
        off_db = os.path.join(workdir, "offline.db")
        off_log = os.path.join(workdir, "offlinelogs")
        os.makedirs(off_log, exist_ok=True)
        fleet.init_fleet_db(off_db)
        backups.init_backups_db(off_db)
        off_dest = backups.create_destination(
            off_db, off_log, master_key, name="Off", kind=backups.KIND_S3,
            config=good_s3, secret={"access_key_id": "AKID", "secret_access_key": "shh"},
            actor="root@x.com")
        off_policy = dict(policy, fleet_destination=off_dest)

        T0 = 1_950_000_000
        check("an offline machine is not dispatched",
              backups.files_dispatch_once(
                  off_db, off_log, machines=[{"machine": "LAPTOP", "online": False}],
                  now=T0, **off_policy) == 0)
        check("...and no run row was invented for it",
              backups.list_runs(off_db, limit=5, kind=backups.BACKUP_MACHINE_FILES,
                                machine="LAPTOP") == [])
        check("...so its due clock did NOT move (this is the whole bug)",
              backups.files_due_at(off_db, "LAPTOP", 24) <= T0)

        T1 = T0 + 3 * 86400          # gone for a long weekend
        check("the moment it comes back it is dispatched",
              backups.files_dispatch_once(
                  off_db, off_log, machines=[{"machine": "LAPTOP", "online": True}],
                  now=T1, **off_policy) == 1)
        caught_up = backups.list_runs(off_db, limit=5,
                                      kind=backups.BACKUP_MACHINE_FILES,
                                      machine="LAPTOP")[0]
        check("...as a normal scheduled run",
              caught_up["trigger"] == backups.TRIGGER_SCHEDULE)
        check("...and not again on the next pass",
              backups.files_dispatch_once(
                  off_db, off_log, machines=[{"machine": "LAPTOP", "online": True}],
                  now=T1 + 60, **off_policy) == 0)
        check("a bare machine name still counts as online (old callers keep working)",
              backups.roster_entry("PC-X") == ("PC-X", True))

        # ============================================================
        print("\n== Back up now: a request outlives the PC being off ==")
        # ============================================================
        backups.request_file_run(off_db, "DESK", actor="op@x.com", now=T1)
        stored = backups.get_machine_config(off_db, "DESK")
        check("the request is recorded against the machine",
              stored["run_requested_at"] == T1 and stored["run_requested_by"] == "op@x.com")
        check("...without making the machine a policy exception",
              backups.has_overrides(stored) is False)

        check("a requested backup on an OFFLINE machine does not dispatch",
              backups.files_dispatch_once(
                  off_db, off_log, machines=[{"machine": "DESK", "online": False}],
                  now=T1 + 10, **off_policy) == 0)
        check("...and the request survives, waiting for it",
              backups.get_machine_config(off_db, "DESK")["run_requested_at"] == T1)

        backups.request_file_run(off_db, "DESK", actor="op@x.com", now=T1 + 20)
        check("pressing the button twice does not queue two backups",
              backups.get_machine_config(off_db, "DESK")["run_requested_at"] == T1 + 20)

        check("once online, the request becomes a real backup",
              backups.files_dispatch_once(
                  off_db, off_log, machines=[{"machine": "DESK", "online": True}],
                  now=T1 + 30, **off_policy) == 1)
        manual = backups.list_runs(off_db, limit=5, kind=backups.BACKUP_MACHINE_FILES,
                                   machine="DESK")[0]
        check("...labelled manual, not scheduled",
              manual["trigger"] == backups.TRIGGER_MANUAL)
        check("...crediting the operator who asked", manual["actor"] == "op@x.com")
        check("...and the request is cleared so it runs once, not forever",
              backups.get_machine_config(off_db, "DESK")["run_requested_at"] is None)

        # A request must beat the interval, or "Back up now" would silently do nothing
        # on a machine that was backed up an hour ago -- which is exactly when someone
        # presses it (before a risky change).
        backups.request_file_run(off_db, "DESK", actor="op@x.com", now=T1 + 40)
        backups.ingest_file_result(off_db, off_log, manual["id"], {"error": "x"},
                                   keep_chains=2)
        check("a manual request overrides 'not due yet'",
              backups.files_dispatch_once(
                  off_db, off_log, machines=[{"machine": "DESK", "online": True}],
                  now=T1 + 50, **off_policy) == 1)

        # ============================================================
        print("\n== Catch-up throttle ==")
        # ============================================================
        # Catch-up creates the herd this guards against: a fleet of laptops that were off
        # all weekend comes back within minutes of each other on Monday, and without a
        # cap every one of them starts pushing at once.
        herd_db = os.path.join(workdir, "herd.db")
        herd_log = os.path.join(workdir, "herdlogs")
        os.makedirs(herd_log, exist_ok=True)
        fleet.init_fleet_db(herd_db)
        backups.init_backups_db(herd_db)
        herd_dest = backups.create_destination(
            herd_db, herd_log, master_key, name="Herd", kind=backups.KIND_S3,
            config=good_s3, secret={"access_key_id": "AKID", "secret_access_key": "shh"},
            actor="root@x.com")
        herd_policy = dict(policy, fleet_destination=herd_dest)
        herd = [{"machine": f"M{i:02d}", "online": True} for i in range(10)]

        check("the throttle caps one pass",
              backups.files_dispatch_once(herd_db, herd_log, machines=herd, now=T1,
                                          max_concurrent=3, **herd_policy) == 3)
        check("...counted against what is actually running",
              backups.running_file_runs(herd_db) == 3)
        check("a second pass adds nothing while those are still in flight",
              backups.files_dispatch_once(herd_db, herd_log, machines=herd, now=T1 + 60,
                                          max_concurrent=3, **herd_policy) == 0)
        # Retiring an abandoned run must give its slot back, or one dead agent
        # permanently shrinks the fleet's backup capacity.
        backups.expire_stale_file_runs(herd_db, now=T1 + 25 * 3600)
        check("expiring stale runs releases capacity",
              backups.running_file_runs(herd_db) == 0)
        check("...and the queue drains on the next pass",
              backups.files_dispatch_once(herd_db, herd_log, machines=herd,
                                          now=T1 + 25 * 3600, max_concurrent=3,
                                          **herd_policy) == 3)
        check("0 means unlimited",
              backups.files_dispatch_once(
                  herd_db, herd_log,
                  machines=[{"machine": f"U{i}", "online": True} for i in range(6)],
                  now=T1 + 25 * 3600, max_concurrent=0, **herd_policy) == 6)

        # Manual beats scheduled when the throttle is holding a queue: an operator
        # standing at a PC should not wait behind thirty laptops that are merely due.
        pri_db = os.path.join(workdir, "priority.db")
        pri_log = os.path.join(workdir, "prioritylogs")
        os.makedirs(pri_log, exist_ok=True)
        fleet.init_fleet_db(pri_db)
        backups.init_backups_db(pri_db)
        pri_dest = backups.create_destination(
            pri_db, pri_log, master_key, name="Pri", kind=backups.KIND_S3,
            config=good_s3, secret={"access_key_id": "AKID", "secret_access_key": "shh"},
            actor="root@x.com")
        pri_roster = [{"machine": f"P{i}", "online": True} for i in range(5)]
        backups.request_file_run(pri_db, "P4", actor="op@x.com", now=T1)
        backups.files_dispatch_once(pri_db, pri_log, machines=pri_roster, now=T1,
                                    max_concurrent=1,
                                    **dict(policy, fleet_destination=pri_dest))
        served = backups.list_runs(pri_db, limit=5,
                                   kind=backups.BACKUP_MACHINE_FILES)
        check("the manually-requested machine is served first",
              len(served) == 1 and served[0]["machine"] == "P4")

        # ============================================================
        print("\n== Cancel: three states, three guarantees ==")
        # ============================================================
        can_db = os.path.join(workdir, "cancel.db")
        can_log = os.path.join(workdir, "cancellogs")
        os.makedirs(can_log, exist_ok=True)
        fleet.init_fleet_db(can_db)
        backups.init_backups_db(can_db)
        can_dest = backups.create_destination(
            can_db, can_log, master_key, name="Cancel", kind=backups.KIND_S3,
            config=good_s3, secret={"access_key_id": "AKID", "secret_access_key": "shh"},
            actor="root@x.com")
        can_policy = dict(policy, fleet_destination=can_dest)
        Tc = 1_960_000_000

        # State A -- a queued request, the PC never online. Cancel just drops it.
        backups.request_file_run(can_db, "A", actor="op@x.com", now=Tc)
        backups.files_dispatch_once(
            can_db, can_log, machines=[{"machine": "A", "online": False}],
            now=Tc, **can_policy)
        a = backups.cancel_file_run(can_db, "A", actor="op@x.com")
        check("cancelling a queued request clears it", a["request_cleared"] is True)
        check("...and reports nothing was actually running",
              a["stopped_in_flight"] is False and a["stopped_before_start"] is False)
        check("...so the machine has no pending request left",
              backups.get_machine_config(can_db, "A")["run_requested_at"] is None)

        # State B -- dispatched, command still pending (agent has not polled). Cancel
        # expires the command so the PC never starts.
        backups.files_dispatch_once(
            can_db, can_log, machines=[{"machine": "B", "online": True}],
            now=Tc, **can_policy)
        b = backups.cancel_file_run(can_db, "B", actor="op@x.com")
        check("cancelling before the agent claims it stops it clean",
              b["stopped_before_start"] is True and b["stopped_in_flight"] is False)
        b_run = backups.list_runs(can_db, kind=backups.BACKUP_MACHINE_FILES,
                                  machine="B")[0]
        check("...the run is marked cancelled, not failed",
              b_run["status"] == backups.RUN_CANCELLED)
        check("...the throttle slot is freed", backups.running_file_runs(can_db) == 0)
        b_cmd = fleet.list_commands(can_db, machine="B")[0]
        check("...and the command is expired so the agent never runs it",
              b_cmd["status"] == fleet.STATUS_EXPIRED)
        # An agent that DID claim it can no longer report against it -- proving the recall
        # actually closed the window rather than just relabelling.
        conn = sqlite3.connect(can_db)
        conn.execute("INSERT INTO agents(agent_id,machine,token_hash,enrolled_at,"
                     "last_seen,revoked) VALUES ('ag-b','B','h',?,?,0)", (Tc, Tc))
        conn.commit(); conn.close()
        check("...an expired command is not handed out on the next poll",
              fleet.claim_commands(can_db, "ag-b", "B") == [])

        # A cancelled machine is not immediately re-dispatched: its cancelled run stamped
        # started_at, so it is no longer "due" until the interval passes.
        check("a cancelled machine is not re-dispatched on the next tick",
              backups.files_dispatch_once(
                  can_db, can_log, machines=[{"machine": "B", "online": True}],
                  now=Tc + 120, **can_policy) == 0)

        # A manual request that lands while a run is in flight must NOT start a second
        # concurrent backup -- it waits for the current one. (The request bypasses the
        # due check, so without an explicit "already running" guard it would double up.)
        backups.files_dispatch_once(
            can_db, can_log, machines=[{"machine": "R", "online": True}],
            now=Tc, **can_policy)
        backups.request_file_run(can_db, "R", actor="op@x.com", now=Tc + 5)
        check("a request while a run is in flight does not double-dispatch",
              backups.files_dispatch_once(
                  can_db, can_log, machines=[{"machine": "R", "online": True}],
                  now=Tc + 10, **can_policy) == 0)
        check("...and the request is still pending, waiting its turn",
              backups.get_machine_config(can_db, "R")["run_requested_at"] == Tc + 5)
        r_runs = [r for r in backups.list_runs(can_db, limit=10,
                                               kind=backups.BACKUP_MACHINE_FILES)
                  if r["machine"] == "R"]
        check("...so only one run exists for it", len(r_runs) == 1)

        # State C -- the agent has claimed the command; it cannot be recalled.
        backups.files_dispatch_once(
            can_db, can_log, machines=[{"machine": "C", "online": True}],
            now=Tc, **can_policy)
        conn = sqlite3.connect(can_db)
        conn.execute("INSERT INTO agents(agent_id,machine,token_hash,enrolled_at,"
                     "last_seen,revoked) VALUES ('ag-c','C','h',?,?,0)", (Tc, Tc))
        conn.commit(); conn.close()
        claimed = fleet.claim_commands(can_db, "ag-c", "C")
        check("the agent claimed the backup command", len(claimed) == 1)
        c = backups.cancel_file_run(can_db, "C", actor="op@x.com")
        check("cancelling a claimed run is honest that the PC is already going",
              c["stopped_in_flight"] is True and c["stopped_before_start"] is False)
        c_run = backups.list_runs(can_db, kind=backups.BACKUP_MACHINE_FILES,
                                  machine="C")[0]
        check("...the run is cancelled and its slot freed",
              c_run["status"] == backups.RUN_CANCELLED
              and backups.current_running_file_run(can_db, "C") is None)

        # The orphan: the agent finishes its pass and uploads AFTER the cancel. The result
        # must be discarded, and the object it uploaded cleaned up -- nothing in
        # backup_file_sets will ever reference it, so rotation would never reap it.
        can_bucket = FakeDestination()
        can_bucket.objects[c_run["object_key"]] = b"already uploaded"
        saved_build = backups.build_client
        backups.build_client = lambda record, secret: can_bucket
        late = backups.ingest_file_result(can_db, can_log, c_run["id"], {
            "stored_bytes": 16,
            "files": [{"path": "C:\\x.txt", "size": 1, "mtime": 1, "sha256": "aa"}]},
            keep_chains=2)
        backups.build_client = saved_build
        check("a late result for a cancelled run is ignored",
              late["status"] == backups.RUN_CANCELLED)
        check("...it does not populate the manifest",
              backups.current_manifest(can_db, "C") == [])
        check("...and the orphaned object is deleted from the destination",
              c_run["object_key"] not in can_bucket.objects
              and ("delete", c_run["object_key"]) in can_bucket.calls)

        # Idempotent: a second cancel, or a cancel with nothing running, is a calm no-op.
        none = backups.cancel_file_run(can_db, "C", actor="op@x.com")
        check("cancelling with nothing running says so",
              none["nothing_to_cancel"] is True)

        # Fleet-wide cancel is just the per-machine one across the roster; assert it clears
        # a mix of states in one pass.
        backups.request_file_run(can_db, "F1", actor="op@x.com", now=Tc)
        backups.files_dispatch_once(
            can_db, can_log,
            machines=[{"machine": "F1", "online": False},
                      {"machine": "F2", "online": True}],
            now=Tc, **can_policy)
        f1 = backups.cancel_file_run(can_db, "F1", actor="op@x.com")
        f2 = backups.cancel_file_run(can_db, "F2", actor="op@x.com")
        check("a fleet cancel drops F1's queued request", f1["request_cleared"])
        check("...and stops F2's pending run", f2["stopped_before_start"])

        # ============================================================
        print("\n== Restore: browsing the manifest ==")
        # ============================================================
        # The browser has to be honest about what is RECOVERABLE, which is not the same as
        # what was ever backed up: a deleted file and a rotated-away chain both have rows
        # in backup_files, and offering either as restorable is a 404 an operator only
        # discovers after they have already deleted the original.
        br_db = os.path.join(workdir, "browse.db")
        fleet.init_fleet_db(br_db)
        backups.init_backups_db(br_db)
        br_plan = backups.plan_next_run(br_db, "PC-M", full_every=5)
        backups.record_file_set(
            br_db, run_id=uuid_hex(), machine="PC-M", chain_id=br_plan["chain_id"],
            sequence=0, object_key="p/machines/PC-M/full.fhb", stored_bytes=99, files=[
                {"path": "C:\\Users\\bob\\Desktop\\a.txt", "size": 10, "mtime": 1,
                 "sha256": "aa"},
                {"path": "C:\\Users\\bob\\Desktop\\notes\\deep.txt", "size": 5, "mtime": 1,
                 "sha256": "dd"},
                {"path": "C:\\Users\\bob\\Documents\\report.docx", "size": 40, "mtime": 1,
                 "sha256": "rr"},
                {"path": "C:\\Users\\carol\\Desktop\\c.txt", "size": 7, "mtime": 1,
                 "sha256": "cc"},
                {"path": "C:\\Users\\bob\\Desktop\\gone.txt", "size": 3, "mtime": 1,
                 "sha256": "gg"},
            ])
        br_inc = backups.plan_next_run(br_db, "PC-M", full_every=5)
        backups.record_file_set(
            br_db, run_id=uuid_hex(), machine="PC-M", chain_id=br_inc["chain_id"],
            sequence=1, object_key="p/machines/PC-M/inc.fhb", stored_bytes=9, files=[
                {"path": "C:\\Users\\bob\\Desktop\\a.txt", "size": 12, "mtime": 2,
                 "sha256": "aa2"},
                {"path": "C:\\Users\\bob\\Desktop\\gone.txt", "deleted": True},
            ])

        summary = backups.manifest_summary(br_db, "PC-M")
        check("the summary counts only recoverable files", summary["file_count"] == 4)
        check("...and counts the archives behind them", summary["archives"] == 2)

        root = backups.manifest_listing(br_db, "PC-M", "")
        check("the root listing shows the drive as a folder",
              [d["name"] for d in root["dirs"]] == ["C:"])
        check("...with everything under it counted", root["dirs"][0]["file_count"] == 4)
        check("the root has no files of its own", root["files"] == [])

        desktop = backups.manifest_listing(br_db, "PC-M", "C:\\Users\\bob\\Desktop")
        check("a folder lists its files",
              [f["name"] for f in desktop["files"]] == ["a.txt"])
        check("...at their NEWEST version", desktop["files"][0]["size"] == 12)
        check("a deleted file is not offered for restore",
              all(f["name"] != "gone.txt" for f in desktop["files"]))
        check("subfolders are derived from the paths beneath them",
              [d["name"] for d in desktop["dirs"]] == ["notes"])
        check("breadcrumbs walk back to the drive",
              [p["name"] for p in desktop["parents"]] == ["C:", "Users", "bob"])
        check("...as usable paths", desktop["parents"][2]["path"] == "C:\\Users\\bob")

        # A folder name containing a LIKE wildcard must not act as one: `%` and `_` are
        # legal in Windows filenames, and a raw prefix concatenation would hand back files
        # from a completely different folder.
        wild_plan = backups.plan_next_run(br_db, "PC-W", full_every=5)
        backups.record_file_set(
            br_db, run_id=uuid_hex(), machine="PC-W", chain_id=wild_plan["chain_id"],
            sequence=0, object_key="p/machines/PC-W/full.fhb", stored_bytes=1, files=[
                {"path": "C:\\temp%\\inside.txt", "size": 1, "sha256": "x"},
                {"path": "C:\\tempZ\\outside.txt", "size": 1, "sha256": "y"},
            ])
        wild = backups.manifest_listing(br_db, "PC-W", "C:\\temp%")
        check("a folder named with a LIKE wildcard matches only itself",
              [f["name"] for f in wild["files"]] == ["inside.txt"])

        found = backups.manifest_search(br_db, "PC-M", "desktop")
        check("search spans folders",
              {f["name"] for f in found["files"]} == {"a.txt", "deep.txt", "c.txt"})
        check("search does not resurrect deleted files",
              all(f["name"] != "gone.txt" for f in found["files"]))
        check("an empty search returns nothing rather than everything",
              backups.manifest_search(br_db, "PC-M", "  ")["files"] == [])

        # ============================================================
        print("\n== Restore: planning ==")
        # ============================================================
        plan_out = backups.plan_restore(br_db, "PC-M", ["C:\\Users\\bob"])
        check("a folder selection expands to everything under it",
              plan_out["file_count"] == 3)
        check("...spanning every archive that holds a wanted version",
              len(plan_out["archives"]) == 2)
        check("...and totals the bytes actually being fetched",
              plan_out["total_bytes"] == 12 + 5 + 40)
        by_key = {a["object_key"]: a for a in plan_out["archives"]}
        check("the changed file is taken from the INCREMENTAL",
              [f["path"] for f in by_key["p/machines/PC-M/inc.fhb"]["files"]]
              == ["C:\\Users\\bob\\Desktop\\a.txt"])
        check("...and the untouched ones from the full",
              len(by_key["p/machines/PC-M/full.fhb"]["files"]) == 2)
        check("archives are ordered oldest first",
              [a["index"] for a in plan_out["archives"]] == [0, 1]
              and plan_out["archives"][0]["object_key"].endswith("full.fhb"))
        # The shared contract with the agent's tar writer. If these disagree the restore
        # finds nothing and reports it as missing from the archive.
        check("every file names the member it lives under inside the archive",
              by_key["p/machines/PC-M/inc.fhb"]["files"][0]["member"]
              == "C/Users/bob/Desktop/a.txt")

        exact = backups.plan_restore(br_db, "PC-M",
                                     ["C:\\Users\\bob\\Documents\\report.docx"])
        check("a single file can be restored on its own", exact["file_count"] == 1)
        check("...from just the one archive it lives in", len(exact["archives"]) == 1)

        mixed = backups.plan_restore(br_db, "PC-M",
                                     ["C:\\Users\\carol", "C:\\Users\\nobody"])
        check("a selection matching nothing is NAMED, not silently dropped",
              mixed["missing"] == ["C:\\Users\\nobody"])
        check("...while the rest of the selection still resolves",
              mixed["file_count"] == 1)
        check("a selection that matches nothing at all is refused",
              raises(ValueError, backups.plan_restore, br_db, "PC-M", ["D:\\nope"]))
        check("an empty selection is refused",
              raises(ValueError, backups.plan_restore, br_db, "PC-M", []))
        # A bare drive root has to match everything on that drive. normalize() keeps the
        # trailing separator on "C:\" (a root is not the same as the drive-relative "C:"),
        # but the outermost ancestor of C:\Users\bob\a.txt is "C:" -- so without trimming
        # it here, "restore this whole drive" would match nothing, silently.
        whole_drive = backups.plan_restore(br_db, "PC-M", ["C:\\"])
        check("selecting a drive ROOT matches everything on it",
              whole_drive["file_count"] == 4)
        check("...spelled either way", backups.plan_restore(
            br_db, "PC-M", ["C:"])["file_count"] == 4)
        check("a selection over the file cap is refused rather than truncated",
              raises(ValueError, backups.plan_restore, br_db, "PC-M", ["C:\\"],
                     max_files=1))
        # A prefix that is not a whole path segment must not match: matching on a raw
        # string prefix would make selecting C:\Users\bob drag in C:\Users\bobby too.
        check("folder matching is by path segment, not string prefix",
              raises(ValueError, backups.plan_restore, br_db, "PC-M",
                     ["C:\\Users\\bo"]))

        print("\n-- target folder validation --")
        check("no folder means the original locations",
              backups.validate_target_dir("") == "")
        check("a local absolute path is accepted",
              backups.validate_target_dir("C:/Restored/") == "C:\\Restored")
        check("a relative path is refused (SYSTEM's cwd is System32)",
              raises(ValueError, backups.validate_target_dir, "Restored"))
        check("a UNC path is refused",
              raises(ValueError, backups.validate_target_dir, "\\\\srv\\share\\x"))
        check("a path with .. is refused",
              raises(ValueError, backups.validate_target_dir, "C:\\a\\..\\b"))

        print("\n-- the restore lifecycle --")
        br_dest = backups.create_destination(
            br_db, sched_log, master_key, name="Restores", kind=backups.KIND_S3,
            config=good_s3, secret={"access_key_id": "AKID", "secret_access_key": "shh"},
            actor="root@x.com")
        restore_id = backups.create_restore(
            br_db, machine="PC-NEW", source_machine="PC-M", destination_id=br_dest,
            plan=plan_out, target_dir="C:\\Restored", overwrite=False,
            actor="root@x.com", now=1_950_000_000)
        opened = backups.get_restore(br_db, restore_id)
        check("a restore opens as running", opened["status"] == backups.RUN_RUNNING)
        check("...naming both ends of a cross-machine restore",
              opened["machine"] == "PC-NEW" and opened["source_machine"] == "PC-M")
        check("...and is audited when it starts",
              "backup_restore_start" in audit_actions(br_db))

        cmd_params = backups.build_restore_command_params(
            restore_id=restore_id, source_machine="PC-M", plan=plan_out)
        # The whole reason the plan is fetched rather than carried: fleet.create_command
        # audits params verbatim, so a file list here would write a multi-megabyte audit
        # row -- with the decryption key in it.
        check("command params carry no file list", "files" not in json.dumps(cmd_params))
        check("...and no encryption key", "key" not in json.dumps(cmd_params))
        check("...but do carry the size of the job", cmd_params["file_count"] == 3)

        payload = backups.restore_plan_payload(br_db, sched_log, restore_id,
                                               hub_url="https://hub.example")
        check("the plan payload carries the SOURCE machine's derived key",
              base64.b64decode(payload["encryption"]["key"])
              == backups.derive_machine_key(master_key, "PC-M"))
        check("...not the target machine's",
              base64.b64decode(payload["encryption"]["key"])
              != backups.derive_machine_key(master_key, "PC-NEW"))
        check("...and never the master key",
              base64.b64decode(payload["encryption"]["key"]) != master_key)
        check("every archive gets a pre-signed download URL",
              all(a["download"]["kind"] == "s3"
                  and "X-Amz-Signature=" in a["download"]["url"]
                  for a in payload["archives"]))
        check("the S3 secret never reaches the agent", "shh" not in json.dumps(payload))
        check("the target folder rides along", payload["target_dir"] == "C:\\Restored")

        check("an archive index resolves to the key the hub planned",
              backups.restore_archive_key(br_db, restore_id, 0)
              == plan_out["archives"][0]["object_key"])
        check("an index outside the plan resolves to nothing",
              backups.restore_archive_key(br_db, restore_id, 99) is None)

        # Partial success is a FAILURE with the numbers, not a green row: "restored 2 of 3"
        # needs someone to look at the third, and success means nobody ever does.
        partial = backups.ingest_restore_result(br_db, restore_id,
                                                {"restored": 2, "bytes_restored": 50,
                                                 "failures": ["C:\\x: locked"]})
        check("a short restore is recorded as failed",
              partial["status"] == backups.RUN_FAILED)
        check("...saying how many of how many", "2 of 3" in partial["error"])
        check("...and naming the first problem", "locked" in partial["error"])
        check("a repeat report is ignored",
              backups.ingest_restore_result(br_db, restore_id,
                                            {"restored": 3})["restored_count"] == 2)

        full_restore = backups.create_restore(
            br_db, machine="PC-M", source_machine="PC-M", destination_id=br_dest,
            plan=exact, actor="root@x.com", now=1_950_000_100)
        done = backups.ingest_restore_result(br_db, full_restore,
                                             {"restored": 1, "bytes_restored": 40})
        check("restoring everything asked for succeeds",
              done["status"] == backups.RUN_SUCCEEDED)
        check("...and is audited", "backup_restore" in audit_actions(br_db))
        check("history shows a machine's restores from BOTH ends",
              len(backups.list_restores(br_db, machine="PC-M")) == 2)
        check("the history view drops the plan rather than shipping it to a browser",
              "plan" not in backups.list_restores(br_db, machine="PC-M")[0])

        stuck_restore = backups.create_restore(
            br_db, machine="PC-Z", source_machine="PC-M", destination_id=br_dest,
            plan=exact, actor="root@x.com", now=1_950_000_200)
        check("a fresh restore is not expired",
              backups.expire_stale_restores(br_db, now=1_950_000_300) == 0)
        check("a restore nobody reported IS expired",
              backups.expire_stale_restores(
                  br_db, now=1_950_000_200 + 25 * 3600) == 1)
        check("...and says why",
              "never reported" in backups.get_restore(br_db, stuck_restore)["error"])

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
