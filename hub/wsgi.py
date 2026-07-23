"""WSGI entrypoint the hub service serves as `wsgi:application` under waitress.

Beyond wiring up `app:application`, this module carries a boot-time self-heal for the
files-only self-updater -- see _self_heal_missing_modules. It leans only on the stdlib
and its own constants, on purpose: it has to stay importable in exactly the situation
where app.py (and everything it pulls in) cannot be.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# Branch archive, duplicated from app.py's HUB_ARCHIVE_URL rather than imported -- see the
# module docstring for why this file must not depend on app.py.
HUB_ARCHIVE_URL = "https://codeload.github.com/aw08-2004/Temp_Monitor/zip/refs/heads/main"


def _copy_missing_pyfiles(src_dir, dst_dir):
    """Copy every top-level *.py in src_dir that is absent from dst_dir. ADD-ONLY: an
    existing file is never overwritten, so a genuine bug in a file we already have still
    surfaces instead of being papered over. Returns the sorted names actually copied.

    Kept pure (two dirs in, list out) so it's unit-testable without a network fetch --
    the archive download that feeds src_dir is the only part that isn't."""
    import shutil
    copied = []
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(".py"):
            continue
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            continue  # add-only
        shutil.copy2(src, dst)
        copied.append(name)
    return copied


def _self_heal_missing_modules(exc):
    """One-shot recovery for the two-phase self-updater gap.

    The files-only updater copies a fixed runtime file list out of the branch archive,
    and that list is decided by the *currently running* version. So a release that adds a
    brand-new module lands app.py -- which imports it -- without ever copying the module,
    and the next boot dies with ModuleNotFoundError before app.py's own updater thread can
    run. (Exactly how 1.34.0 -> 1.35.0 took the hub down: it added users.py/users_web.py,
    and 1.34.0's list didn't know to fetch them.)

    Running before `import app`, this pulls the branch archive and adds any *.py the
    archive has that we're missing; the caller then retries the import once. The archive is
    authoritative about what should exist -- there is no allowlist to drift, which is the
    exact fragility that caused the outage. Returns True if it added at least one file.
    Never raises: a self-heal that itself fails must not replace one traceback with another.
    """
    # A dev checkout manages its own files: a missing module there is a real bug, not a
    # half-delivered update, so don't reach out to the network and mask it.
    if os.path.isdir(os.path.join(_HERE, ".git")):
        return False
    print(f"[wsgi] '{exc}' on startup import; attempting one-shot self-heal from the archive.",
          file=sys.stderr)
    import io
    import zipfile
    import tempfile
    import shutil
    import urllib.request
    staging = tempfile.mkdtemp(prefix="hub-selfheal-")
    try:
        with urllib.request.urlopen(HUB_ARCHIVE_URL, timeout=120) as resp:
            blob = resp.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            zf.extractall(staging)
        # codeload wraps everything in a single <repo>-<branch>/ directory.
        roots = [d for d in os.listdir(staging) if os.path.isdir(os.path.join(staging, d))]
        if len(roots) != 1:
            print(f"[wsgi] self-heal: unexpected archive layout ({len(roots)} top-level dirs);"
                  " giving up.", file=sys.stderr)
            return False
        copied = _copy_missing_pyfiles(os.path.join(staging, roots[0]), _HERE)
        if copied:
            print(f"[wsgi] self-heal: added {', '.join(copied)} -- retrying startup.",
                  file=sys.stderr)
        else:
            print("[wsgi] self-heal: the archive had nothing we were missing; the failure is real.",
                  file=sys.stderr)
        return bool(copied)
    except Exception as e:
        print(f"[wsgi] self-heal failed ({e}); leaving the tree untouched.", file=sys.stderr)
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


try:
    from app import application  # noqa: F401  (waitress serves `wsgi:application`)
except ModuleNotFoundError as exc:
    if not _self_heal_missing_modules(exc):
        raise
    # Retry exactly once. Anything the archive still didn't provide is a real failure.
    from app import application  # noqa: F401

# The hub deliberately does NOT self-report its own host temperature anymore.
# The companion agent (companion.py) runs on the hub machine too and reports it
# with full sensor data, so also starting the built-in local_logger here would
# double-report the hub's hostname -- one stream with sensors, one without --
# which made the dashboard's CPU/GPU Load & Clock flicker. If you ever run the
# hub on a box that has no companion, re-enable it with:
#     from app import start_local_logger
#     start_local_logger()
