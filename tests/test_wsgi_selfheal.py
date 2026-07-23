"""Unit tests for wsgi.py's boot-time self-heal -- the guard that recovers from the
files-only updater's two-phase gap (a release adds a module, the old updater lands
app.py but not the new module, next boot ImportErrors). See wsgi._self_heal_missing_modules.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.
Under pytest, conftest.py wraps `check` so a false condition fails the test properly.

wsgi.py does `from app import application` at import time. We don't want the real app
(wmi, socketio, background threads) just to test a filesystem helper, so we stand a stub
`app` module in front of it long enough to import wsgi, then restore what was there.
"""
import os
import sys
import types
import shutil
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub")
sys.path.insert(0, ROOT)

_saved_app = sys.modules.get("app")
_stub = types.ModuleType("app")
_stub.application = object()
sys.modules["app"] = _stub
try:
    import wsgi
finally:
    if _saved_app is not None:
        sys.modules["app"] = _saved_app
    else:
        sys.modules.pop("app", None)

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


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main():
    tmp = tempfile.mkdtemp(prefix="wsgi-selfheal-test-")
    try:
        src = os.path.join(tmp, "src")
        dst = os.path.join(tmp, "dst")
        os.makedirs(src)
        os.makedirs(dst)

        # dst already has app.py (the file the updater DID deliver) with real content.
        _write(os.path.join(dst, "app.py"), "ORIGINAL")
        # The archive (src) carries: app.py (already present -> must be skipped),
        # two new sibling modules (must be copied), a non-.py file (ignored), and a
        # nested .py under a subdir (ignored -- only top-level siblings count).
        _write(os.path.join(src, "app.py"), "NEWER-DO-NOT-COPY")
        _write(os.path.join(src, "users.py"), "u")
        _write(os.path.join(src, "users_web.py"), "uw")
        _write(os.path.join(src, "requirements.txt"), "flask")
        os.makedirs(os.path.join(src, "templates"))
        _write(os.path.join(src, "templates", "buried.py"), "nope")

        print("\n== _copy_missing_pyfiles ==")
        copied = wsgi._copy_missing_pyfiles(src, dst)
        check("copies missing users.py", os.path.exists(os.path.join(dst, "users.py")))
        check("copies missing users_web.py", os.path.exists(os.path.join(dst, "users_web.py")))
        check("returns exactly the copied names, sorted",
              copied == ["users.py", "users_web.py"])
        check("add-only: existing app.py is NOT overwritten",
              _read(os.path.join(dst, "app.py")) == "ORIGINAL")
        check("ignores non-.py files", not os.path.exists(os.path.join(dst, "requirements.txt")))
        check("ignores nested .py (top-level siblings only)",
              not os.path.exists(os.path.join(dst, "buried.py")))
        check("second run is a no-op (idempotent)",
              wsgi._copy_missing_pyfiles(src, dst) == [])

        print("\n== dev-checkout guard ==")
        # wsgi._WORKTREE_ROOT is the repo root (parent of the hub/ code dir), a .git checkout
        # in dev/CI -- so the heal must refuse to phone home and mask what is a real bug
        # there. No network hit.
        check("self-heal is a no-op inside a .git checkout",
              wsgi._self_heal_missing_modules(ModuleNotFoundError("boom")) is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
