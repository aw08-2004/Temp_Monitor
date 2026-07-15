#!/usr/bin/env python3
"""Sign companion.py for the Temp_Monitor self-updater.

Clients (companion.py) verify a detached Ed25519 signature over the exact bytes of
companion.py before applying an update, using the public key embedded in that file's
UPDATE_PUBLIC_KEY_HEX. This script generates that keypair and produces the
companion.py.sig file you commit alongside companion.py.

Usage
-----
  python sign_release.py --genkey [--key PATH]
      Generate a new Ed25519 keypair. Writes the PRIVATE key (raw hex) to PATH
      (default: %USERPROFILE%/.temp_monitor_signing_key) and prints the PUBLIC key
      line to paste into companion.py's UPDATE_PUBLIC_KEY_HEX.
      KEEP THE PRIVATE KEY SECRET AND OUT OF THE REPO. Anyone with it can push code
      that runs as admin on the whole fleet.

  python sign_release.py [--key PATH] [--file companion.py]
      Sign FILE with the private key and write FILE + '.sig' (hex signature).
      Run this after every edit to companion.py, then commit BOTH files together.

  python sign_release.py --sign-command --type run_script --machine PC-01 \
                         --params '{"script": "..."}' [--key PATH]
      Sign a single high-risk fleet command (run_script / install_driver /
      update_bios). Prints the detached signature hex to paste into the hub's
      "issue command" form. The hub AND the agent both verify this same signature
      over the canonical payload (see fleet.canonical_command_bytes) before the
      command is queued or executed -- so a high-risk command can only originate
      from whoever holds this offline private key, not from a compromised hub.

Note: .gitattributes pins companion.py / companion.py.sig to '-text' so git never
rewrites line endings -- otherwise the committed bytes wouldn't match what you
signed and clients would reject the update.
"""
import argparse
import json
import os
import sys

DEFAULT_KEY_PATH = os.path.join(os.path.expanduser("~"), ".temp_monitor_signing_key")
DEFAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "companion.py")


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        return Ed25519PrivateKey, serialization
    except ImportError:
        sys.exit("cryptography is not installed. Run: python -m pip install cryptography")


def genkey(path):
    Ed25519PrivateKey, serialization = _ed25519()
    if os.path.exists(path):
        sys.exit(f"Refusing to overwrite existing key at {path}. Delete it first if you really mean to.")

    priv = Ed25519PrivateKey.generate()
    raw_priv = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(raw_priv.hex())
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

    print(f"Private key written to: {path}")
    print("  ^ KEEP THIS SECRET and OUT of the git repo.\n")
    print("Paste this line into companion.py:")
    print(f'    UPDATE_PUBLIC_KEY_HEX = "{raw_pub.hex()}"')


def sign(path, file):
    Ed25519PrivateKey, _ = _ed25519()
    if not os.path.exists(path):
        sys.exit(f"No signing key at {path}. Run: python sign_release.py --genkey")

    with open(path, "r", encoding="utf-8") as f:
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))

    with open(file, "rb") as f:
        data = f.read()

    signature = priv.sign(data)
    sig_path = file + ".sig"
    with open(sig_path, "w", encoding="utf-8") as f:
        f.write(signature.hex())

    print(f"Signed {file} ({len(data)} bytes) -> {sig_path}")
    print("Commit BOTH companion.py and companion.py.sig together, then push.")


def sign_command(path, command_type, machine, params_json):
    """Sign one high-risk fleet command with the offline private key and print the
    detached signature hex. Canonicalization is imported from fleet.py so the
    signer and the two verifiers (hub + agent) can never drift out of sync."""
    Ed25519PrivateKey, _ = _ed25519()
    if not os.path.exists(path):
        sys.exit(f"No signing key at {path}. Run: python sign_release.py --genkey")

    try:
        params = json.loads(params_json) if params_json else {}
    except (ValueError, TypeError) as e:
        sys.exit(f"--params must be valid JSON: {e}")
    if not isinstance(params, dict):
        sys.exit("--params must be a JSON object (e.g. '{\"script\": \"...\"}')")

    # Single source of truth for the signed bytes -- same function the hub and
    # agent use to verify.
    try:
        from fleet import canonical_command_bytes, HIGH_RISK_COMMANDS
    except ImportError:
        sys.exit("fleet.py must be importable (run from the repo root).")
    if command_type not in HIGH_RISK_COMMANDS:
        sys.exit(f"{command_type!r} is not a high-risk command; only these need signing: "
                 f"{', '.join(sorted(HIGH_RISK_COMMANDS))}")

    with open(path, "r", encoding="utf-8") as f:
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))

    message = canonical_command_bytes(command_type, machine, params)
    signature = priv.sign(message)
    print(f"Command : {command_type} on {machine}")
    print(f"Params  : {json.dumps(params, sort_keys=True, separators=(',', ':'))}")
    print("\nSignature (paste into the hub's issue-command form):")
    print(signature.hex())


def main():
    ap = argparse.ArgumentParser(description="Sign companion.py / fleet commands for Temp_Monitor.")
    ap.add_argument("--genkey", action="store_true", help="generate a new keypair instead of signing")
    ap.add_argument("--sign-command", action="store_true", help="sign one high-risk fleet command")
    ap.add_argument("--type", help="command type to sign (with --sign-command)")
    ap.add_argument("--machine", help="target machine name (with --sign-command)")
    ap.add_argument("--params", default="{}", help="JSON params object (with --sign-command)")
    ap.add_argument("--key", default=DEFAULT_KEY_PATH, help="path to the private signing key")
    ap.add_argument("--file", default=DEFAULT_FILE, help="file to sign (default: companion.py)")
    args = ap.parse_args()

    if args.genkey:
        genkey(args.key)
    elif args.sign_command:
        if not args.type or not args.machine:
            sys.exit("--sign-command requires --type and --machine")
        sign_command(args.key, args.type, args.machine, args.params)
    else:
        sign(args.key, args.file)


if __name__ == "__main__":
    main()
