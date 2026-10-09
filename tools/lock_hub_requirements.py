"""Rebuild hub/requirements.lock -- the exact, hash-pinned dependency set the hub's container
image installs (hub/Dockerfile, roadmap #28).

    python tools/lock_hub_requirements.py

Run it whenever hub/requirements.txt changes, and to take dependency updates into the image on
purpose. Commit the result with the change that needed it. Needs Docker and nothing else.

**Why a lock only for the image.** hub/requirements.txt stays unpinned, because the Windows
service's self-updater pip-installs it into whatever Python the box has, and pins there would
fight the operator's interpreter. An image has no such constraint and is meant to be the
release: the same tag must hold the same bytes whenever and wherever it is rebuilt. Unpinned,
two builds of one HUB_VERSION could carry different Flask or cryptography releases, and a
compromised upstream release would reach the image on its next build without anyone choosing
it. So the image installs from this file with `--require-hashes --only-binary :all:`: every
wheel checked against a hash recorded here, and no package's setup script run at build time.

**It cannot silently drift from requirements.txt.** The Dockerfile installs the lock, then runs
`pip install --no-index -r requirements.txt`: a requirement the lock is missing has nowhere to
come from, and the image build fails naming it. A forgotten re-lock is a red CI build, not an
image that crashes on import.

Resolved inside the image's own base (python:3.13-slim), so markers evaluate as they will in the
image -- pywin32's `sys_platform == "win32"` drops out, as it should. pip-compile records the
hashes of every file of each pinned version, so the lock serves amd64 and arm64 alike. pip-tools
is installed in that throwaway container only; it is not a dependency of this repository.

*Rejected:* `pip freeze` from a built image -- pins without hashes, and it only proves what one
build happened to get. Pinning requirements.txt itself -- see above.
"""
import os
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
    "# hub/Dockerfile fails the build if this file is missing a requirement.\n"
)


def main():
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


if __name__ == "__main__":
    sys.exit(main())
