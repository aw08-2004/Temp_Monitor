"""Tests the History-dashboard metric pipeline: diagnostics extraction of the new
disk/network metrics, the typed metric columns on `readings` (populated at ingest and
gated by the metrics.* collection toggles), the multi-metric per-machine history endpoint,
and enabled_history_metrics().

The load-bearing cases:
  - net_rx/net_tx must NOT pick up disk read/write throughput (both are SensorType
    Throughput); the matcher pins them to NIC hardware. test_network_matcher_ignores_disk.
  - a toggled-off metric must be stored NULL, not silently recorded anyway -- that is what
    "which sensors are read" means. test_ingest_respects_collection_toggles.

Run from the repo root so `import app` resolves.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-histmetrics-test-")
# app resolves its DB from HUB_LOG_DIR; declare it before importing app so a standalone
# run stays off the real logs/ (see test_sensor_pick.py).
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


def s(name, value, stype, hardware_id, hardware="Device"):
    return {"name": name, "value": value, "type": stype,
            "hardware_id": hardware_id, "hardware": hardware}


NIC = "/nic/{6c1f-network}"
# A realistic block spanning every category the dashboard charts, plus a disk Read Rate
# throughput sensor that the network matcher must NOT mistake for network traffic, and the
# synthetic "/volume/..." capacity sensors the agent appends (VolumeReader.cs).
BLOCK = [
    s("CPU Package", 70.0, "Temperature", "/amdcpu/0", "Ryzen 7"),
    s("CPU Total", 12.0, "Load", "/amdcpu/0", "Ryzen 7"),
    s("Memory", 41.0, "Load", "/ram", "Generic Memory"),
    s("Memory Used", 6.6, "Data", "/ram", "Generic Memory"),
    s("Memory Available", 9.4, "Data", "/ram", "Generic Memory"),
    s("Virtual Memory Used", 99.0, "Data", "/ram", "Generic Memory"),
    s("GPU Core", 55.0, "Temperature", "/gpu-nvidia/0", "RTX 3060"),
    s("GPU Core", 30.0, "Load", "/gpu-nvidia/0", "RTX 3060"),
    s("Used Space", 63.0, "Load", "/nvme/0", "Samsung SSD"),
    s("Read Rate", 1000.0, "Throughput", "/nvme/0", "Samsung SSD"),
    s("Write Rate", 250.0, "Throughput", "/nvme/0", "Samsung SSD"),
    s("Download Speed", 480.0, "Throughput", NIC, "Intel Ethernet"),
    s("Upload Speed", 630.0, "Throughput", NIC, "Intel Ethernet"),
    s("Total Space", 476.0, "Data", "/volume/c", "C: (Windows)"),
    s("Used Space", 412.0, "Data", "/volume/c", "C: (Windows)"),
]

METRIC_COLUMNS = ("cpu_load_pct", "memory_load_pct", "gpu_temp", "gpu_load_pct",
                  "disk_load_pct", "net_rx_bps", "net_tx_bps",
                  "disk_read_bps", "disk_write_bps")


# --------------------------------------------------------------------------- diagnostics
def test_diagnostics_extracts_all_metrics():
    print("\n-- extract_diagnostics pulls disk & network alongside cpu/gpu/memory --")
    d = app.extract_diagnostics(BLOCK)
    check("cpu load", d["cpu_load_pct"] == 12.0)
    check("memory", d["memory_load_pct"] == 41.0)
    check("gpu temp", d["gpu_temp"] == 55.0)
    check("gpu load", d["gpu_load_pct"] == 30.0)
    check("disk used space", d["disk_load_pct"] == 63.0)
    check("network download", d["net_rx_bps"] == 480.0)
    check("network upload", d["net_tx_bps"] == 630.0)
    check("disk read rate", d["disk_read_bps"] == 1000.0)
    check("disk write rate", d["disk_write_bps"] == 250.0)
    check("memory used GB", d["mem_used_gb"] == 6.6)
    # total = used (6.6) + available (9.4) = 16.0; virtual-memory sensors must NOT leak in.
    check("memory total GB (used + available, not virtual)", d["mem_total_gb"] == 16.0)


def test_network_matcher_ignores_disk():
    print("\n-- disk throughput is not mistaken for network throughput --")
    disk_only = [s("Read Rate", 1234.0, "Throughput", "/nvme/0", "Samsung SSD"),
                 s("Write Rate", 5678.0, "Throughput", "/nvme/0", "Samsung SSD")]
    d = app.extract_diagnostics(disk_only)
    check("no NIC => net_rx is None", d["net_rx_bps"] is None)
    check("no NIC => net_tx is None", d["net_tx_bps"] is None)


def test_network_picks_the_busiest_adapter():
    """The regression that made the Network In/Out panels useless: a real Windows box
    reports dozens of NICs, the idle ones (Bluetooth, disconnected Wi-Fi, Hyper-V/WSL
    switches) sort ahead of the live one, and taking the first NIC charted a flat 0 on
    every machine whose real adapter didn't happen to come first. Block below mirrors
    what the fleet actually reports, including the NDIS filter pseudo-adapters that
    duplicate the parent NIC's counters -- summing those would multiply the traffic."""
    print("\n-- the busiest NIC wins, and mirrored filter adapters aren't double-counted --")
    real, mirror = "/nic/{eth-real}", "/nic/{eth-wfp-filter}"
    block = [
        s("Download Speed", 0.0, "Throughput", "/nic/{bt}", "Bluetooth Network Connection"),
        s("Upload Speed", 0.0, "Throughput", "/nic/{bt}", "Bluetooth Network Connection"),
        s("Download Speed", 0.0, "Throughput", "/nic/{lac1}", "Local Area Connection* 1"),
        s("Upload Speed", 0.0, "Throughput", "/nic/{lac1}", "Local Area Connection* 1"),
        s("Download Speed", 2679.8, "Throughput", real, "Ethernet"),
        s("Upload Speed", 1057.3, "Throughput", real, "Ethernet"),
        # Same physical NIC seen through an NDIS filter: near-identical, must not add.
        s("Download Speed", 2679.7, "Throughput", mirror, "Ethernet-WFP Native MAC Layer"),
        s("Upload Speed", 1057.3, "Throughput", mirror, "Ethernet-WFP Native MAC Layer"),
        s("Download Speed", 0.0, "Throughput", "/nic/{wifi}", "Wi-Fi"),
        s("Upload Speed", 0.0, "Throughput", "/nic/{wifi}", "Wi-Fi"),
        s("Read Rate", 999999.0, "Throughput", "/nvme/0", "Samsung SSD"),
    ]
    d = app.extract_diagnostics(block)
    check("an idle adapter listed first does not win", d["net_rx_bps"] == 2679.8)
    check("upload comes from that same adapter", d["net_tx_bps"] == 1057.3)

    idle = [s("Download Speed", 0.0, "Throughput", "/nic/{bt}", "Bluetooth"),
            s("Upload Speed", 0.0, "Throughput", "/nic/{bt}", "Bluetooth")]
    d_idle = app.extract_diagnostics(idle)
    check("a genuinely idle machine reports 0, not a gap",
          d_idle["net_rx_bps"] == 0.0 and d_idle["net_tx_bps"] == 0.0)


