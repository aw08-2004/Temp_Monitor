"""Tests settings.py: the registry, the sparse table, coercion/validation, the
copy-on-write cache, and the agent-config hash.

The most important test here is test_defaults_match_the_old_constants. Every default
is asserted against a HARDCODED literal, deliberately not imported from settings --
the whole safety property of this module is "an empty settings table behaves exactly
like the hub did before settings existed", and that only holds while the defaults
equal the constants they replaced. Importing the values would make the test agree
with whatever the registry currently says, which is precisely the bug it exists to catch.

Run from the repo root so `import settings` resolves.
"""
import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings

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


def fresh_db():
    """A new settings DB with a cold cache. The cache is module-global, so every test
    that switches DB must invalidate or it reads the previous test's values."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    settings.init_settings_db(db_path)
    settings.invalidate()
    return db_path


# --------------------------------------------------------------------------- defaults
def test_defaults_match_the_old_constants():
    print("\n-- registry defaults equal the constants they replaced --")
    db = fresh_db()
    # Literals on purpose -- see the module docstring.
    expected = {
        "hub.overheat_threshold": 85,             # was app.OVERHEAT_THRESHOLD
        "hub.low_load_threshold": 40,             # was app.LOW_LOAD_THRESHOLD
        "hub.live_status_cache_seconds": 600,     # was app.LIVE_STATUS_CACHE_SECONDS
        "hub.live_default_window_hours": 3,       # was app.LIVE_DEFAULT_WINDOW_HOURS
        "data.retention_days": 30,                # was app.RETENTION_DAYS
        "data.prune_interval_seconds": 86400,     # was app.RETENTION_PRUNE_INTERVAL_SECONDS
        "data.ingest_max_backdate_days": 30,      # was app.RETENTION_DAYS, now split out
        "data.command_output_retention_seconds": 86400,  # was fleet.OUTPUT_RETENTION_SECONDS
        "fleet.dashboard_online_window_seconds": 120,    # was app.DASHBOARD_ONLINE_WINDOW_SECONDS
        "fleet.offline_after_seconds": 90,        # was fleet.DEFAULT_OFFLINE_AFTER_SECONDS
        "fleet.command_ttl_seconds": 900,         # was fleet.DEFAULT_COMMAND_TTL_SECONDS
    }
    for key, want in expected.items():
        check(f"{key} defaults to {want}", settings.get(db, key) == want)

    check("hub.auto_update defaults to None (follow .env)",
          settings.get(db, "hub.auto_update") is None)
    check("sensor preference matches SensorReader.cs PreferredSensors",
          settings.get(db, "computer.primary_sensor_preference") ==
          ["cpu package", "core (tctl/tdie)", "core average", "core max", "cpu cores"])
    check("every history-metric collection toggle defaults on",
          all(settings.get(db, k) is True for k in (
              "metrics.collect_cpu_load", "metrics.collect_memory",
              "metrics.collect_gpu", "metrics.collect_disk", "metrics.collect_network")))


def test_init_is_idempotent():
    print("\n-- init_settings_db is idempotent --")
    db = fresh_db()
    try:
        settings.init_settings_db(db)
        settings.init_settings_db(db)
        check("second and third init do not raise", True)
    except Exception as e:
        check(f"second init raised: {e}", False)


def test_table_is_sparse():
    print("\n-- only overridden keys get rows --")
    db = fresh_db()
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    check("empty table on a fresh hub", count == 0)
    check("but every key still resolves", settings.get(db, "data.retention_days") == 30)

    settings.set_many(db, {"data.retention_days": 45}, updated_by="op@x.com")
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT key, updated_by FROM settings").fetchall()
    check("exactly one row after one override", len(rows) == 1)
    check("row records who changed it", rows[0][1] == "op@x.com")


# --------------------------------------------------------------------------- set / reset
def test_set_and_reset():
    print("\n-- set, read back, reset to default --")
    db = fresh_db()
    settings.set_many(db, {"fleet.dashboard_online_window_seconds": 300})
    check("value reads back after set",
          settings.get(db, "fleet.dashboard_online_window_seconds") == 300)
    check("schema marks it non-default",
          _field(db, "fleet", "fleet.dashboard_online_window_seconds")["is_default"] is False)

    removed = settings.reset(db, ["fleet.dashboard_online_window_seconds"])
    check("reset reports the key it removed",
          removed == ["fleet.dashboard_online_window_seconds"])
    check("value falls back to the default",
          settings.get(db, "fleet.dashboard_online_window_seconds") == 120)
    check("resetting an already-default key is a no-op",
          settings.reset(db, ["fleet.dashboard_online_window_seconds"]) == [])


def test_coercion():
    print("\n-- JSON type sloppiness is coerced, garbage is rejected --")
    db = fresh_db()
    settings.set_many(db, {"hub.overheat_threshold": "95"})
    check('"95" coerces to int 95', settings.get(db, "hub.overheat_threshold") == 95)

    settings.set_many(db, {"hub.auto_update": 1})
    check("1 coerces to True", settings.get(db, "hub.auto_update") is True)
    settings.set_many(db, {"hub.auto_update": "off"})
    check('"off" coerces to False', settings.get(db, "hub.auto_update") is False)
    settings.set_many(db, {"hub.auto_update": None})
    check("None stays None (tri-state)", settings.get(db, "hub.auto_update") is None)

    settings.set_many(db, {"computer.primary_sensor_preference": ["CPU Package", " Core Max "]})
    check("sensor names are lowercased and trimmed",
          settings.get(db, "computer.primary_sensor_preference") == ["cpu package", "core max"])

    check("non-numeric int is rejected", _rejects(db, {"hub.overheat_threshold": "hot"}))
    check("non-list for a str_list is rejected",
          _rejects(db, {"computer.primary_sensor_preference": "cpu package"}))
    check("empty str_list is rejected",
          _rejects(db, {"computer.primary_sensor_preference": ["", "  "]}))
    check("unparseable bool is rejected", _rejects(db, {"hub.auto_update": "maybe"}))


def test_range_validation():
    print("\n-- bounds are enforced at the edges --")
    db = fresh_db()
    # hub.overheat_threshold is min=40 max=120.
    check("minimum is accepted", _accepts(db, {"hub.overheat_threshold": 40}))
    check("maximum is accepted", _accepts(db, {"hub.overheat_threshold": 120}))
    check("below minimum is rejected", _rejects(db, {"hub.overheat_threshold": 39}))
    check("above maximum is rejected", _rejects(db, {"hub.overheat_threshold": 121}))

    try:
        settings.set_many(db, {"hub.overheat_threshold": 5})
        check("out-of-range message names the field", False)
    except ValueError as e:
        check("out-of-range message names the field", "Overheat threshold" in str(e))
        check("out-of-range message states the bound", "40" in str(e))


def test_set_many_is_all_or_nothing():
    print("\n-- one bad field rejects the whole batch --")
    db = fresh_db()
    settings.set_many(db, {"data.retention_days": 60})
    try:
        settings.set_many(db, {
            "data.retention_days": 90,             # valid
            "hub.overheat_threshold": 9999,        # out of range
            "hub.low_load_threshold": 50,          # valid
        })
        check("batch with an invalid field raises", False)
    except ValueError:
        check("batch with an invalid field raises", True)
    check("the valid field in the batch was NOT applied",
          settings.get(db, "data.retention_days") == 60)
    check("the other valid field was NOT applied",
          settings.get(db, "hub.low_load_threshold") == 40)


def test_unknown_keys():
    print("\n-- the registry is the allow-list --")
    db = fresh_db()
    check("unknown key is rejected", _rejects(db, {"hub.not_a_real_knob": 1}))
    # The guard that keeps secrets out of this table.
    check("a secret-looking key is rejected too",
          _rejects(db, {"AGENT_ENROLLMENT_SECRET": "hunter2"}))

    # A row left behind by a knob removed in a later version must not break startup.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at, updated_by) "
            "VALUES ('hub.retired_knob', '42', 0, NULL)")
    settings.invalidate()
    check("stale row for a removed knob is ignored, not fatal",
          settings.get(db, "data.retention_days") == 30)

    # Same for a row whose JSON got corrupted.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json, updated_at, updated_by) "
            "VALUES ('data.retention_days', 'not json', 0, NULL)")
    settings.invalidate()
    check("corrupt value falls back to the default",
          settings.get(db, "data.retention_days") == 30)


# --------------------------------------------------------------------------- cache
def test_cache_invalidation():
    print("\n-- the cache reflects writes without a manual reload --")
    db = fresh_db()
    check("cold read gives the default", settings.get(db, "data.retention_days") == 30)
    settings.set_many(db, {"data.retention_days": 7})
    check("read after set gives the new value",
          settings.get(db, "data.retention_days") == 7)
    settings.reset(db, ["data.retention_days"])
    check("read after reset gives the default again",
          settings.get(db, "data.retention_days") == 30)


def test_as_dict_is_a_copy():
    print("\n-- as_dict hands out a copy, not the live cache --")
    db = fresh_db()
    snapshot = settings.as_dict(db)
    snapshot["data.retention_days"] = 999
    check("mutating the returned dict does not corrupt the cache",
          settings.get(db, "data.retention_days") == 30)


def test_concurrent_reads_during_writes():
    """Readers must never observe a torn state -- every read is either wholly the old
    value or wholly the new one, and no read raises. This is the property the
    copy-on-write design exists to provide."""
    print("\n-- concurrent readers see no torn state --")
    db = fresh_db()
    settings.set_many(db, {"data.retention_days": 10})

    seen = []
    errors = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                seen.append(settings.get(db, "data.retention_days"))
        except Exception as e:      # noqa: BLE001 -- the point is to catch anything
            errors.append(e)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    for value in (20, 30, 40, 50, 60):
        settings.set_many(db, {"data.retention_days": value})
    stop.set()
    for t in readers:
        t.join(timeout=5)

    check("no reader raised", not errors)
    check("readers actually ran", len(seen) > 0)
    check("every observed value is one that was actually set",
          all(v in (10, 20, 30, 40, 50, 60) for v in seen))


# --------------------------------------------------------------------------- schema
def test_schema_shape():
    print("\n-- schema carries everything the UI renders from --")
    db = fresh_db()
    doc = settings.schema(db)
    names = [s["name"] for s in doc["sections"]]
    check("all sections present in order",
          names == ["computer", "hub", "data", "metrics", "fleet"])
    check("sections carry display labels",
          any(s["label"] == "Data & Retention" for s in doc["sections"]))

    field = _field(db, "data", "data.retention_days")
    for attr in ("key", "label", "type", "value", "default", "is_default",
                 "min", "max", "unit", "help", "choices", "agent"):
        check(f"field exposes {attr}", attr in field)
    check("field carries its bounds", field["min"] == 1 and field["max"] == 3650)
    check("field carries its unit", field["unit"] == "days")
    check("help text warns that retention deletes",
          "PERMANENTLY DELETED" in field["help"])

    check("every registry key appears exactly once in the schema",
          sorted(f["key"] for s in doc["sections"] for f in s["fields"]) ==
          sorted(settings.BY_KEY))


# --------------------------------------------------------------------------- agent config
def test_agent_config():
    print("\n-- agent config channel --")
    db = fresh_db()
    config = settings.agent_config(db)
    check("only agent=True keys are shipped",
          set(config) == {"computer.primary_sensor_preference", "metrics.collect_network"})
    check("the network collection toggle ships to agents",
          config["metrics.collect_network"] is True)
    check("no hub-internal knob leaks to agents",
          "data.retention_days" not in config)
    check("a hub-only metric toggle does NOT ship to agents",
          "metrics.collect_disk" not in config)

    version = settings.agent_config_version(db)
    check("version is a short hex string", len(version) == 16)
    check("version is stable for identical content",
          settings.agent_config_version(db) == version)

    # A non-agent setting must NOT churn the fleet's config version.
    settings.set_many(db, {"data.retention_days": 45})
    check("changing a hub-only knob leaves the agent version untouched",
          settings.agent_config_version(db) == version)

    settings.set_many(db, {"computer.primary_sensor_preference": ["core max"]})
    changed = settings.agent_config_version(db)
    check("changing an agent knob changes the version", changed != version)

    # Content-derived, not a counter: reverting must hash back to the original, so
    # agents that never saw the intermediate value don't re-apply anything.
    settings.set_many(db, {
        "computer.primary_sensor_preference": settings.DEFAULT_SENSOR_PREFERENCE})
    check("reverting restores the original version (content hash, not a counter)",
          settings.agent_config_version(db) == version)


# --------------------------------------------------------------------------- helpers
def _field(db, section, key):
    doc = settings.schema(db)
    for s in doc["sections"]:
        if s["name"] != section:
            continue
        for f in s["fields"]:
            if f["key"] == key:
                return f
    return {}


def _rejects(db, updates):
    try:
        settings.set_many(db, updates)
        return False
    except ValueError:
        return True


def _accepts(db, updates):
    try:
        settings.set_many(db, updates)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    test_defaults_match_the_old_constants()
    test_init_is_idempotent()
    test_table_is_sparse()
    test_set_and_reset()
    test_coercion()
    test_range_validation()
    test_set_many_is_all_or_nothing()
    test_unknown_keys()
    test_cache_invalidation()
    test_as_dict_is_a_copy()
    test_concurrent_reads_during_writes()
    test_schema_shape()
    test_agent_config()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
