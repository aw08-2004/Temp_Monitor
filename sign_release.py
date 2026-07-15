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

Note: .gitattributes pins companion.py / companion.py.sig to '-text' so git never
rewrites line endings -- otherwise the committed bytes wouldn't match what you
signed and clients would reject the update.
"""
import argparse
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


def main():
    ap = argparse.ArgumentParser(description="Sign companion.py for the self-updater.")
    ap.add_argument("--genkey", action="store_true", help="generate a new keypair instead of signing")
    ap.add_argument("--key", default=DEFAULT_KEY_PATH, help="path to the private signing key")
    ap.add_argument("--file", default=DEFAULT_FILE, help="file to sign (default: companion.py)")
    args = ap.parse_args()

    if args.genkey:
        genkey(args.key)
    else:
        sign(args.key, args.file)


if __name__ == "__main__":
    main()