def test_disk_throughput_sums_every_disk():
    """Disk I/O is summed across drives, unlike network -- LHM reports each storage device
    once, with none of the NDIS filter mirrors that force the NIC matcher to pick a single
    adapter. A two-SSD workstation writing on both must chart the total, not one of them."""
    print("\n-- disk read/write sums across disks and ignores NIC throughput --")
    block = [
        s("Read Rate", 1000.0, "Throughput", "/nvme/0", "Samsung SSD"),
        s("Write Rate", 250.0, "Throughput", "/nvme/0", "Samsung SSD"),
        s("Read Rate", 4000.0, "Throughput", "/hdd/1", "Seagate"),
        s("Write Rate", 750.0, "Throughput", "/hdd/1", "Seagate"),
        s("Download Speed", 9e9, "Throughput", NIC, "Intel Ethernet"),
        s("Upload Speed", 9e9, "Throughput", NIC, "Intel Ethernet"),
    ]
    d = app.extract_diagnostics(block)
    check("reads add up across both disks", d["disk_read_bps"] == 5000.0)
    check("writes add up across both disks", d["disk_write_bps"] == 1000.0)

    net_only = [s("Download Speed", 480.0, "Throughput", NIC, "Intel Ethernet"),
                s("Upload Speed", 630.0, "Throughput", NIC, "Intel Ethernet")]
    d_net = app.extract_diagnostics(net_only)
    check("no disk => disk_read is None", d_net["disk_read_bps"] is None)
    check("no disk => disk_write is None", d_net["disk_write_bps"] is None)

    idle = [s("Read Rate", 0.0, "Throughput", "/nvme/0", "Samsung SSD"),
            s("Write Rate", 0.0, "Throughput", "/nvme/0", "Samsung SSD")]
    d_idle = app.extract_diagnostics(idle)
    check("an idle disk reports 0, not a gap",
          d_idle["disk_read_bps"] == 0.0 and d_idle["disk_write_bps"] == 0.0)


