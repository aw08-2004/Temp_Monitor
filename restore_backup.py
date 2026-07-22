"""Decrypt a FleetHub backup. Standalone -- no hub, no database, no network.

THIS IS THE POINT OF THE WHOLE FEATURE. A backup you cannot restore without the server
that made it is not a backup of that server. So this script depends on exactly two
things: the artifact, and the master key. It does not import app.py, does not read
`settings`, does not need `.env`, and does not care whether the hub it came from still
exists. Copy this file and your key onto any machine with Python and `cryptography`, and
you can get the database back.

    python restore_backup.py --in 20260721T030000Z-temp_v2.db.gz.fhb --out temp_v2.db
    python restore_backup.py --in backup.fhb --info          # just read the header

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
import getpass
import os
import sqlite3
import sys
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
        f"  source           {header.get('source', 'unknown')}",
        f"  written by hub   {header.get('hub_version') or 'unknown'}",
        f"  created          " + (time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                                time.gmtime(created))
                                  if created else "unknown"),
        f"  cipher           {header.get('cipher')} + {header.get('compression')}",
        f"  key id           {header.get('key_id')}",
    ]
    source_bytes = header.get("source_bytes")
    if source_bytes:
        lines.append(f"  original size    {source_bytes:,} bytes")
    return "\n".join(lines)


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
    parser.add_argument("--verify", action="store_true",
                        help="run PRAGMA integrity_check on the restored file")
    parser.add_argument("--force", action="store_true",
                        help="overwrite --out if it already exists")
    args = parser.parse_args()

    if not backups.CRYPTO_AVAILABLE:
        print("error: the 'cryptography' package is required. Run: pip install cryptography",
              file=sys.stderr)
        return 2
    if not os.path.exists(args.source):
        print(f"error: no such file: {args.source}", file=sys.stderr)
        return 2
    if not args.info and not args.target:
        print("error: --out is required (or use --info to inspect the file)",
              file=sys.stderr)
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
