"""Makes the suite's `check()` helper actually fail tests under pytest.

Every test module here follows the same house pattern: a `check(name, cond)` that
increments a PASS/FAIL counter and prints `[ok]`/`[XX]`, with a `__main__` block that
prints a summary and exits non-zero. Run standalone (`python tests/test_versions.py`)
that reports failures correctly.

Under pytest it did not. `check()` only incremented a counter -- it never raised -- so a
test function full of failing checks still passed. Every assertion in all eight modules
was reporting false green; `pytest tests/` could only fail on an uncaught exception.

This hook wraps each module's `check` for the duration of a test, collects the ones that
came back false, and fails the test with their names. Deliberately collect-then-fail
rather than raise on the first false: these tests are written to run every check and
print a full report, and aborting at the first failure would hide the rest.

It hooks `pytest_runtest_call` rather than using an autouse fixture so the failure lands
in the test's *call* phase and is reported as a plain FAILED. A fixture could only raise
during teardown, which pytest reports as "passed, 1 error" -- an easy signal to miss, and
still a green-looking test.

No changes to the test modules themselves, and the standalone `__main__` path is
untouched (conftest.py is only loaded by pytest).
"""
import os
import sys
import tempfile

import pytest

# Safety net: guarantee app.py's import-time init_db() lands in a throwaway dir even if a
# test module forgot to set HUB_LOG_DIR. conftest.py is imported before any test module,
# so this runs before the first `import app`. Without it, importing app during collection
# would CREATE TABLE against the real logs/temp_v2.db.
os.environ.setdefault("HUB_LOG_DIR", os.path.join(
    tempfile.mkdtemp(prefix="hub-pytest-session-"), "logs"))


@pytest.fixture(autouse=True)
def module_db(request):
    """Give each test module its own database, addressed by an absolute path.

    app.py resolves its DB from HUB_LOG_DIR (falling back to a path next to app.py), and
    each test module sets HUB_LOG_DIR to its own tmpdir before importing app. That's
    enough for a standalone `python tests/test_x.py`.

    Under `pytest tests/` it isn't: pytest imports every module up front, `app` is only
    really imported once (sys.modules caches it), so app's DB_PATH is frozen to whichever
    module imported it first and every other module would share that one database. This
    fixture re-points app.LOG_DIR/DB_PATH at the current module's tmpdir per test. app.py
    reads both as globals at call time -- including on the db_writer thread -- so the
    reassignment redirects every reader without a re-import. The paths are absolute, which
    is what removes the original cwd race. init_* are CREATE TABLE IF NOT EXISTS, so
    re-running them just ensures the schema.
    """
    app = sys.modules.get("app")
    tmpdir = getattr(request.module, "_TMPDIR", None)
    if app is None or not tmpdir:
        # Modules that don't use the app/_TMPDIR pattern manage their own state.
        yield
        return

    log_dir = os.path.join(tmpdir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    db_path = os.path.join(log_dir, "temp_v2.db")

    saved = (app.LOG_DIR, app.DB_PATH)
    app.LOG_DIR, app.DB_PATH = log_dir, db_path
    app.init_db()
    app.fleet.init_fleet_db(db_path)
    app.alerts.init_alerts_db(db_path)
    app.settings.init_settings_db(db_path)

    # Reset the process-level in-memory caches that would otherwise leak between modules
    # sharing this one cached `app`: the live-temp cache and the persist-throttle
    # timestamps (keyed by machine name, so a name reused across modules could suppress a
    # machine_info write and skew an offline/dedup assertion). Cheap and side-effect-free
    # -- every test sets whatever it needs. No timing involved, unlike draining the async
    # writer, which perturbs the wall-clock-sensitive alert tests.
    for cache_name in ("latest_temp", "_last_live_status_persist"):
        cache = getattr(app, cache_name, None)
        if isinstance(cache, dict):
            cache.clear()
    try:
        yield
    finally:
        app.LOG_DIR, app.DB_PATH = saved


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    module = getattr(item, "module", None)
    original = getattr(module, "check", None)

    # Not every module has to use the house pattern -- leave anything else alone.
    if original is None or not callable(original):
        return (yield)

    failed = []

    def recording_check(name, cond):
        if not cond:
            failed.append(name)
        return original(name, cond)

    module.check = recording_check
    try:
        result = yield
    finally:
        module.check = original

    if failed:
        listed = "\n".join(f"  - {name}" for name in failed)
        raise AssertionError(f"{len(failed)} failed check(s):\n{listed}")
    return result
