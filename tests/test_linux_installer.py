"""The Linux agent installer's signed-manifest check (agent-linux/install/install.sh).

**The silent failure this file exists to catch is an installer that runs an unverified binary
as root while every self-update after it is verified.** The fleet key protects each update the
agent applies to itself; the installer is the one step before the agent exists, and it used to
check only that the download began with the ELF magic. Every enrolled Linux box re-runs it once
to reach 0.3.0 (0.1.0 and 0.2.0 have no updater), so a gap here is a gap across the fleet.

What is pinned, and why each one fails quietly if it drifts:

  * the key in install.sh equals AgentConfig.UpdatePublicKeyHex in BOTH agents -- a mismatch
    would refuse every genuine release, and the obvious "fix" in the field is --agent-url;
  * verification accepts a REAL production signature: the Windows manifest committed in this
    repo is signed by the same fleet key, so this proves the shell path agrees with the agents'
    BouncyCastle verifier on real bytes without the private key being anywhere near a test;
  * it refuses a tampered manifest, a malformed or truncated signature, a binary whose sha256
    differs from the signed one, an unsigned manifest and a missing one -- and in every refusal
    the destination file is never produced;
  * --agent-url still works but says it is unverified.

The shell functions are loaded without running main(): the script's last line is `main "$@"`,
and the harness drops only that line. Tests substitute their own key AFTER loading, the way a
test replaces a constant; the script itself accepts no key from outside.
"""
import hashlib
import http.server
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(ROOT, "agent-linux", "install", "install.sh")

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


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def call(func, *args, key=None, manifest_url=None):
    """Run one install.sh function in a bash that has loaded the script minus `main "$@"`.

    Overrides are assigned AFTER loading, because the script initialises its own globals when
    it loads -- an environment variable set beforehand is simply overwritten. (The first
    version of this harness passed the manifest URL that way, and every download case quietly
    fetched the real GitHub URL instead of the test server.)"""
    overrides = ""
    if key:
        overrides += f'UPDATE_PUBLIC_KEY_HEX="{key}"\n'
    if manifest_url:
        overrides += f'MANIFEST_URL_OVERRIDE="{manifest_url}"\n'
    script = (
        'set -u\n'
        f'eval "$(sed \'/^main "\\$@"/d\' "{INSTALLER}")"\n'
        f'{overrides}'
        '"$@"\n'
    )
    # The test server is on 127.0.0.1; a machine behind an HTTP proxy would otherwise send
    # curl's request for it to the proxy, and every download case would fail for a reason that
    # has nothing to do with the installer.
    env = dict(os.environ)
    for var in ("no_proxy", "NO_PROXY"):
        env[var] = ",".join(filter(None, ["127.0.0.1", "localhost", env.get(var, "")]))
    return subprocess.run(["bash", "-c", script, "harness", func, *args],
                          capture_output=True, text=True, timeout=60, env=env)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory):
    handler = lambda *a, **k: _Quiet(*a, directory=directory, **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main():
    print("\n== The installer's key is the agents' key ==")
    installer = read("agent-linux", "install", "install.sh")
    ours = re.search(r'^UPDATE_PUBLIC_KEY_HEX="([0-9a-f]{64})"', installer, re.M)
    check("install.sh pins a 64-hex-digit key", ours is not None)
    for label, path in (("Linux", ("agent-linux", "src", "FleetHubAgent", "AgentConfig.cs")),
                        ("Windows", ("agent", "src", "TempMonitorAgent", "AgentConfig.cs"))):
        theirs = re.search(r'UpdatePublicKeyHex\s*=\s*"([0-9a-f]{64})"', read(*path))
        check(f"...and it equals the {label} agent's UpdatePublicKeyHex",
              ours and theirs and ours.group(1) == theirs.group(1))
    check("the key is not read from any flag or environment variable",
          not re.search(r'UPDATE_PUBLIC_KEY_HEX=["$]*\{|UPDATE_PUBLIC_KEY_HEX="\$', installer))

    work = tempfile.mkdtemp()
    try:
        print("\n== A real production signature verifies ==")
        manifest = os.path.join(ROOT, "agent", "agent.manifest.json")
        sig = manifest + ".sig"
        r = call("verify_manifest", manifest, sig, work)
        check("the committed Windows manifest verifies under the fleet key", r.returncode == 0)
        tampered = os.path.join(work, "tampered.json")
        with open(manifest, "rb") as fh:
            data = fh.read()
        with open(tampered, "wb") as fh:
            fh.write(data.replace(b'"version":"', b'"version":"9'))
        check("one changed byte is refused",
              call("verify_manifest", tampered, sig, work).returncode != 0)
        for name, body in (("empty", ""), ("non-hex", "zz" * 64),
                           ("truncated", open(sig).read()[:100]),
                           ("odd-length", open(sig).read().strip()[:-1])):
            bad = os.path.join(work, f"sig-{name}")
            with open(bad, "w") as fh:
                fh.write(body)
            check(f"a {name} signature is refused",
                  call("verify_manifest", manifest, bad, work).returncode != 0)
        padded = os.path.join(work, "sig-padded")
        with open(padded, "w") as fh:
            fh.write(open(sig).read().strip() + "\n\n")
        check("trailing newlines on the .sig are tolerated",
              call("verify_manifest", manifest, padded, work).returncode == 0)

        print("\n== The whole download path, against a local server and a test key ==")
        key = Ed25519PrivateKey.generate()
        pub_hex = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        site = os.path.join(work, "site")
        os.makedirs(site)
        binary = b"\x7fELF" + os.urandom(4096)
        with open(os.path.join(site, "fleethub-agent"), "wb") as fh:
            fh.write(binary)
        server, base = serve(site)

        def publish(name, sha, signer=key):
            body = ('{"sha256":"%s","url":"%s/fleethub-agent","version":"0.3.0"}'
                    % (sha, base)).encode()
            with open(os.path.join(site, name), "wb") as fh:
                fh.write(body)
            with open(os.path.join(site, name + ".sig"), "w") as fh:
                fh.write(signer.sign(body).hex() + "\n")
            return f"{base}/{name}"

        def install(manifest_url, tag):
            dest = os.path.join(work, f"out-{tag}")
            step = os.path.join(work, f"step-{tag}")
            os.makedirs(step)
            r = call("fetch_verified_binary", dest, step, key=pub_hex,
                     manifest_url=manifest_url)
            return r, dest

        try:
            good = publish("good.json", hashlib.sha256(binary).hexdigest())
            r, dest = install(good, "good")
            check("a signed manifest whose sha256 matches installs", r.returncode == 0
                  and open(dest, "rb").read() == binary)
            check("...and says it verified", "manifest verified" in r.stdout
                  and "sha256 matches" in r.stdout)

            wrong = publish("wrong-sha.json", "0" * 64)
            r, dest = install(wrong, "wrong")
            check("a binary that differs from the signed sha256 is refused",
                  r.returncode != 0 and "does not match the signed manifest" in r.stderr)

            stranger = publish("unsigned.json", hashlib.sha256(binary).hexdigest(),
                               signer=Ed25519PrivateKey.generate())
            r, dest = install(stranger, "stranger")
            check("a manifest signed by any other key is refused, and nothing is written",
                  r.returncode != 0 and "NOT signed by the fleet's key" in r.stderr
                  and not os.path.exists(dest))

            r, dest = install(f"{base}/missing.json", "missing")
            check("no manifest published is a clear refusal, not an unverified fallback",
                  r.returncode != 0 and f"{base}/missing.json" in r.stderr
                  and "--binary" in r.stderr and not os.path.exists(dest))
        finally:
            server.shutdown()

        print("\n== The unverified paths say so ==")
        check("--agent-url prints that it is not checked",
              "NOT checked against the fleet's signing key" in installer)
        check("--binary is labelled as not checked",
              "local, not checked against the fleet key" in installer)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