def test_disk_volumes():
    """The Storage cards. GB comes from the agent's synthetic /volume/ sensors, because LHM
    reports used space only as a percentage -- so a machine without them (companion.py, or
    an agent below 3.10.0) must still get a percentage rather than an empty card."""
    print("\n-- per-volume space usage, with a percentage-only fallback --")
    disks = app.extract_diagnostics(BLOCK)["disks"]
    check("one entry per volume", len(disks) == 1)
    check("named from the volume label", disks[0]["name"] == "C: (Windows)")
    check("absolute used GB", disks[0]["used_gb"] == 412.0)
    check("absolute total GB", disks[0]["total_gb"] == 476.0)
    check("percentage derived from the pair", disks[0]["used_pct"] == 86.6)

    multi = BLOCK + [s("Total Space", 1000.0, "Data", "/volume/d", "D: (Data)"),
                     s("Used Space", 250.0, "Data", "/volume/d", "D: (Data)")]
    names = [v["name"] for v in app.extract_diagnostics(multi)["disks"]]
    check("every volume is reported, in drive-letter order", names == ["C: (Windows)", "D: (Data)"])

    # An agent that predates VolumeReader sends only LHM's per-device Load sensor.
    legacy = [s("Used Space", 63.0, "Load", "/nvme/0", "Samsung SSD"),
              s("Used Space", 12.0, "Load", "/hdd/1", "Seagate")]
    fb = app.extract_diagnostics(legacy)["disks"]
    check("fallback yields one entry per storage device", len(fb) == 2)
    check("fallback carries the percentage", fb[0]["used_pct"] == 63.0)
    check("fallback has no size to report", fb[0]["total_gb"] is None)

    check("no storage sensors at all => empty list",
          app.extract_diagnostics([s("CPU Total", 5.0, "Load", "/amdcpu/0")])["disks"] == [])


def test_volume_sensors_do_not_displace_disk_load_pct():
    """The volume block reuses the name "Used Space" (as Data, in GB). disk_load_pct takes
    the first sensor named "Used Space" of type Load, so the two must not collide -- a
    regression here would chart 412 on a 0-100 axis."""
    print("\n-- volume capacity sensors don't hijack the disk usage % metric --")
    d = app.extract_diagnostics(BLOCK)
    check("disk_load_pct is still the device percentage", d["disk_load_pct"] == 63.0)


def test_diagnostics_empty_has_all_keys():
    print("\n-- an empty/None block returns every key as None --")
    for block in (None, []):
        d = app.extract_diagnostics(block)
        check(f"all metric keys present and None ({block!r})",
              all(d.get(k) is None for k in METRIC_COLUMNS))


# --------------------------------------------------------------------------- ingest -> columns
def _client():
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"email": "tester@example.com"}
    return client


