"""Decrypt a FleetHub backup. Standalone -- no hub, no database, no network.

THIS IS THE POINT OF THE WHOLE FEATURE. A backup you cannot restore without the server
that made it is not a backup of that server. So this script depends on exactly two
things: the artifact, and the master key. It does not import app.py, does not read
`settings`, does not need `.env`, and does not care whether the hub it came from still
exists. Copy this file and your key onto any machine with Python and `cryptography`, and
you can get the database back.

    python restore_backup.py --in 20260721T030000Z-temp_v2.db.gz.fhb --out temp_v2.db
    python restore_backup.py --in backup.fhb --info          # just read the header

A machine's FILE backup is a tar inside the same envelope, so the same tool opens it --
and one master key opens every machine, because the per-machine key is derived from it and
the header says which machine to derive for:

    python restore_backup.py --in 20260721T030000Z-a1b2c3-000-full.fhb --list
    python restore_backup.py --in ...-full.fhb --extract C:\\Recovered
    python restore_backup.py --in ...-full.fhb --extract C:\\Recovered --match "*/Desktop/*"

This is the console's restore button minus the console: if the hub is gone, or an operator
wants one file out of an archive without pushing it back onto a PC, this is the path. It
is also why the archive is tar and not a proprietary container.

The key is read from BACKUP_MASTER_KEY, or --key, or an interactive prompt. Prefer the
prompt or the environment: a key passed as --key is visible in the process list and in
your shell history.

To put a restored database back into service:

  1. Stop the hub service   (`Stop-Service "FleetHub - Hub"`).
  2. Move the existing logs\\temp_v2.db aside -- do not delete it until the restore is
     confirmed good, and take its -wal and -shm files with it.
  3. Copy the restored file into place as logs\\temp_v2.db.
  4. Start the service and check the machine list.

The restored file is a plain SQLite database produced by VACUUM INTO, so it has no -wal
sidecar and opens cleanly on its own. `--verify` opens it and runs an integrity check
before you trust it, which is worth the seconds it costs.
"""
import argparse
import fnmatch
import getpass
import io
import os
import sqlite3
import sys
import tarfile
import time

# Sits next to backups.py in a hub install (both are in HUB_RUNTIME_FILES), so importing
# it directly is right -- the envelope format has exactly one implementation, and a second
# copy here would be free to drift from the one that writes the files.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backups


def resolve_key(explicit):
    if explicit:
        return explicit.strip()
    from_env = os.environ.get(backups.MASTER_KEY_ENV, "").strip()
    if from_env:
        return from_env
    return getpass.getpass("Backup master key (base64): ").strip()


def describe(header):
    created = header.get("created_at")
    lines = [
        f"  format version   {header.get('v')}",
        f"  contents         {header.get('kind', 'unknown')}",
        f"  written by hub   {header.get('hub_version') or 'unknown'}",
        f"  created          " + (time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                                time.gmtime(created))
                                  if created else "unknown"),
        f"  cipher           {header.get('cipher')} + {header.get('compression')}",
        f"  key id           {header.get('key_id')}",
    ]
    if header.get("machine"):
        # A machine archive. Named explicitly because the machine is what the decryption
        # key was derived FROM -- if this line is wrong, no key on earth opens the file.
        lines.insert(2, f"  machine          {header['machine']}")
        if header.get("chain_id"):
            lines.insert(3, "  archive          "
                            f"chain {str(header['chain_id'])[:12]}, "
                            f"sequence {header.get('sequence')}"
                            + (" (full)" if header.get("full") else " (incremental)"))
        if header.get("agent_version"):
            lines.append(f"  written by agent {header['agent_version']}")
    else:
        lines.insert(2, f"  source           {header.get('source', 'unknown')}")
    source_bytes = header.get("source_bytes")
    if source_bytes:
        lines.append(f"  original size    {source_bytes:,} bytes")
    return "\n".join(lines)


def is_archive(header):
    """Does this artifact hold a tar of files, rather than a single blob?

    Decided from the header's `kind`, not from the filename or by sniffing the plaintext:
    the envelope is the thing that knows, and it is authenticated -- a `kind` an attacker
    edited fails the AAD check before any of this runs.
    """
    return header.get("kind") == "machine_files"


