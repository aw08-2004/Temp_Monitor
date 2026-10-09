"""Rebuild hub/requirements.lock -- the exact, hash-pinned dependency set the hub's container
image installs (hub/Dockerfile, roadmap #28) -- or check that it is still current.

    python tools/lock_hub_requirements.py           # re-lock (needs Docker)
    python tools/lock_hub_requirements.py --check   # is the lock stale? (needs nothing)

Re-lock whenever hub/requirements.txt changes, and to take dependency updates into the image on
purpose. Commit the result with the change that needed it.

**Why a lock only for the image.** hub/requirements.txt stays unpinned, because the Windows
service's self-updater pip-installs it into whatever Python the box has, and pins there would
fight the operator's interpreter. An image has no such constraint and is meant to be the
release: the same tag must hold the same bytes whenever and wherever it is rebuilt. Unpinned,
two builds of one HUB_VERSION could carry different Flask or cryptography releases, and a
compromised upstream release would reach the image on its next build without anyone choosing
it. So the image installs from this file with `--require-hashes --only-binary :all:`: every
wheel checked against a hash recorded here, and no package's setup script run at build time.

**It cannot silently drift from requirements.txt.** `--check` reads requirements.txt as the
image will (Linux, Python 3.13 -- pywin32's `sys_platform == "win32"` drops out) and names any
requirement the lock is missing or pins below what requirements.txt asks for. The image
workflow runs it before every build, and tests/test_hub_update_mode.py runs it locally, so a
forgotten re-lock is a red check rather than an image that crashes on import. It used to live
in the Dockerfile as `pip install --no-index -r requirements.txt` after the locked install,
which caught the same thing -- but an unlocked `pip install` in the image build is exactly what
a supply-chain scanner is right to flag, and the check needs no image to run.

Resolved inside the image's own base (python:3.13-slim), so markers evaluate as they will in
the image. pip-compile records the hashes of every file of each pinned version, so the lock
serves amd64 and arm64 alike. pip-tools is installed in that throwaway container only; it is
not a dependency of this repository.

*Rejected:* `pip freeze` from a built image -- pins without hashes, and it only proves what one
build happened to get. Pinning requirements.txt itself -- see above.
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "hub")
BASE_IMAGE = "python:3.13-slim"

# Run inside the container, against hub/ mounted at /hub. The header is dropped so the output
# does not depend on the command line or the container's paths, which keeps a re-lock that
# found nothing new byte-identical.
SCRIPT = (
    "set -e; "
    "pip install --quiet --root-user-action=ignore --disable-pip-version-check pip-tools; "
    "cd /hub; "
    "pip-compile --quiet --no-header --generate-hashes --allow-unsafe --strip-extras "
    "--pip-args '--only-binary :all:' "
    "--output-file /tmp/requirements.lock requirements.txt; "
    "cat /tmp/requirements.lock"
)

PREAMBLE = (
    "# The hub container image's exact dependencies, with hashes -- generated, do not edit.\n"
    "# Regenerate with `python tools/lock_hub_requirements.py` after changing requirements.txt;\n"
    "# `--check` (run by the image workflow and the hub tests) fails while this file is stale.\n"
)

# The environment requirements.txt's markers are evaluated in: the image's, not this machine's.
# A Windows-only requirement must not be demanded of a Linux image's lock, and a Linux-only one
# must not be excused because the check happens to run on Windows.
IMAGE_ENVIRONMENT = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": "3.13",
    "python_full_version": "3.13.0",
}

_LOCK_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", re.MULTILINE)


def _requirement_class():
    """packaging's Requirement, from the package if installed, else the copy every pip
    carries -- so --check runs on a bare runner or dev box without installing anything."""
    try:
        from packaging.requirements import Requirement
    except ImportError:
        from pip._vendor.packaging.requirements import Requirement
    return Requirement


def _normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def stale(requirements_text, lock_text):
    """What is wrong with `lock_text` as a lock for `requirements_text`: one line per problem,
    empty when the lock covers every requirement the image needs at a version it accepts."""
    requirement = _requirement_class()
    locked = {_normalise(name): version for name, version in _LOCK_PIN.findall(lock_text)}
    problems = []
    for raw in requirements_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        req = requirement(line)
        if req.marker is not None and not req.marker.evaluate(IMAGE_ENVIRONMENT):
            continue
        name = _normalise(req.name)
        version = locked.get(name)
        if version is None:
            problems.append(f"{name}: in requirements.txt but not in requirements.lock")
        elif req.specifier and not req.specifier.contains(version, prereleases=True):
            problems.append(f"{name}: locked at {version}, requirements.txt asks for {req.specifier}")
    return problems


def check():
    """stale() for the committed files."""
    with open(os.path.join(HUB, "requirements.txt"), encoding="utf-8") as fh:
        requirements_text = fh.read()
    with open(os.path.join(HUB, "requirements.lock"), encoding="utf-8") as fh:
        lock_text = fh.read()
    return stale(requirements_text, lock_text)


def relock():
    cmd = ["docker", "run", "--rm", "-v", f"{HUB}:/hub:ro", BASE_IMAGE, "sh", "-c", SCRIPT]
    # MSYS (Git Bash) rewrites /hub-style arguments into Windows paths unless told not to.
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env).stdout
    except FileNotFoundError:
        print("docker was not found on PATH -- this needs Docker to resolve inside the image's base.",
              file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(e.stderr or e.stdout, file=sys.stderr)
        return e.returncode or 1
    path = os.path.join(HUB, "requirements.lock")
    # LF on every platform: the image reads it on Linux, and a CRLF checkout must not make a
    # re-lock look like a change.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(PREAMBLE + out.replace("\r\n", "\n"))
    print(f"wrote {os.path.relpath(path, REPO)}")
    return 0


def main(argv):
    if "--check" in argv:
        problems = check()
        for problem in problems:
            print(f"requirements.lock is stale -- {problem}", file=sys.stderr)
        if problems:
            print("Re-lock with `python tools/lock_hub_requirements.py` and commit the result.",
                  file=sys.stderr)
            return 1
        print("requirements.lock covers requirements.txt.")
        return 0
    return relock()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