def _drain(timeout=5.0):
    deadline = time.time() + timeout
    while not app.db_write_queue.empty() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(app.DB_WRITE_FLUSH_SECONDS + 0.3)


def _report(client, machine, temp, sensors=None):
    payload = {"machine": machine, "temp": temp, "serial_number": f"SN-{machine}"}
    if sensors is not None:
        payload["sensors"] = sensors
    resp = client.post("/api/report", json=payload)
    _drain()
    return resp


def _stored_metrics(machine):
    cols = ", ".join(METRIC_COLUMNS)
    with app.get_db_conn() as conn:
        row = conn.execute(
            f"SELECT {cols} FROM readings WHERE machine = ? ORDER BY id DESC LIMIT 1",
            (machine,)).fetchone()
    return dict(row) if row else None


def test_ingest_stores_metric_columns():
    print("\n-- /api/report promotes the sensor block into typed columns --")
    for key in ("metrics.collect_cpu_load", "metrics.collect_memory", "metrics.collect_gpu",
                "metrics.collect_disk", "metrics.collect_disk_io", "metrics.collect_network"):
        settings.reset(app.DB_PATH, [key])
    client = _client()
    _report(client, "METRICS-ALL", 70.0, BLOCK)
    m = _stored_metrics("METRICS-ALL")
    check("row was stored", m is not None)
    check("cpu_load_pct column", m["cpu_load_pct"] == 12.0)
    check("memory_load_pct column", m["memory_load_pct"] == 41.0)
    check("gpu_temp column", m["gpu_temp"] == 55.0)
    check("gpu_load_pct column", m["gpu_load_pct"] == 30.0)
    check("disk_load_pct column", m["disk_load_pct"] == 63.0)
    check("net_rx_bps column", m["net_rx_bps"] == 480.0)
    check("net_tx_bps column", m["net_tx_bps"] == 630.0)
    check("disk_read_bps column", m["disk_read_bps"] == 1000.0)
    check("disk_write_bps column", m["disk_write_bps"] == 250.0)


def test_ingest_respects_collection_toggles():
    print("\n-- a toggled-off metric is recorded NULL, not silently kept --")
    client = _client()
    settings.set_many(app.DB_PATH, {"metrics.collect_network": False,
                                    "metrics.collect_disk_io": False})
    _report(client, "METRICS-GATED", 66.0, BLOCK)
    m = _stored_metrics("METRICS-GATED")
    check("network off => net_rx NULL", m["net_rx_bps"] is None)
    check("network off => net_tx NULL", m["net_tx_bps"] is None)
    check("disk I/O off => disk_read NULL", m["disk_read_bps"] is None)
    check("disk I/O off => disk_write NULL", m["disk_write_bps"] is None)
    # collect_disk and collect_disk_io are deliberately separate knobs: turning off the
    # noisy per-second rates must not also stop recording "is C: filling up".
    check("disk usage is unaffected by the disk I/O toggle", m["disk_load_pct"] == 63.0)
    settings.set_many(app.DB_PATH, {"metrics.collect_disk": False})
    _report(client, "METRICS-GATED2", 66.0, BLOCK)
    check("disk off => disk_load NULL", _stored_metrics("METRICS-GATED2")["disk_load_pct"] is None)
    check("an unaffected metric is still recorded", m["cpu_load_pct"] == 12.0)
    settings.reset(app.DB_PATH, ["metrics.collect_network", "metrics.collect_disk",
                                 "metrics.collect_disk_io"])


def test_sensorless_report_stores_null_metrics():
    print("\n-- a report with no sensor block stores NULL metrics, not zeros --")
    client = _client()
    _report(client, "METRICS-NOSENSORS", 60.0)
    m = _stored_metrics("METRICS-NOSENSORS")
    check("every metric column is NULL without a sensor block",
          all(m[k] is None for k in METRIC_COLUMNS))