class ChunkReader(io.RawIOBase):
    """A read-only file object over an iterator of byte blocks.

    `tarfile` wants something with `read()`; `read_envelope` gives a generator. Adapting
    rather than joining the blocks is the whole point -- a machine archive is allowed to
    be bigger than RAM, which is why the format is chunked in the first place.
    """

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buffer = b""

    def readable(self):
        return True

    def readinto(self, target):
        while not self._buffer:
            try:
                self._buffer = next(self._chunks)
            except StopIteration:
                return 0
        count = min(len(target), len(self._buffer))
        target[:count] = self._buffer[:count]
        self._buffer = self._buffer[count:]
        return count


def open_archive(header, chunks):
    """A streaming tarfile over a decrypted archive.

    `r|` (stream mode), not `r`: the plaintext arrives as a one-pass generator and cannot
    be seeked, and asking tarfile for random access would make it buffer the whole thing.
    """
    stream = backups.iter_gunzip(chunks) if header.get("compression") == "gzip" else chunks
    return tarfile.open(fileobj=io.BufferedReader(ChunkReader(stream)), mode="r|")


def matches(name, patterns):
    """Case-insensitive glob match against a tar member name, or True if no patterns.

    Matched on the ARCHIVE name (`C/Users/bob/Desktop/x.txt`), which is what --list
    prints, so a pattern is written against what you can see rather than against the
    Windows path it came from.
    """
    if not patterns:
        return True
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, p.lower()) for p in patterns)


