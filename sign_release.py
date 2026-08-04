#!/usr/bin/env python3
"""Sign the C#/.NET agent's self-update manifest for the Temp_Monitor fleet.

The running agent verifies a detached Ed25519 signature over the exact bytes of
agent/agent.manifest.json before it downloads or applies any update, using the public
key embedded in AgentConfig.UpdatePublicKeyHex. This script generates that keypair and
produces the signed manifest.

This is the RELEASE trust root: it decides what code the fleet is allowed to run as
SYSTEM, and it is fully enforced. It is unrelated to fleet COMMANDS, which used to
be signed here too (--sign-command) and no longer are -- issuing a command now
requires only an authenticated, allow-listed console session, so a whole helpdesk
group can use the channel without sharing an offline key. See fleet.py's module
docstring. Do not conflate the two: a compromised hub still must not be able to push
a malicious binary, which is exactly what the signatures below prevent.

(Until it was removed from the repo, the Python companion.py was signed here too, with
the same key. Nothing signs a loose file anymore -- the agent manifest is the only
release artifact left.)

Usage
-----
  python sign_release.py --genkey [--key PATH]
      Generate a new Ed25519 keypair. Writes the PRIVATE key (raw hex) to PATH
      (default: %USERPROFILE%/.temp_monitor_signing_key) and prints the PUBLIC key
      line to paste into AgentConfig.UpdatePublicKeyHex.
      KEEP THE PRIVATE KEY SECRET AND OUT OF THE REPO. Anyone with it can push code
      that runs as admin on the whole fleet.

  python sign_release.py --sign-agent --file agent/dist/TempMonitorAgent.exe                          --agent-version 3.0.1 --agent-url <release-asset-url> [--key PATH]
      Produce and sign the agent's self-update manifest. Hashes the built exe
      (sha256), writes agent/agent.manifest.json = {version, sha256, url}, and signs
      those exact bytes -> agent/agent.manifest.json.sig. The running agent verifies
      this signature before downloading + hash-checking the binary. Commit the
      manifest + .sig to main and upload the exe to the release asset URL, or fleet
      updates stall.

  python sign_release.py --sign-client --client-version 1.0.0 --builds builds.json
      Produce and sign hub/client.manifest.json -- the catalogue the console's Download
      Client page renders and the app's own updater checks (roadmap #11). Unlike the
      agent's, this manifest holds a LIST of builds, so adding a platform is a release
      action rather than a hub and console edit. `builds.json` names each build and where
      it will be hosted; every sha256 and size is computed here from the bytes on disk.

Note: .gitattributes pins agent/agent.manifest.json, hub/client.manifest.json and their
.sig files to '-text' so git never rewrites line endings -- otherwise the committed bytes
wouldn't match what you signed and the fleet (or the download page) would reject them.
"""
import argparse
import json
import os
import sys

DEFAULT_KEY_PATH = os.path.join(os.path.expanduser("~"), ".temp_monitor_signing_key")


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
    print("Paste this key into AgentConfig.UpdatePublicKeyHex:")
    print(f'    public const string UpdatePublicKeyHex = "{raw_pub.hex()}";')


def sign_agent(path, exe_file, version, url, manifest_path):
    """Build and sign the C# agent's self-update manifest. The signed bytes are
    written verbatim to the manifest file so what's served == what was signed."""
    import hashlib

    Ed25519PrivateKey, _ = _ed25519()
    if not os.path.exists(path):
        sys.exit(f"No signing key at {path}. Run: python sign_release.py --genkey")
    if not os.path.exists(exe_file):
        sys.exit(f"No agent exe at {exe_file}. Build/publish it first.")
    if not version or not url:
        sys.exit("--sign-agent requires --agent-version and --agent-url")

    with open(exe_file, "rb") as f:
        exe_bytes = f.read()
    sha256 = hashlib.sha256(exe_bytes).hexdigest()

    manifest = {"version": version, "sha256": sha256, "url": url}
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")

    with open(path, "r", encoding="utf-8") as f:
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))
    signature = priv.sign(data)

    with open(manifest_path, "wb") as f:
        f.write(data)
    with open(manifest_path + ".sig", "w", encoding="utf-8") as f:
        f.write(signature.hex())

    print(f"Agent   : v{version} ({len(exe_bytes)} bytes, sha256 {sha256})")
    print(f"Manifest: {manifest_path} (+ .sig)")
    print(f"Asset   : upload {exe_file} to {url}")
    print("Commit the manifest + .sig together; upload the exe to the asset URL.")


