"""Tests per-metric retention (roadmap #3): the window arithmetic, the prune that blanks
aged-out metric columns, and the map that ties each column to its window.

The silent failure this file exists to catch is a prune that deletes MORE than it was asked
to. Every knob here is a number of days that becomes a `DELETE`-shaped `UPDATE ... SET col =
NULL` against the one table that holds the fleet's history, and the failure mode is not an
exception -- it is a chart that is simply empty the next time somebody opens it, weeks after
the setting was changed, with no way back. So the cases asserted are the ones where an
off-by-one or a misread setting would quietly take the wrong column or the wrong rows:

  - a window of 0 means "follow the fleet-wide one" and must prune NOTHING. It is the shipped
    default for all eight, so a bug here empties every metric on every hub on upgrade day.
  - a window LONGER than data.retention_days must be ignored rather than honoured -- the row
    is deleted there, so there is nothing to keep -- and ignoring it must not be confused
    with pruning at the longer number.
  - blanking one metric must leave its NEIGHBOURS and the row's temperature untouched. A
    reading is shared by twelve columns, and the whole design rests on being able to drop one
    without losing the sample.
  - METRIC_COLUMN_RETENTION must cover exactly READING_METRIC_COLUMNS with keys the registry
    actually has, or a window an operator sets is one the pruner never reads (or one it
    crashes on, in a background thread, on the one hub that set it).

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-metricretention-test-")
# app resolves its DB from HUB_LOG_DIR; declare it before importing app so a standalone run
# stays off the real logs/ (see test_history_metrics.py).
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app
import settings

PASS = 0
FAIL = 0

DAY = 86400
WINDOWS = tuple(s.key for s in settings.REGISTRY
                if s.key.startswith("metrics.retention_days_"))


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def reset_windows():
    settings.reset(app.DB_PATH, list(WINDOWS) + ["data.retention_days"])


def seed(machine, ages_days):
    """One reading per age, with every metric column filled. Written with raw SQL rather
    than through /api/report because the point here is the age of a row, and the ingest path
    deliberately refuses timestamps older than data.ingest_max_backdate_days."""
    now = int(time.time())
    with app.get_db_conn() as conn:
        conn.execute("DELETE FROM readings WHERE machine = ?", (machine,))
        for age in ages_days:
            ts = now - age * DAY
            columns = ", ".join(app.READING_METRIC_COLUMNS)
            placeholders = ", ".join(["?"] * len(app.READING_METRIC_COLUMNS))
            conn.execute(
                f"INSERT INTO readings(ts_text, ts_epoch, machine, temp, sensors_json, "
                f"{columns}) VALUES (?, ?, ?, ?, NULL, {placeholders})",
                (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), ts, machine, 50.0)
                + tuple(1.0 for _ in app.READING_METRIC_COLUMNS),
            )
    return now


def rows(machine):
    columns = ", ".join(("ts_epoch", "temp") + app.READING_METRIC_COLUMNS)
    with app.get_db_conn() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {columns} FROM readings WHERE machine = ? ORDER BY ts_epoch DESC",
            (machine,)).fetchall()]


# --------------------------------------------------------------------------- the map
def test_every_column_has_a_window():
    print("\n-- METRIC_COLUMN_RETENTION covers every metric column, with real keys --")
    check("one window per metric column, no extras",
          set(app.METRIC_COLUMN_RETENTION) == set(app.READING_METRIC_COLUMNS))
    check("every window is a key the registry actually has",
          all(key in settings.BY_KEY for key in app.METRIC_COLUMN_RETENTION.values()))
    # The grouping has to be the collection toggles', or the Settings page shows an operator
    # a pair of knobs that disagree about what "GPU" means.
    check("the windows group exactly as the collection toggles do",
          {col: app.METRIC_COLUMN_RETENTION[col] for col in app.READING_METRIC_COLUMNS
           }.keys() == app.METRIC_COLUMN_TOGGLE.keys())
    for column in app.READING_METRIC_COLUMNS:
        toggle = app.METRIC_COLUMN_TOGGLE[column]
        window = app.METRIC_COLUMN_RETENTION[column]
        check(f"{column}: {toggle} and {window} are the same group",
              toggle.rsplit("collect_", 1)[-1] == window.rsplit("retention_days_", 1)[-1])


# --------------------------------------------------------------------------- arithmetic
def test_cutoffs():
    print("\n-- which windows produce a cutoff, and where --")
    reset_windows()
    check("an untouched hub prunes nothing", app.metric_column_cutoffs() == {})

    now = int(time.time())
    settings.set_many(app.DB_PATH, {"metrics.retention_days_network": 7})
    buckets = app.metric_column_cutoffs(now=now)
    check("one bucket for the one window set", len(buckets) == 1)
    check("...seven days back", list(buckets) == [now - 7 * DAY])
    check("...carrying both network columns",
          sorted(buckets[now - 7 * DAY]) == ["net_rx_bps", "net_tx_bps"])

    # Two metrics dropped to the same number is the common case, and it must cost one UPDATE
    # rather than two passes over the same rows.
    settings.set_many(app.DB_PATH, {"metrics.retention_days_disk_io": 7})
    buckets = app.metric_column_cutoffs(now=now)
    check("windows that agree share a bucket", len(buckets) == 1)
    check("...with all four columns in it", len(buckets[now - 7 * DAY]) == 4)

    settings.set_many(app.DB_PATH, {"metrics.retention_days_fans": 14})
    check("windows that differ get their own", len(app.metric_column_cutoffs(now=now)) == 2)

    # The trap: a window at or above the fleet-wide one cannot be honoured, because the row
    # itself is gone at that point. Dropped, not pruned at the longer number.
    reset_windows()
    settings.set_many(app.DB_PATH, {"data.retention_days": 30,
                                    "metrics.retention_days_gpu": 90})
    check("a window longer than the fleet-wide one is ignored",
          app.metric_column_cutoffs(now=now) == {})
    settings.set_many(app.DB_PATH, {"metrics.retention_days_gpu": 30})
    check("...and so is one exactly equal to it",
          app.metric_column_cutoffs(now=now) == {})
    settings.set_many(app.DB_PATH, {"metrics.retention_days_gpu": 29})
    check("one day shorter does prune", len(app.metric_column_cutoffs(now=now)) == 1)
    reset_windows()


# --------------------------------------------------------------------------- the prune
def test_prune_blanks_only_the_aged_out_metric():
    print("\n-- the prune NULLs one metric's old columns and nothing else --")
    reset_windows()
    settings.set_many(app.DB_PATH, {"data.retention_days": 30,
                                    "metrics.retention_days_network": 7})
    seed("RETAIN-PC", (1, 5, 10, 20))
    touched = app.prune_metric_columns_once()
    check("the two rows past the window were touched", touched == 2)

    fresh, older = [r for r in rows("RETAIN-PC") if r["ts_epoch"] > time.time() - 7 * DAY], \
                   [r for r in rows("RETAIN-PC") if r["ts_epoch"] < time.time() - 7 * DAY]
    check("rows inside the window keep their network history",
          all(r["net_rx_bps"] == 1.0 and r["net_tx_bps"] == 1.0 for r in fresh))
    check("rows past it lost it",
          all(r["net_rx_bps"] is None and r["net_tx_bps"] is None for r in older))
    check("every row is still a row", len(rows("RETAIN-PC")) == 4)
    check("...still carrying its temperature",
          all(r["temp"] == 50.0 for r in rows("RETAIN-PC")))
    check("...and every other metric untouched",
          all(r[c] == 1.0 for r in rows("RETAIN-PC")
              for c in app.READING_METRIC_COLUMNS if not c.startswith("net_")))

    check("a second run has nothing left to do", app.prune_metric_columns_once() == 0)
    reset_windows()


def test_a_zero_window_prunes_nothing():
    print("\n-- 0 means follow the fleet-wide window, not delete everything --")
    reset_windows()
    seed("RETAIN-ZERO", (1, 40, 400))
    check("nothing is touched", app.prune_metric_columns_once() == 0)
    check("every metric on every row survives",
          all(r[c] == 1.0 for r in rows("RETAIN-ZERO")
              for c in app.READING_METRIC_COLUMNS))


def test_prune_respects_the_toggle_grouping():
    print("\n-- one window covers both columns of a two-column metric --")
    reset_windows()
    settings.set_many(app.DB_PATH, {"data.retention_days": 30,
                                    "metrics.retention_days_power": 3})
    seed("RETAIN-POWER", (1, 9))
    app.prune_metric_columns_once()
    old = [r for r in rows("RETAIN-POWER") if r["ts_epoch"] < time.time() - 3 * DAY][0]
    # CPU and GPU package power share one knob because they are the same measurement on two
    # chips -- a prune that took one and left the other would chart half an answer.
    check("both chips' power went", old["cpu_power_w"] is None and old["gpu_power_w"] is None)
    check("GPU temperature, on a different knob, did not", old["gpu_temp"] == 1.0)
    reset_windows()


def main():
    test_every_column_has_a_window()
    test_cutoffs()
    test_prune_blanks_only_the_aged_out_metric()
    test_a_zero_window_prunes_nothing()
    test_prune_respects_the_toggle_grouping()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
