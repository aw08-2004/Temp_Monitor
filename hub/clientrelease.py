"""The signed catalogue of downloadable FleetHub client builds (roadmap #11).

The Download Client page does not list links somebody typed into a template. It renders
whatever this manifest declares, and it renders nothing at all unless the manifest's
Ed25519 signature verifies against the SAME release trust root that signs the agent's
self-update manifest (see sign_release.py). Two reasons, and both are the point:

  * A hand-maintained page of links is a page that offers an Android build three weeks
    before the APK exists, and keeps offering one that was pulled. Adding a platform
    should be a release action -- build it, re-sign, push -- with no hub edit and no
    console edit. That is what makes `builds` a list.
  * The console is a place operators already trust. A page there that hands out an
    executable is a distribution point, and an unverified one would let anybody who could
    write a file into the hub's code directory hand the whole helpdesk a binary. The
    agent's manifest is verified before a single byte is downloaded; the client's is
    verified before it is even displayed.

**The manifest lives in `hub/`, not in `app/`, and that placement is load-bearing.** The
hub's self-updater mirrors the `hub/` directory and nothing else, so a manifest sitting
beside the Flutter sources would simply never arrive on an installed hub -- the identical
shape of the 1.27.x bug where packages.py was left out of the runtime file list and the
hub died on import. The Flutter client's own self-update reads the same file from the
repository over HTTPS, exactly as the agent reads agent/agent.manifest.json.

Kept free of Flask so it can be unit-tested in isolation.
"""
import json
import os

# The fleet's release trust root, in its public half. Identical to the agent's
# AgentConfig.UpdatePublicKeyHex -- deliberately ONE key for every artifact this project
# ships, because a second signing key is a second private key to keep safe and a second
# way for a release to be signed by something nobody meant to trust.
#
# Safe to have in the repository: it verifies signatures, it cannot make them.
RELEASE_PUBLIC_KEY_HEX = "9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c"

MANIFEST_FILENAME = "client.manifest.json"
SIGNATURE_FILENAME = "client.manifest.json.sig"

#: A `file` build is bytes this hub can hand over; a `link` build is somewhere else to go.
#: iOS will only ever be the second kind -- Apple does not permit an app to be installed
#: from a link to a file -- so the distinction exists from the start rather than arriving
#: as a special case in the template later.
KIND_FILE = "file"
KIND_LINK = "link"
KINDS = (KIND_FILE, KIND_LINK)

#: Names, not an enum: the manifest is written by a release script and read by a template,
#: and a platform this hub has never heard of should still be offered rather than dropped.
#: The list is what the page knows how to LABEL, which is a different question.
KNOWN_PLATFORMS = ("windows", "macos", "linux", "android", "ios")


class ManifestError(Exception):
    """The manifest is absent, malformed, or not signed by the release key."""


def manifest_paths(code_dir):
    return (os.path.join(code_dir, MANIFEST_FILENAME),
            os.path.join(code_dir, SIGNATURE_FILENAME))


def verify_signature(manifest_bytes, signature_hex, public_key_hex=None):
    """Ed25519-verify the EXACT manifest bytes. Fails closed on every error path.

    Byte-exact rather than over a re-serialised object, for the reason .gitattributes
    pins these files to `-text`: a signature covers bytes, and a line-ending rewrite
    between signing and serving is indistinguishable from tampering -- which is the
    correct outcome, but only if nothing here quietly re-encodes the input first.
    """
    # Resolved at CALL time, not bound as a default argument: a default is evaluated once
    # at import, which would make the key impossible to re-point from a test and would
    # quietly ignore anyone who ever needed to rotate it at runtime.
    public_key_hex = public_key_hex or RELEASE_PUBLIC_KEY_HEX

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        raise ManifestError(
            "The 'cryptography' package is required to verify the client manifest. "
            "Install it with: python -m pip install cryptography")

    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(str(signature_hex or "").strip()), manifest_bytes)
    except InvalidSignature:
        return False
    except (ValueError, TypeError):
        # Malformed hex in either the key or the signature. Not "valid", and not a crash.
        return False
    return True


def _build(entry, index):
    """Normalise one build entry, or raise ManifestError naming which one is wrong."""
    if not isinstance(entry, dict):
        raise ManifestError(f"Build {index} is not an object.")

    kind = str(entry.get("kind") or KIND_FILE).strip().lower()
    if kind not in KINDS:
        raise ManifestError(f"Build {index} has an unknown kind {kind!r}.")

    url = str(entry.get("url") or "").strip()
    if not url:
        raise ManifestError(f"Build {index} has no url.")
    # The manifest is signed, so a hostile URL means the signing key is compromised and
    # this check is not the control that saves anyone. It is here so a TYPO -- a relative
    # path, a `javascript:` left over from a paste -- fails at release time rather than
    # rendering an unclickable or surprising link on the console.
    if not (url.startswith("https://") or url.startswith("http://")):
        raise ManifestError(f"Build {index} has a url that is not http(s).")

    sha256 = str(entry.get("sha256") or "").strip().lower()
    if kind == KIND_FILE:
        # A file build without a digest is a download nobody can check, which is the one
        # thing this page exists to avoid handing out.
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ManifestError(f"Build {index} has no valid sha256.")
    elif sha256:
        raise ManifestError(f"Build {index} is a link and cannot carry a sha256.")

    return {
        "platform": str(entry.get("platform") or "").strip().lower(),
        "arch": str(entry.get("arch") or "").strip().lower(),
        "kind": kind,
        "filename": str(entry.get("filename") or "").strip(),
        "size": int(entry.get("size") or 0),
        "sha256": sha256,
        "url": url,
        "label": str(entry.get("label") or "").strip(),
        "notes": str(entry.get("notes") or "").strip(),
    }


def parse_manifest(manifest_bytes):
    """Parse and validate the manifest document. Does NOT check the signature -- callers
    go through load_manifest, which does both in the right order."""
    try:
        doc = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ManifestError(f"The client manifest is not valid JSON: {e}")
    if not isinstance(doc, dict):
        raise ManifestError("The client manifest is not an object.")

    version = str(doc.get("version") or "").strip()
    if not version:
        raise ManifestError("The client manifest has no version.")

    raw_builds = doc.get("builds")
    if not isinstance(raw_builds, list) or not raw_builds:
        raise ManifestError("The client manifest lists no builds.")

    return {
        "version": version,
        "released_at": str(doc.get("released_at") or "").strip(),
        "notes": str(doc.get("notes") or "").strip(),
        "builds": [_build(b, i) for i, b in enumerate(raw_builds)],
    }


def load_manifest(code_dir, public_key_hex=None):
    """Read, verify and parse the manifest shipped beside the hub's code.

    Raises ManifestError for every failure, including "there isn't one yet" -- which is
    the state a hub is in before the first client release, and is reported as a plain
    message on the page rather than as an empty list. An empty list would read as "no
    client exists", and the two need to be told apart.
    """
    manifest_path, sig_path = manifest_paths(code_dir)
    if not os.path.exists(manifest_path):
        raise ManifestError("No client release has been published for this hub yet.")
    if not os.path.exists(sig_path):
        raise ManifestError(
            "The client manifest has no signature beside it, so it cannot be trusted.")

    with open(manifest_path, "rb") as f:
        manifest_bytes = f.read()
    with open(sig_path, "r", encoding="utf-8") as f:
        signature_hex = f.read()

    if not verify_signature(manifest_bytes, signature_hex, public_key_hex):
        raise ManifestError(
            "The client manifest's signature is not valid for this hub's release key. "
            "Nothing will be offered for download until it is re-signed.")

    return parse_manifest(manifest_bytes)
