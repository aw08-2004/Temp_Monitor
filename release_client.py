#!/usr/bin/env python3
"""Build, package and sign a FleetHub client release (roadmap #11).

The agent's release is two commands and a manual upload. The client's is more steps --
version bump, pub get, analyze, test, build, zip, digest, sign, upload -- and every one of
them is a step somebody can skip at 6pm on a Friday. So it is a script, and the script
**refuses rather than warns** on the things that produce a broken release:

  * **The version has to agree in three places** -- `app/lib/version.dart` (compiled into
    the binary, and what the update check compares against), `app/pubspec.yaml`, and the
    signed manifest. `app/lib/version.dart` is the source of truth and this reads it rather
    than taking a version on the command line: a script you can hand a version to is a
    script you can hand the wrong one. Use `--set-version` to move all of them at once.
  * **Tests and the analyzer must pass.** A release script that ships a red build is worse
    than no release script, because it looks like a process. `--skip-tests` exists for a
    genuine emergency and says loudly that it was used.
  * **Every sha256 is computed from the bytes on disk**, by sign_release.py, never typed.

What it deliberately does NOT do is decide the version for you or push a git tag. Both are
judgement calls, and a release script that makes them is one nobody reads the output of.

Usage
-----
  python release_client.py
      Full release. ASKS which version to publish (offering the patch/minor/major bumps
      and naming what is currently published), then: pub get, analyze, test, build,
      package, sign. Prints the asset URL each build must be uploaded to.

  python release_client.py --version 1.2.0     # skip the prompt
  python release_client.py --keep-version      # publish the tree's version, no prompt

  python release_client.py --set-version 1.1.0
      Only move app/lib/version.dart and app/pubspec.yaml, then stop. For bumping the
      version as its own commit, separately from cutting a release.

  python release_client.py --upload
      The same, then create the GitHub release and upload the assets with `gh`.

  python release_client.py --skip-build
      Sign an already-built package. For re-signing after a manifest edit, or on a machine
      that cannot build.

Prerequisites for a Windows build (`flutter doctor` will confirm both):
  * Visual Studio with the "Desktop development with C++" workload.
  * Windows Developer Mode, for the symlink support Flutter plugins need.
Neither is needed for --set-version or --skip-build.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT, "app")
VERSION_DART = os.path.join(APP_DIR, "lib", "version.dart")
PUBSPEC = os.path.join(APP_DIR, "pubspec.yaml")
DIST_DIR = os.path.join(APP_DIR, "dist")
BUILD_DIR = os.path.join(APP_DIR, "build", "windows", "x64", "runner", "Release")
RUNNER_DIR = os.path.join(APP_DIR, "windows", "runner")

# The generated Windows runner is untracked EXCEPT for these, which carry the
# single-instance guard and the version resource -- see app/.gitignore. Each entry is
# (file, what must be in it, what must not be). `flutter create` puts the boilerplate
# back, and the boilerplate builds a client that opens a second copy of itself and
# reports com.example as its publisher.
RUNNER_CUSTOMIZATIONS = [
    ("single_instance.cpp", "kMutexName", None),
    ("single_instance.h", "ClaimSingleInstance", None),
    ("main.cpp", "ClaimSingleInstance", None),
    ("Runner.rc", "FleetHub Client", "com.example"),
]

VERSION_RE = re.compile(r"^const String clientVersion = '([^']+)';$", re.M)
# `[^\S\n]*` rather than `\s*` for the trailing run: `\s` matches newlines, so with re.M
# the old pattern swallowed the blank line after `version:` and every bump ate one more
# line of the file's spacing.
PUBSPEC_VERSION_RE = re.compile(r"^version:[^\S\n]*(\S+)[^\S\n]*$", re.M)


def die(message):
    sys.exit(f"error: {message}")


# ---------------------------------------------------------------- toolchain
def find_flutter():
    """The repo-local SDK first, then PATH.

    Local first on purpose: a checkout that carries its own SDK should build with it, so
    two people on the same commit produce the same binary rather than whatever each of
    them happens to have installed.
    """
    local = os.path.join(ROOT, "flutter_sdk", "bin",
                         "flutter.bat" if os.name == "nt" else "flutter")
    if os.path.exists(local):
        return local
    found = shutil.which("flutter")
    if found:
        return found
    die("no Flutter SDK found. Put one in ./flutter_sdk or add flutter to PATH.")


def run(command, cwd=APP_DIR, capture=False):
    print(f"\n$ {' '.join(str(c) for c in command)}")
    result = subprocess.run(command, cwd=cwd, text=True,
                            capture_output=capture)
    # stdout AND stderr, always joined. Flutter writes its most useful failures -- the
    # missing-toolchain ones -- to stderr, so a caller that inspected only stdout would
    # match nothing and report "it failed" with the reason on screen but unread.
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and capture:
        print(output)
    return result.returncode, output


# ---------------------------------------------------------------- runner
def check_runner_customizations():
    """Refuse to release a runner that has been regenerated over.

    The same shape as check_versions_agree, and for the same reason: the failure it
    guards is silent. `flutter create --platforms=windows .` is the documented way to
    materialise the runner, it overwrites these files, and nothing about the build that
    follows looks wrong -- you get a client that happily opens a second copy of itself
    and calls its publisher com.example, signed and published before anyone notices.
    """
    broken = []
    for name, required, forbidden in RUNNER_CUSTOMIZATIONS:
        path = os.path.join(RUNNER_DIR, name)
        if not os.path.exists(path):
            broken.append(f"{name} is missing")
            continue
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        if required not in body:
            broken.append(f"{name} no longer contains {required!r}")
        if forbidden and forbidden in body:
            broken.append(f"{name} still contains the placeholder {forbidden!r}")
    if broken:
        # chr(10) rather than an escape: this message is the one an operator reads at
        # the moment the release stops, so it gets real line breaks.
        line = chr(10)
        die("the Windows runner has lost its tracked customizations:" + line + "  "
            + (line + "  ").join(broken)
            + line + line
            + "This is what a re-run of `flutter create` does. Restore them with:"
            + line + "  git checkout app/windows/runner")


# ---------------------------------------------------------------- version
def read_version():
    with open(VERSION_DART, "r", encoding="utf-8") as f:
        match = VERSION_RE.search(f.read())
    if not match:
        die(f"could not find clientVersion in {VERSION_DART}")
    return match.group(1)


def read_pubspec_version():
    with open(PUBSPEC, "r", encoding="utf-8") as f:
        match = PUBSPEC_VERSION_RE.search(f.read())
    return match.group(1) if match else None


def set_version(version):
    """Move both files together. Never one without the other -- a binary that reports a
    version its pubspec disagrees with is a release nobody can reason about afterwards."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        die(f"{version!r} is not a MAJOR.MINOR.PATCH version")

    with open(VERSION_DART, "r", encoding="utf-8") as f:
        dart = f.read()
    dart, count = VERSION_RE.subn(
        f"const String clientVersion = '{version}';", dart)
    if count != 1:
        die("could not rewrite clientVersion")
    with open(VERSION_DART, "w", encoding="utf-8", newline="\n") as f:
        f.write(dart)

    with open(PUBSPEC, "r", encoding="utf-8") as f:
        spec = f.read()
    # The +N build number is Flutter's own and is bumped alongside, because two releases
    # sharing one build number is a thing the Play Store refuses outright later.
    current = read_pubspec_version() or "0.0.0+0"
    build_number = int(current.split("+")[1]) + 1 if "+" in current else 1
    spec, count = PUBSPEC_VERSION_RE.subn(
        f"version: {version}+{build_number}", spec)
    if count != 1:
        die("could not rewrite the pubspec version")
    with open(PUBSPEC, "w", encoding="utf-8", newline="\n") as f:
        f.write(spec)

    print(f"Version  : {version} (pubspec build {build_number})")
    print("Commit app/lib/version.dart and app/pubspec.yaml, then run the release.")


