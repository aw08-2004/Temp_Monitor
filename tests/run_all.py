"""Run every test module in its own process, and exit non-zero if any fails.

Why not just `pytest tests/`? These modules are standalone scripts: each sets up its
own temp database, imports `app` (which starts a process-lifetime db_writer daemon and
keeps in-memory caches), and asserts on wall-clock-relative "online/offline" state.
Sharing one process across all of them -- which is what `pytest tests/` does -- makes a
small fraction of runs flaky no matter how carefully the state is reset, because the
async writer and the clock are genuinely shared. Run per-process, each module is
deterministic (and this is how the modules were designed to run: every one has a
`__main__` block that exits non-zero on failure).

This runner also covers test_fleet / test_fleet_web / test_settings_web, whose checks
live under `__main__` rather than in `def test_*` functions -- so `pytest` collects
"no tests" from them, while running them as scripts exercises them fully.

For editing a single module, `pytest tests/test_x.py` is still the right tool: conftest.py
makes its `check()` failures report as real pytest failures.

    python tests/run_all.py          # run everything
    python tests/run_all.py -q       # summary only
"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    quiet = "-q" in sys.argv[1:]
    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if not files:
        print("no test_*.py files found")
        return 1

    failures = []
    for path in files:
        name = os.path.basename(path)
        started = time.time()
        # Each module in a fresh interpreter: no shared db_writer thread, caches, or cwd.
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True)
        elapsed = time.time() - started
        ok = proc.returncode == 0
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}  ({elapsed:.1f}s)")
        if not ok:
            failures.append(name)
            # On failure, surface the module's own output so the reason is visible.
            if not quiet:
                tail = (proc.stdout or "").strip().splitlines()[-25:]
                for line in tail:
                    print(f"      {line}")
                if proc.stderr.strip():
                    print(f"      --- stderr ---")
                    for line in proc.stderr.strip().splitlines()[-15:]:
                        print(f"      {line}")

    print()
    if failures:
        print(f"==== {len(files) - len(failures)}/{len(files)} modules passed; "
              f"FAILED: {', '.join(failures)} ====")
        return 1
    print(f"==== all {len(files)} modules passed ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