def safe_target(root, name):
    """Where a member is written under `root`, or None if it tried to escape.

    Tar member names come from a machine that was, by assumption, worth backing up but is
    not necessarily trustworthy now -- and an archive is a file an attacker could hand you.
    A member named `../../Windows/System32/x.dll` extracting outside the folder you named
    is the classic tar traversal, so absolute paths, drive letters and `..` are refused
    rather than sanitised.
    """
    parts = [p for p in str(name or "").replace("\\", "/").split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    if os.path.isabs(name) or ":" in parts[0]:
        return None
    target = os.path.abspath(os.path.join(root, *parts))
    if os.path.commonpath([os.path.abspath(root), target]) != os.path.abspath(root):
        return None
    return target


def list_archive(header, chunks):
    """Print an archive's contents. Returns how many members were listed."""
    count, total = 0, 0
    with open_archive(header, chunks) as tar:
        for member in tar:
            if not member.isfile():
                continue
            count += 1
            total += member.size
            stamp = time.strftime("%Y-%m-%d %H:%M",
                                  time.gmtime(member.mtime)) if member.mtime else ""
            print(f"  {member.size:>12,}  {stamp:>16}  {member.name}")
    print(f"\n{count:,} file(s), {total:,} bytes")
    return count


def extract_archive(header, chunks, root, patterns, force):
    """Extract matching members under `root`. Returns (extracted, skipped)."""
    os.makedirs(root, exist_ok=True)
    extracted, skipped = 0, 0
    with open_archive(header, chunks) as tar:
        for member in tar:
            if not member.isfile() or not matches(member.name, patterns):
                continue
            target = safe_target(root, member.name)
            if target is None:
                print(f"  refused (unsafe name): {member.name}", file=sys.stderr)
                skipped += 1
                continue
            if os.path.exists(target) and not force:
                print(f"  skipped (exists): {member.name}")
                skipped += 1
                continue
            source = tar.extractfile(member)
            if source is None:
                skipped += 1
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Written to a temp name and renamed, like the database path above: the chunk
            # generator raises on a corrupt tail mid-write, and a half-written file sitting
            # at the real name is one somebody restores over the good copy.
            partial = target + ".partial"
            with open(partial, "wb") as out:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
            os.replace(partial, target)
            if member.mtime:
                os.utime(target, (member.mtime, member.mtime))
            extracted += 1
    return extracted, skipped


def verify_database(path):
    """Open the restored file and ask SQLite whether it is intact.

    `PRAGMA integrity_check` rather than just opening it: a file can open fine and fail
    on the first read of a corrupt page, which during a restore means finding out after
    you have already deleted the original.
    """
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        machines = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    finally:
        conn.close()
    return result, machines


def main():
    parser = argparse.ArgumentParser(
        description="Decrypt a FleetHub backup artifact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("To put a restored database")[0])
    parser.add_argument("--in", dest="source", required=True,
                        help="the .fhb artifact to read")
    parser.add_argument("--out", dest="target",
                        help="where to write the decrypted database")
    parser.add_argument("--key", help="base64 master key (prefer the prompt: this is "
                                      "visible in the process list)")
    parser.add_argument("--info", action="store_true",
                        help="print the artifact header and exit without decrypting")
    parser.add_argument("--list", action="store_true",
                        help="list the files inside a machine's file backup")
    parser.add_argument("--extract", metavar="DIR",
                        help="extract a machine's file backup into DIR")
    parser.add_argument("--match", action="append", default=[], metavar="GLOB",
                        help="with --list/--extract, only members matching this glob "
                             "(repeatable, e.g. --match '*/Desktop/*')")
    parser.add_argument("--verify", action="store_true",
                        help="run PRAGMA integrity_check on the restored file")
    parser.add_argument("--force", action="store_true",
                        help="overwrite files that already exist")
    args = parser.parse_args()

    modes = [bool(args.target), args.info, args.list, bool(args.extract)]
    if not backups.CRYPTO_AVAILABLE:
        print("error: the 'cryptography' package is required. Run: pip install cryptography",
              file=sys.stderr)
        return 2
    if not os.path.exists(args.source):
        print(f"error: no such file: {args.source}", file=sys.stderr)
        return 2
    if sum(modes) > 1:
        print("error: choose one of --out, --info, --list or --extract.", file=sys.stderr)
        return 2
    if not any(modes):
        print("error: choose one of --out (whole file), --list / --extract (files inside "
              "a machine backup), or --info to inspect the header.", file=sys.stderr)
        return 2
    if args.target and os.path.exists(args.target) and not args.force:
        print(f"error: {args.target} already exists. Pass --force to overwrite it.",
              file=sys.stderr)
        return 2

    try:
        key = backups.decode_master_key(resolve_key(args.key))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    with open(args.source, "rb") as source:
        try:
            header, chunks = backups.read_envelope(source, key)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        print(f"{args.source}:")
        print(describe(header))
        if args.info:
            return 0

        if args.list or args.extract:
            # Refused rather than attempted: a hub-database artifact is a SQLite file, and
            # handing it to tarfile produces "file could not be opened successfully", which
            # tells an operator nothing about what they actually did wrong.
            if not is_archive(header):
                print("error: this is not a machine file backup, so there are no files "
                      "inside it. Use --out to write the decrypted contents.",
                      file=sys.stderr)
                return 2
            try:
                if args.list:
                    print()
                    list_archive(header, chunks)
                    return 0
                extracted, skipped = extract_archive(header, chunks, args.extract,
                                                     args.match, args.force)
            except (ValueError, tarfile.TarError) as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            print(f"\nExtracted {extracted:,} file(s) to {args.extract}"
                  + (f"; {skipped:,} skipped" if skipped else ""))
            if not extracted:
                # An exit code, not just a sentence: a script that treated "nothing
                # restored" as success would cheerfully report a recovery that never
                # happened. The two reasons need different fixes, so they are named
                # separately rather than both being "nothing matched".
                print("error: " + ("every matching file already exists here -- pass "
                                   "--force to overwrite them." if skipped
                                   else "nothing in this archive matched."),
                      file=sys.stderr)
                return 1
            return 0

        # Written to a temp file and renamed only on success. read_envelope's generator
        # raises on a corrupt or truncated tail while output is already being written, so
        # decrypting straight onto --out would leave a half-database sitting at exactly
        # the path someone is about to copy over their live one.
        partial = args.target + ".partial"
        written = 0
        try:
            with open(partial, "wb") as target:
                stream = (backups.iter_gunzip(chunks)
                          if header.get("compression") == "gzip" else chunks)
                for block in stream:
                    target.write(block)
                    written += len(block)
        except ValueError as e:
            os.remove(partial)
            print(f"error: {e}", file=sys.stderr)
            return 1
        os.replace(partial, args.target)

    print(f"\nRestored {written:,} bytes to {args.target}")

    if args.verify:
        try:
            result, tables = verify_database(args.target)
        except sqlite3.Error as e:
            print(f"error: the restored file is not a readable database: {e}",
                  file=sys.stderr)
            return 1
        if result != "ok":
            print(f"error: integrity check failed: {result}", file=sys.stderr)
            return 1
        print(f"Integrity check passed ({tables} tables).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