def check_versions_agree(version):
    pubspec = read_pubspec_version()
    if not pubspec:
        die("pubspec.yaml has no version")
    if pubspec.split("+")[0] != version:
        die(f"version.dart says {version} but pubspec.yaml says {pubspec}. "
            f"Run: python release_client.py --set-version {version}")


def published_version():
    """What the signed manifest in this checkout currently advertises, or None.

    Read WITHOUT verifying the signature, deliberately: this is used to tell an operator
    what they last published, not to decide whether to trust anything. The verification
    that matters happens in the hub (clientrelease.load_manifest) and in the client's own
    update check, and duplicating it here would imply this number is load-bearing.
    """
    path = os.path.join(ROOT, "hub", "client.manifest.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("version") or None
    except (ValueError, OSError):
        return None


def bump(version, part):
    major, minor, patch = (int(p) for p in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def prompt_version(current, published, ask=input):
    """Ask which version to publish, defaulting to the one in the tree.

    Interactive rather than an argument, because "which version is this" is the decision
    most easily got wrong by muscle memory -- and the failure is silent: publishing the
    version that is already out means every installed client compares it as not-newer and
    nobody is ever offered the update.

    Re-prompts on bad input rather than exiting, the same lesson install.ps1 records:
    somebody halfway through a release should not have to start again over a typo.
    """
    print(f"\nCurrent version in the tree : {current}")
    if published:
        print(f"Currently published         : {published}")
    else:
        print("Currently published         : nothing yet -- this is the first release")

    print("\nWhat should this release be?")
    print(f"  [1] {bump(current, 'patch')}   (patch -- fixes)")
    print(f"  [2] {bump(current, 'minor')}   (minor -- new features)")
    print(f"  [3] {bump(current, 'major')}   (major -- breaking changes)")
    print(f"  [k] {current}   (keep -- re-release the version already in the tree)")
    print("  or type a version like 1.4.2")

    choices = {"1": bump(current, "patch"),
               "2": bump(current, "minor"),
               "3": bump(current, "major"),
               "k": current, "": current}

    while True:
        try:
            answer = (ask("\nVersion [1]: ") or "").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # The real non-interactive guard. sys.stdin.isatty() is checked by the caller
            # but cannot be trusted alone -- under MSYS/Git Bash it reports True even with
            # stdin redirected from /dev/null, so a CI runner would hang here forever on a
            # prompt nobody can see. An EOF is unambiguous, and the safe answer to it is
            # "publish what is in the tree", never "guess a bump".
            print(f"\n  no input available -- publishing the tree's version ({current}). "
                  f"Pass --version to choose one.")
            return current
        # Blank picks the SUGGESTED patch bump rather than "keep", because keeping is the
        # answer that silently publishes nothing new -- the mistake this prompt exists to
        # prevent should not also be the one you get by pressing Enter.
        chosen = choices.get(answer or "1")
        if chosen is None:
            if re.fullmatch(r"\d+\.\d+\.\d+", answer):
                chosen = answer
            else:
                print(f"  {answer!r} is not one of the options or a MAJOR.MINOR.PATCH "
                      f"version. Try again.")
                continue

        if published and _cmp_versions(chosen, published) <= 0:
            # Not refused outright -- re-publishing after a botched upload is legitimate --
            # but named, because the consequence is invisible: no installed client will
            # ever offer an update to a version that is not strictly newer.
            print(f"  !! {chosen} is not newer than the published {published}. "
                  f"No installed client will be offered it.")
            confirm = (ask("     Publish it anyway? [y/N]: ") or "").strip().lower()
            if confirm not in ("y", "yes"):
                continue
        return chosen


def _cmp_versions(a, b):
    """Numeric comparison, matching the hub's cmp_versions and the client's
    compareVersions -- 2.10.1 is newer than 2.9.9, which a string compare gets wrong."""
    def parts(v):
        return [int(p) if p.isdigit() else -1 for p in v.split("-")[0].split(".")]
    left, right = parts(a), parts(b)
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else 0
        r = right[i] if i < len(right) else 0
        if l != r:
            return -1 if l < r else 1
    return 0


# ---------------------------------------------------------------- build
def build(flutter, skip_tests):
    code, _ = run([flutter, "pub", "get"])
    if code:
        die("flutter pub get failed")

    code, _ = run([flutter, "analyze"], capture=True)
    if code:
        die("flutter analyze reported problems. Fix them, or the release ships them.")

    if skip_tests:
        print("\n!! --skip-tests: this release has NOT been tested. Say so in the notes.")
    else:
        code, _ = run([flutter, "test"])
        if code:
            die("flutter test failed. A red build is not a release.")

    code, output = run([flutter, "build", "windows", "--release"], capture=True)
    if code:
        # Each of these produces a wall of CMake output that says nothing about the cause,
        # so each is matched on the ONE line that identifies it.
        #
        # Matched on exact sentences rather than on keywords, and that is a fix rather
        # than a preference: an earlier version matched "symlink" anywhere in the output,
        # and EVERY plugin path contains `.plugin_symlinks` -- so a missing ATL header was
        # confidently diagnosed as "Developer Mode is off" on a machine where Developer
        # Mode was already on, and the person believed it. A wrong hint costs more than no
        # hint, so the fallback below says "no known cause" rather than guessing.
        hints = []
        if "requires symlink support" in output:
            hints.append("Windows Developer Mode is off (Flutter plugins need symlinks). "
                         "Run: start ms-settings:developers")
        if "atlstr.h" in output or "atlbase.h" in output:
            hints.append("Visual Studio is missing the C++ ATL component, which "
                         "flutter_secure_storage_windows includes. In the Visual Studio "
                         "Installer, tick 'C++ ATL for latest v143 build tools "
                         "(x86 & x64)' under Desktop development with C++.")
        if "Unable to locate Visual Studio" in output or "Build tools" in output:
            hints.append("Visual Studio with the 'Desktop development with C++' workload "
                         "may be missing. Run: flutter doctor")
        if not hints:
            hints.append("No known cause matched -- the compiler output above is the real "
                         "answer. `flutter doctor -v` is the next place to look.")
        # After the captured output, not before it: a diagnosis printed forty lines above
        # the thing it diagnoses is one nobody connects to the failure.
        sys.stdout.flush()
        for hint in hints:
            print(f"  hint: {hint}", file=sys.stderr)
        die("flutter build windows failed")


def package(version):
    """Zip the release directory. A zip rather than an installer because there is no
    code-signing certificate yet -- an unsigned installer is a SmartScreen warning, and an
    unsigned zip is a folder. Swap this for MSIX when a certificate exists."""
    if not os.path.isdir(BUILD_DIR):
        die(f"no build output at {BUILD_DIR}. Run without --skip-build first.")

    os.makedirs(DIST_DIR, exist_ok=True)
    name = f"FleetHubClient-{version}-windows-x64.zip"
    path = os.path.join(DIST_DIR, name)
    if os.path.exists(path):
        os.unlink(path)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, _dirs, files in os.walk(BUILD_DIR):
            for filename in files:
                full = os.path.join(dirpath, filename)
                archive.write(full, os.path.relpath(full, BUILD_DIR))

    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    print(f"\nPackage  : {path}")
    print(f"           {os.path.getsize(path)} bytes, sha256 {digest}")
    return path


# ---------------------------------------------------------------- release
def repo_slug():
    """<owner>/<repo> from the git remote, so the asset URL is not another thing to type
    correctly. Returns None outside a checkout with an origin."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                             text=True, capture_output=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def sign(version, package_path, notes, released_at):
    slug = repo_slug() or "OWNER/REPO"
    tag = f"client-v{version}"
    url = (f"https://github.com/{slug}/releases/download/{tag}/"
           f"{os.path.basename(package_path)}")

    builds_path = os.path.join(APP_DIR, "builds.json")
    with open(builds_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump([{
            "platform": "windows",
            "arch": "x64",
            "kind": "file",
            "file": package_path,
            "url": url,
        }], f, indent=2)

    command = [sys.executable, os.path.join(ROOT, "sign_release.py"),
               "--sign-client", "--client-version", version,
               "--builds", builds_path]
    if notes:
        command += ["--client-notes", notes]
    if released_at:
        command += ["--released-at", released_at]

    code, _ = run(command, cwd=ROOT)
    if code:
        die("signing failed. Is the private key at ~/.temp_monitor_signing_key?")
    return tag, url


def upload(tag, version, package_path, notes):
    if not shutil.which("gh"):
        die("gh is not installed, so --upload cannot create the release. "
            "Upload the asset by hand to the URL printed above.")
    code, _ = run(["gh", "release", "create", tag, package_path,
                   "--title", f"FleetHub client {version}",
                   "--notes", notes or f"FleetHub client {version}"], cwd=ROOT)
    if code:
        # A tag that already exists is the common case on a re-run, and uploading to it is
        # the right recovery rather than an error.
        print("  release exists; uploading the asset to it instead")
        code, _ = run(["gh", "release", "upload", tag, package_path, "--clobber"],
                      cwd=ROOT)
        if code:
            die("gh release upload failed")


def main():
    ap = argparse.ArgumentParser(
        description="Build, package and sign a FleetHub client release.")
    ap.add_argument("--set-version", help="move version.dart + pubspec.yaml, then stop")
    ap.add_argument("--version", dest="release_version",
                    help="the version to publish; skips the prompt")
    ap.add_argument("--keep-version", action="store_true",
                    help="publish whatever is in the tree without asking")
    ap.add_argument("--skip-build", action="store_true",
                    help="package and sign what is already built")
    ap.add_argument("--skip-tests", action="store_true",
                    help="emergency escape hatch; the release is untested")
    ap.add_argument("--upload", action="store_true",
                    help="create the GitHub release and upload the asset with gh")
    ap.add_argument("--notes", default="", help="release note shown on the download page")
    ap.add_argument("--released-at", default="", help="release date, e.g. 2026-08-04")
    args = ap.parse_args()

    if args.set_version:
        set_version(args.set_version)
        return

    current = read_version()
    check_versions_agree(current)
    # Before the version prompt, not inside build(): --skip-build packages an exe that
    # was built from these same files, so it needs the check just as much.
    check_runner_customizations()

    # Which version is being published is a decision, so it is ASKED rather than inferred
    # from whatever was left in the tree. --version and --keep-version are the
    # non-interactive answers, and a non-tty (a pipe, a CI runner) takes the tree's version
    # rather than hanging on a prompt nobody will ever see.
    if args.release_version:
        version = args.release_version
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            die(f"{version!r} is not a MAJOR.MINOR.PATCH version")
    elif args.keep_version or not sys.stdin.isatty():
        version = current
        if not args.keep_version:
            print(f"note: not a terminal, so publishing the tree's version ({current}). "
                  f"Pass --version to choose one.")
    else:
        version = prompt_version(current, published_version())

    if version != current:
        set_version(version)
        # Rewritten, not just recorded: version.dart is COMPILED into the binary, so the
        # build below has to happen after this or the app would report the old version to
        # its own update check forever.
        print("\n!! app/lib/version.dart and app/pubspec.yaml were rewritten -- "
              "commit them with the manifest.")

    print(f"\nReleasing FleetHub client {version}")

    if not args.skip_build:
        build(find_flutter(), args.skip_tests)

    package_path = package(version)
    tag, url = sign(version, package_path, args.notes, args.released_at)

    if args.upload:
        upload(tag, version, package_path, args.notes)
        print("\nDone. Commit hub/client.manifest.json + .sig and deploy the hub.")
    else:
        print(f"\nNext:\n"
              f"  1. gh release create {tag} {package_path} "
              f"--title \"FleetHub client {version}\"\n"
              f"     (or run this script again with --upload)\n"
              f"  2. commit hub/client.manifest.json + hub/client.manifest.json.sig\n"
              f"  3. deploy the hub, so the Download Client page serves them\n"
              f"\nAsset URL the manifest expects:\n  {url}")


if __name__ == "__main__":
    main()
