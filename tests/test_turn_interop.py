"""TURN credential interop test (roadmap #2): proves the credentials the hub mints
(remote.mint_turn_credentials) actually authenticate against a real coturn using the TURN REST
scheme -- the one part of the hub-as-TURN design that can't be verified by unit-testing the
HMAC alone.

Requires Docker and pulls the coturn image, so it is DEFAULT-SKIP: it runs only when
FLEETHUB_TURN_INTEROP=1, keeping the normal `run_all.py` suite deterministic and offline.

    FLEETHUB_TURN_INTEROP=1 python tests/test_turn_interop.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import remote

PASS = 0
FAIL = 0
SECRET = "testsecret123"
CONTAINER = "fleethub-coturn-interop-test"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _skip(reason):
    print(f"  [skip] {reason}")
    sys.exit(0)


def main():
    if os.environ.get("FLEETHUB_TURN_INTEROP") != "1":
        _skip("set FLEETHUB_TURN_INTEROP=1 to run (needs Docker + coturn image)")
    try:
        if _docker("version", timeout=15).returncode != 0:
            _skip("Docker is not available")
    except (OSError, subprocess.SubprocessError):
        _skip("Docker is not available")

    _docker("rm", "-f", CONTAINER)
    started = _docker(
        "run", "-d", "--name", CONTAINER, "coturn/coturn:4.6",
        "-n", "--no-cli", "--no-tls", "--no-dtls",
        "--use-auth-secret", f"--static-auth-secret={SECRET}", "--realm=fleethub",
        "--listening-port=3478", "--min-port=49200", "--max-port=49210",
        "--listening-ip=0.0.0.0", "--relay-ip=127.0.0.1",
        timeout=180,
    )
    if started.returncode != 0:
        _skip(f"could not start coturn: {started.stderr.strip()}")
    try:
        time.sleep(3)
        cred = remote.mint_turn_credentials(SECRET, "interop-session", ttl_seconds=600)
        check("username has the '<expiry>:<session>' shape", cred["username"].endswith(":interop-session"))

        # Correct credential: coturn authenticates and completes an allocation (a routable peer
        # target so the relay actually forms; the loopback default policy would 403 the bind).
        ok = _docker("exec", CONTAINER, "turnutils_uclient",
                     "-u", cred["username"], "-w", cred["password"],
                     "-p", "3478", "-n", "2", "-c", "-e", "8.8.8.8", "127.0.0.1", timeout=60)
        ok_out = ok.stdout + ok.stderr
        check("hub-minted credential authenticates + allocates against coturn",
              "Total transmit time" in ok_out)
        check("no auth failure with the correct credential", "401" not in ok_out)

        # Wrong password: rejected at auth, no allocation.
        bad = _docker("exec", CONTAINER, "turnutils_uclient",
                      "-u", cred["username"], "-w", "wrongpassword",
                      "-p", "3478", "-n", "2", "-c", "-e", "8.8.8.8", "127.0.0.1", timeout=60)
        bad_out = bad.stdout + bad.stderr
        check("a wrong password is rejected (no allocation)",
              "Cannot complete Allocation" in bad_out or "401" in bad_out)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        sys.exit(1 if FAIL else 0)
    finally:
        _docker("rm", "-f", CONTAINER)


if __name__ == "__main__":
    main()