# --------------------------------------------------------------------------- history endpoint
def test_machine_history_endpoint():
    print("\n-- the multi-metric per-machine history endpoint --")
    client = _client()
    for key in ("metrics.collect_cpu_load", "metrics.collect_memory", "metrics.collect_gpu",
                "metrics.collect_disk", "metrics.collect_disk_io", "metrics.collect_network"):
        settings.reset(app.DB_PATH, [key])
    _report(client, "HIST-1", 71.5, BLOCK)
    today = app.today_str()

    r = client.get(f"/api/machines/HIST-1/history?date={today}&resolution=raw")
    check("history 200", r.status_code == 200)
    body = r.get_json()
    check("carries the machine name", body.get("machine") == "HIST-1")
    check("metrics is a dict", isinstance(body.get("metrics"), dict))
    metrics = body.get("metrics", {})
    check("temperature series has a point", len(metrics.get("temp", [])) >= 1)
    check("cpu_load series has a point", len(metrics.get("cpu_load", [])) >= 1)
    check("net_rx series has a point", len(metrics.get("net_rx", [])) >= 1)
    check("disk_read series has a point", len(metrics.get("disk_read", [])) >= 1)
    check("disk_write series has a point", len(metrics.get("disk_write", [])) >= 1)
    check("a point looks like {x, y}",
          bool(metrics["cpu_load"]) and set(("x", "y")).issubset(metrics["cpu_load"][0]))

    r2 = client.get(f"/api/machines/HIST-1/history?date={today}&resolution=raw&metrics=cpu_load,memory")
    b2 = r2.get_json()
    check("the metrics filter limits the returned keys",
          set(b2["metrics"].keys()) == {"cpu_load", "memory"})

    r3 = client.get(f"/api/machines/HIST-1/history?date={today}&resolution=raw&metrics=bogus")
    check("an all-unknown metrics filter yields no series",
          r3.status_code == 200 and r3.get_json()["metrics"] == {})


def test_history_metric_param_on_fleet_endpoint():
    print("\n-- /api/history honours the metric param and rejects unknowns --")
    client = _client()
    _report(client, "HIST-2", 72.0, BLOCK)
    today = app.today_str()
    r = client.get(f"/api/history?machine=HIST-2&metric=cpu_load&date={today}&resolution=raw")
    check("known metric 200", r.status_code == 200)
    check("returns that machine's series", len(r.get_json().get("HIST-2", [])) >= 1)
    r_bad = client.get(f"/api/history?metric=not_a_metric&date={today}")
    check("unknown metric 400", r_bad.status_code == 400)


# --------------------------------------------------------------------------- enabled map
def test_enabled_history_metrics():
    print("\n-- enabled_history_metrics reflects the toggles; temp is always on --")
    for key in ("metrics.collect_cpu_load", "metrics.collect_disk"):
        settings.reset(app.DB_PATH, [key])
    en = app.enabled_history_metrics()
    check("temperature always enabled", en["temp"] is True)
    check("cpu_load enabled by default", en["cpu_load"] is True)

    settings.set_many(app.DB_PATH, {"metrics.collect_disk": False})
    en2 = app.enabled_history_metrics()
    check("disabling disk shows up in the map", en2["disk"] is False)
    check("temperature stays enabled regardless", en2["temp"] is True)
    check("the disk I/O panels have their own toggle", en2["disk_read"] is True)
    settings.reset(app.DB_PATH, ["metrics.collect_disk"])


if __name__ == "__main__":
    test_diagnostics_extracts_all_metrics()
    test_network_matcher_ignores_disk()
    test_network_picks_the_busiest_adapter()
    test_disk_throughput_sums_every_disk()
    test_disk_volumes()
    test_volume_sensors_do_not_displace_disk_load_pct()
    test_diagnostics_empty_has_all_keys()
    test_ingest_stores_metric_columns()
    test_ingest_respects_collection_toggles()
    test_sensorless_report_stores_null_metrics()
    test_machine_history_endpoint()
    test_history_metric_param_on_fleet_endpoint()
    test_enabled_history_metrics()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