def sign_client(path, builds_file, version, manifest_path, notes="", released_at=""):
    """Build and sign the CLIENT manifest -- the catalogue the console's Download Client
    page renders and the app's own updater checks (roadmap #11).

    Unlike the agent's, this manifest holds a LIST of builds. The agent has exactly one
    artifact; a client has several and will have more (Windows today, Android and iOS
    later), and a shape that assumed one would mean a hub edit and a console edit per
    platform. With a list, adding a platform is: build it, re-sign, push.

    `builds_file` is a small JSON file naming each build:

        [{"platform": "windows", "arch": "x64", "kind": "file",
          "file": "app/build/FleetHubSetup.zip",
          "url": "https://github.com/.../FleetHubSetup.zip"},
         {"platform": "ios", "kind": "link", "url": "https://testflight.apple.com/..."}]

    The sha256 and size of every `file` build are computed HERE from the bytes on disk,
    never taken from the input: a digest somebody typed is a digest that can be wrong, and
    the whole point of publishing one is that it is checkable.
    """
    import hashlib

    Ed25519PrivateKey, _ = _ed25519()
    if not os.path.exists(path):
        sys.exit(f"No signing key at {path}. Run: python sign_release.py --genkey")
    if not builds_file or not os.path.exists(builds_file):
        sys.exit("--sign-client requires --builds pointing at a build list JSON file")
    if not version:
        sys.exit("--sign-client requires --client-version")

    with open(builds_file, "r", encoding="utf-8") as f:
        wanted = json.load(f)
    if not isinstance(wanted, list) or not wanted:
        sys.exit(f"{builds_file} must be a non-empty JSON list of builds")

    builds = []
    for i, entry in enumerate(wanted):
        kind = str(entry.get("kind") or "file").lower()
        url = str(entry.get("url") or "").strip()
        if not url:
            sys.exit(f"build {i} has no url")
        build = {
            "platform": str(entry.get("platform") or "").lower(),
            "arch": str(entry.get("arch") or "").lower(),
            "kind": kind,
            "url": url,
        }
        for optional in ("label", "notes"):
            if entry.get(optional):
                build[optional] = str(entry[optional])

        if kind == "file":
            local = entry.get("file")
            if not local or not os.path.exists(local):
                sys.exit(f"build {i} ({build['platform']}): no such file {local!r}. "
                         "Build it first.")
            with open(local, "rb") as f:
                blob = f.read()
            build["filename"] = os.path.basename(local)
            build["size"] = len(blob)
            build["sha256"] = hashlib.sha256(blob).hexdigest()
            print(f"Client  : {build['platform']} {build['arch']} "
                  f"({build['size']} bytes, sha256 {build['sha256']})")
            print(f"Asset   : upload {local} to {url}")
        else:
            print(f"Client  : {build['platform']} -> {url} (link)")
        builds.append(build)

    manifest = {"version": version, "builds": builds}
    if notes:
        manifest["notes"] = notes
    if released_at:
        manifest["released_at"] = released_at
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")

    with open(path, "r", encoding="utf-8") as f:
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))
    signature = priv.sign(data)

    with open(manifest_path, "wb") as f:
        f.write(data)
    with open(manifest_path + ".sig", "w", encoding="utf-8") as f:
        f.write(signature.hex())

    print(f"Manifest: {manifest_path} (+ .sig)")
    print("Commit the manifest + .sig together; upload each asset to its URL.")


def main():
    ap = argparse.ArgumentParser(description="Sign the agent self-update manifest for Temp_Monitor.")
    ap.add_argument("--genkey", action="store_true", help="generate a new keypair instead of signing")
    ap.add_argument("--sign-agent", action="store_true", help="sign the C# agent self-update manifest")
    ap.add_argument("--agent-version", help="agent version for the manifest (with --sign-agent)")
    ap.add_argument("--agent-url", help="release-asset URL of the agent exe (with --sign-agent)")
    ap.add_argument("--manifest",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent", "agent.manifest.json"),
                    help="manifest output path (with --sign-agent)")
    ap.add_argument("--key", default=DEFAULT_KEY_PATH, help="path to the private signing key")
    ap.add_argument("--file", help="path to the built agent exe (with --sign-agent)")
    # ---- client (roadmap #11) ----
    ap.add_argument("--sign-client", action="store_true",
                    help="sign the FleetHub client download manifest")
    ap.add_argument("--client-version", help="client version (with --sign-client)")
    ap.add_argument("--builds", help="JSON file listing the client builds (with --sign-client)")
    ap.add_argument("--client-notes", default="", help="release note shown on the download page")
    ap.add_argument("--released-at", default="", help="release date shown on the download page")
    # Ships under hub/, NOT under app/. The hub's self-updater mirrors the hub/ directory
    # and nothing else, so a manifest beside the Flutter sources would never reach an
    # installed hub -- the same shape as the 1.27.x bug where packages.py was left out of
    # the runtime file list. See hub/clientrelease.py.
    ap.add_argument("--client-manifest",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "hub", "client.manifest.json"),
                    help="client manifest output path (with --sign-client)")
    args = ap.parse_args()

    if args.genkey:
        genkey(args.key)
    elif args.sign_agent:
        sign_agent(args.key, args.file, args.agent_version, args.agent_url, args.manifest)
    elif args.sign_client:
        sign_client(args.key, args.builds, args.client_version, args.client_manifest,
                    notes=args.client_notes, released_at=args.released_at)
    else:
        ap.error("nothing to do: pass --sign-agent or --sign-client "
                 "(or --genkey for one-time setup)")


if __name__ == "__main__":
    main()
