"""Tests hub-side primary-temperature re-derivation (app.pick_primary_temp and the
resolve/override path around it).

The load-bearing case is test_absent_sensor_defers_to_agent. When the configured sensor
isn't in the block, the picker must return None so the caller keeps the temperature the
AGENT chose. The tempting alternative -- fall back to any CPU temperature, the way
SensorReader does on the endpoint -- would let a renamed sensor silently swap a real
91 °C package reading for a 28 °C board probe and stop every overheat alert on that
machine. That failure is silent, plausible-looking, and safety-relevant, so it gets an
explicit test asserting None rather than "something reasonable".

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMPDIR = tempfile.mkdtemp(prefix="hub-sensorpick-test-")
# See test_alerts.py: app resolves its DB from HUB_LOG_DIR, so declare this module's dir
# before importing app to keep a standalone run off the real logs/.
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
# The session user these tests sign in as has to be a break-glass superuser, or every
# console endpoint below now 403s on the permission-group layer. Set before importing
# app, which reads ALLOWED_EMAILS at import time; load_dotenv doesn't override an
# already-set env var, so this beats the real .env.
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app
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


def sensor(name, value, hardware_id="/amdcpu/0", stype="Temperature", hardware="Ryzen 7"):
    return {"name": name, "value": value, "type": stype,
            "hardware_id": hardware_id, "hardware": hardware}


# A realistic block: two CPU temps, a GPU temp, a board probe, and a load sensor.
BLOCK = [
    sensor("Core (Tctl/Tdie)", 91.0),
    sensor("CPU Package", 88.5),
    sensor("Core Max", 93.0),
    sensor("GPU Core", 45.0, hardware_id="/gpu-nvidia/0", hardware="RTX 3060"),
    sensor("Temperature #1", 28.0, hardware_id="/lpc/nct6798d", hardware="Motherboard"),
    sensor("CPU Total", 12.0, stype="Load"),
]

DEFAULTS = ["cpu package", "core (tctl/tdie)", "core average", "core max", "cpu cores"]


def test_preference_order():
    print("\n-- the preference list is honoured in order --")
    check("first preference wins",
          app.pick_primary_temp(BLOCK, preferred=DEFAULTS) == 88.5)
    check("reordering changes the pick",
          app.pick_primary_temp(BLOCK, preferred=["core (tctl/tdie)", "cpu package"]) == 91.0)
    check("an earlier preference beats a later one",
          app.pick_primary_temp(BLOCK, preferred=["core max", "cpu package"]) == 93.0)
    check("preferences that match nothing skip to the next",
          app.pick_primary_temp(BLOCK, preferred=["nonexistent", "cpu package"]) == 88.5)
    check("matching is case-insensitive",
          app.pick_primary_temp(BLOCK, preferred=["CPU PACKAGE"]) is None)  # list is pre-lowered
    check("substring matching works (fuzzy, cross-vendor)",
          app.pick_primary_temp(BLOCK, preferred=["package"]) == 88.5)


def test_explicit_override():
    print("\n-- the per-machine override matches exactly, not fuzzily --")
    check("exact name matches",
          app.pick_primary_temp(BLOCK, preferred=DEFAULTS, explicit="core max") == 93.0)
    check("exact match is case-insensitive",
          app.pick_primary_temp(BLOCK, preferred=DEFAULTS, explicit="Core Max") == 93.0)
    check("surrounding whitespace is tolerated",
          app.pick_primary_temp(BLOCK, preferred=DEFAULTS, explicit="  core max  ") == 93.0)
    # A substring must NOT match an override: the operator picked a real name from a
    # dropdown, so a partial hit means the sensor they chose is gone.
    check("a substring does NOT satisfy an override",
          app.pick_primary_temp(BLOCK, preferred=DEFAULTS, explicit="core") is None)
    check("the override beats the preference list",
          app.pick_primary_temp(BLOCK, preferred=["cpu package"], explicit="core max") == 93.0)


def test_absent_sensor_defers_to_agent():
    print("\n-- nothing matched => None, so the caller keeps the agent's pick --")
    check("missing override returns None, NOT another CPU temp",
          app.pick_primary_temp(BLOCK, preferred=DEFAULTS, explicit="no such sensor") is None)
    check("no preference matches returns None",
          app.pick_primary_temp(BLOCK, preferred=["nothing", "nowhere"]) is None)
    check("empty preference list returns None",
          app.pick_primary_temp(BLOCK, preferred=[]) is None)
    check("empty sensor block returns None", app.pick_primary_temp([], preferred=DEFAULTS) is None)
    check("None sensor block returns None", app.pick_primary_temp(None, preferred=DEFAULTS) is None)

    # And the wrapper actually falls back to the reported value.
    check("resolve_primary_temp keeps the agent's temp when nothing matches",
          app.resolve_primary_temp("PC-X", 77.7, [sensor("Weird Name", 50.0)]) == 77.7)
    check("resolve_primary_temp keeps the agent's temp with no sensor block",
          app.resolve_primary_temp("PC-X", 77.7, None) == 77.7)


def test_candidate_filtering():
    print("\n-- only real CPU temperatures are candidates --")
    check("GPU temperature is excluded",
          app.pick_primary_temp(BLOCK, preferred=["gpu core"]) is None)
    check("motherboard probe is excluded",
          app.pick_primary_temp(BLOCK, preferred=["temperature #1"]) is None)
    check("a Load sensor is excluded even on CPU hardware",
          app.pick_primary_temp(BLOCK, preferred=["cpu total"]) is None)
    # LHM reports 0 for sensors it couldn't read; 0 °C must never look like the coldest,
    # healthiest machine in the fleet.
    check("zero is treated as 'no reading'",
          app.pick_primary_temp([sensor("CPU Package", 0.0)], preferred=DEFAULTS) is None)
    check("negative is treated as 'no reading'",
          app.pick_primary_temp([sensor("CPU Package", -5.0)], preferred=DEFAULTS) is None)
    check("non-numeric values are excluded",
          app.pick_primary_temp([sensor("CPU Package", "hot")], preferred=DEFAULTS) is None)
    check("a True boolean is not a temperature",
          app.pick_primary_temp([sensor("CPU Package", True)], preferred=DEFAULTS) is None)
    check("Intel hardware ids are recognised too",
          app.pick_primary_temp([sensor("CPU Package", 70.0, hardware_id="/intelcpu/0")],
                                preferred=DEFAULTS) == 70.0)


def test_sensor_name_listing():
    print("\n-- the dropdown lists distinct CPU temp sensors --")
    names = app.list_cpu_temp_sensor_names(BLOCK)
    check("only CPU temperature sensors listed",
          names == ["core (tctl/tdie)", "cpu package", "core max"])
    check("duplicates collapse",
          app.list_cpu_temp_sensor_names(
              [sensor("CPU Package", 50.0), sensor("CPU Package", 51.0)]) == ["cpu package"])


def test_end_to_end_through_report():
    print("\n-- /api/report records the re-derived temperature --")
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"email": "tester@example.com"}

    def report(machine, temp, sensors=None):
        payload = {"machine": machine, "temp": temp, "serial_number": f"SN-{machine}"}
        if sensors is not None:
            payload["sensors"] = sensors
        resp = client.post("/api/report", json=payload)
        drain()
        return resp

    def drain(timeout=5.0):
        """Readings are written by a background batched writer (db_writer), so a report
        is not in the table the instant the request returns. Wait for the queue to empty,
        then a little longer for the in-flight batch to commit."""
        deadline = time.time() + timeout
        while not app.db_write_queue.empty() and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(app.DB_WRITE_FLUSH_SECONDS + 0.3)

    def stored(machine):
        with app.get_db_conn() as conn:
            row = conn.execute(
                "SELECT temp FROM readings WHERE machine = ? ORDER BY id DESC LIMIT 1",
                (machine,)).fetchone()
        return row["temp"] if row else None

    # A distinct machine per assertion: readings carry a UNIQUE(ts_epoch, machine, temp)
    # index, so two reports for one machine inside the same second can collapse and make
    # "the last row" ambiguous.
    settings.reset(app.DB_PATH, ["computer.primary_sensor_preference"])
    # The agent reported 91.0 (its own pick); the default preference prefers "cpu package".
    report("PICK-DEFAULT", 91.0, BLOCK)
    check("default preference re-derives to CPU Package", stored("PICK-DEFAULT") == 88.5)

    settings.set_many(app.DB_PATH, {"computer.primary_sensor_preference": ["core max"]})
    report("PICK-CONFIGURED", 91.0, BLOCK)
    check("changing the setting changes the recorded temp", stored("PICK-CONFIGURED") == 93.0)

    # A report with no sensor block keeps the agent's number.
    report("PICK-NOSENSORS", 60.0)
    check("sensor-less report stores the agent's temp", stored("PICK-NOSENSORS") == 60.0)

    # A preference that matches nothing must not invent a value.
    settings.set_many(app.DB_PATH, {"computer.primary_sensor_preference": ["not a sensor"]})
    report("PICK-UNMATCHED", 64.0, BLOCK)
    check("unmatched preference falls back to the agent's temp", stored("PICK-UNMATCHED") == 64.0)

    settings.reset(app.DB_PATH, ["computer.primary_sensor_preference"])
    report("PICK-1", 91.0, BLOCK)

    print("\n-- per-machine override, end to end --")
    r = client.get("/api/machines/PICK-1/sensors")
    check("sensors endpoint 200", r.status_code == 200)
    body = r.get_json()
    check("lists this machine's CPU temp sensors",
          [s["name"] for s in body["sensors"]] == ["core (tctl/tdie)", "cpu package", "core max"])
    check("includes current values for recognition",
          any(s["name"] == "core max" and s["value"] == 93.0 for s in body["sensors"]))
    check("no override set initially", body["primary_sensor_name"] is None)

    r = client.put("/api/machines/PICK-1/primary_sensor",
                   json={"primary_sensor_name": "Core (Tctl/Tdie)"})
    check("PUT override 200", r.status_code == 200)
    check("override normalised to lowercase",
          r.get_json()["primary_sensor_name"] == "core (tctl/tdie)")

    report("PICK-OVERRIDE", 50.0, BLOCK)
    # PICK-OVERRIDE has no override of its own, so it follows the fleet preference...
    check("a machine without an override follows the fleet preference",
          stored("PICK-OVERRIDE") == 88.5)
    # ...while PICK-1, which does, gets its pinned sensor.
    report("PICK-1", 50.0, BLOCK)
    check("override beats the fleet preference in a real report", stored("PICK-1") == 91.0)

    r = client.get("/api/machines/PICK-1")
    check("machine detail surfaces the override",
          r.get_json()["primary_sensor_name"] == "core (tctl/tdie)")

    # Clearing it returns the machine to the fleet-wide preference.
    client.put("/api/machines/PICK-1/primary_sensor", json={"primary_sensor_name": None})
    report("PICK-CLEARED", 50.0, BLOCK)
    check("clearing the override restores preference-order picking",
          stored("PICK-CLEARED") == 88.5)

    r = client.put("/api/machines/NOSUCH/primary_sensor", json={"primary_sensor_name": "x"})
    check("unknown machine 404", r.status_code == 404)
    r = client.put("/api/machines/PICK-1/primary_sensor", json={"primary_sensor_name": 5})
    check("non-string override 400", r.status_code == 400)

    print("\n-- override survives a cold cache (hub restart) --")
    client.put("/api/machines/PICK-1/primary_sensor", json={"primary_sensor_name": "core max"})
    app._primary_sensor_overrides = None      # simulate a fresh process
    check("override reloads from the DB", app.get_primary_sensor_override("PICK-1") == "core max")
    client.put("/api/machines/PICK-1/primary_sensor", json={"primary_sensor_name": None})


if __name__ == "__main__":
    test_preference_order()
    test_explicit_override()
    test_absent_sensor_defers_to_agent()
    test_candidate_filtering()
    test_sensor_name_listing()
    test_end_to_end_through_report()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
