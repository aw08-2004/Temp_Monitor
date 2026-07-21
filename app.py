import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import csv
import socket
import sqlite3
import queue
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps
import wmi
import pythoncom
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from flask_socketio import SocketIO, join_room
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
import requests

import fleet
import alerts
import settings
import permissions
import packages
from fleet_web import create_fleet_blueprint
from settings_web import create_settings_blueprint
from permissions_web import create_access, create_permissions_blueprint
from packages_web import create_packages_blueprint

# Load .env from next to this file rather than the cwd -- under the Windows service the working
# directory isn't the hub folder -- and with utf-8-sig so a UTF-8 BOM (which PowerShell and
# Windows editors happily prepend) doesn't corrupt the first key and blank out the config.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), encoding="utf-8-sig")

# ================================
# CONFIG
# ================================
# Bump on every push to main and restart the hub service -- shown in the
# dashboard header so a stale/un-restarted deployment is obvious at a glance.
HUB_VERSION = "1.27.0"
CHECK_INTERVAL = 5
SPIKE_THRESHOLD = 10
LHM_URL = "http://localhost:8085/data.json"
HUB_URL = os.environ.get("HUB_URL", "http://localhost:5000")
# Opt-in hub self-update. Off by default so a dev clone never resets itself; the
# operator sets HUB_AUTO_UPDATE=1 in the real hub's .env. The Settings tab can
# override this per-hub -- see hub_auto_update_enabled() and hub_update_watcher().
HUB_AUTO_UPDATE_ENV = os.environ.get("HUB_AUTO_UPDATE", "").strip().lower() in ("1", "true", "yes", "on")

# Absolute, and overridable via HUB_LOG_DIR, so the database location never depends on
# the process's current working directory. A relative "logs" is re-resolved by sqlite on
# every connect, and the db_writer runs on a daemon thread -- so a background write that
# resolves the path while another thread has changed cwd is a data race. It never bites
# in production (the service's cwd is fixed), but it makes the test suite flaky. The
# default sits next to this file, which under the WinSW service is the hub install dir --
# exactly where the old cwd-relative "logs" resolved to, so this is behaviour-preserving.
LOG_DIR = os.path.abspath(
    os.environ.get("HUB_LOG_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)

DB_PATH = os.path.join(LOG_DIR, "temp_v2.db")
# Daily CSV archives are retired -- the DB is the single source of truth now.
# Existing CSV files on disk are left untouched; we just stop writing new ones.
WRITE_CSV_ARCHIVE = False
SQLITE_TIMEOUT_SECONDS = 30
DB_WRITE_BATCH_SIZE = 200
DB_WRITE_FLUSH_SECONDS = 0.5

# Readings retention. A background pruner deletes readings older than the configured
# window, so the DB stays bounded instead of growing forever (see start_retention_pruner()).
# The window itself, and how often the pruner runs, are operator-settable:
# data.retention_days and data.prune_interval_seconds. Batch size stays a constant --
# it's a lock-contention tuning detail, not something an operator has an opinion about.
RETENTION_PRUNE_BATCH = 50000
DEFAULT_HISTORY_LIMIT = 1200
MAX_HISTORY_POINTS_PER_MACHINE = 2000
MAX_HISTORY_MACHINE_MULTIPLIER = 16
VALID_RESOLUTIONS = {"raw": None, "10s": 10, "1m": 60, "5m": 300}

LOCAL_MACHINE = socket.gethostname()

# Latest known uptime/temp per machine -- kept in memory for speed, but also
# mirrored to machine_info (see persist_live_status) so a hub restart doesn't
# instantly blank them out. The DB fallback only counts for a bounded age
# (hub.live_status_cache_seconds); past that a machine that's actually gone quiet
# should read as unknown again, not show an arbitrarily stale reading forever.
#
# The machine online/offline window is fleet.dashboard_online_window_seconds. Live
# temp reports refresh machine_info.updated_at at least every ~30s
# (persist_live_status throttling), so the 2-minute default comfortably tolerates a
# couple of missed reports without flapping -- keep that in mind before setting it low.

latest_uptime = {}
latest_uptime_lock = threading.Lock()

def get_uptime_seconds():
    try:
        return round(ctypes.windll.kernel32.GetTickCount64() / 1000)
    except Exception:
        return None

def set_latest_uptime(machine, uptime_seconds):
    if uptime_seconds is None:
        return
    with latest_uptime_lock:
        latest_uptime[str(machine).strip()] = int(uptime_seconds)

def get_latest_uptime(machine):
    machine_name = str(machine).strip()
    with latest_uptime_lock:
        cached = latest_uptime.get(machine_name)
    if cached is not None:
        return cached
    return load_cached_live_status(machine_name).get('uptime_seconds')

latest_temp = {}
latest_temp_lock = threading.Lock()

def set_latest_temp(machine, temp):
    if temp is None:
        return
    with latest_temp_lock:
        latest_temp[str(machine).strip()] = float(temp)

def get_latest_temp(machine):
    machine_name = str(machine).strip()
    with latest_temp_lock:
        cached = latest_temp.get(machine_name)
    if cached is not None:
        return cached
    return load_cached_live_status(machine_name).get('temp')

latest_sensors = {}
latest_sensors_lock = threading.Lock()

def set_latest_sensors(machine, sensors):
    if not sensors:
        return
    with latest_sensors_lock:
        latest_sensors[str(machine).strip()] = sensors

def get_latest_sensors(machine):
    with latest_sensors_lock:
        return latest_sensors.get(str(machine).strip())

def _find_sensor_value(sensors, hardware_substr, sensor_type, preferred_name_substrs=None):
    """Fuzzy-matches one numeric value out of a flattened LHM sensor list -- same
    preferred-name-first-match style as companion.py's PREFERRED_SENSORS, since
    sensor naming varies across CPU/GPU vendors."""
    def matches_hardware(s):
        # hardware_id (e.g. "/amdcpu/0", "/gpu-nvidia/0", "/ram") is what reliably
        # identifies the category -- the display name ("AMD Ryzen 7 5800X") never
        # contains the literal word "cpu"/"gpu"/etc, so check both defensively.
        haystack = f"{s.get('hardware_id') or ''} {s.get('hardware') or ''}".lower()
        return hardware_substr in haystack

    candidates = [
        s for s in sensors
        if s.get("type") == sensor_type
        and matches_hardware(s)
        and isinstance(s.get("value"), (int, float))
    ]
    if not candidates:
        return None
    if preferred_name_substrs:
        for wanted in preferred_name_substrs:
            for s in candidates:
                if wanted in str(s.get("name") or "").lower():
                    return s["value"]
    return candidates[0]["value"]

def _cpu_temp_candidates(sensors):
    """Every usable CPU temperature in a reported sensor block.

    Same rules the agent applies in SensorReader.CollectHardware: identify CPU hardware
    by its identifier ("/amdcpu/0", "/intelcpu/0"), and treat 0/negative as "no reading"
    rather than a real temperature -- LHM reports 0 for sensors it couldn't read, and a
    0 °C CPU would otherwise look like the coldest, healthiest machine in the fleet.
    """
    candidates = []
    for s in sensors or []:
        if s.get("type") != "Temperature":
            continue
        haystack = f"{s.get('hardware_id') or ''} {s.get('hardware') or ''}".lower()
        if "cpu" not in haystack:
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            continue
        candidates.append((str(s.get("name") or "").lower(), value))
    return candidates


def pick_primary_temp(sensors, preferred=None, explicit=None):
    """Re-derive a machine's primary CPU temperature from its reported sensor block.

    Returns None when nothing matches, and that is the important part of the contract:
    the caller keeps whatever temperature the AGENT picked. The obvious alternative --
    falling back to "any CPU temperature", the way SensorReader does on the endpoint --
    is wrong here, because the hub has something the agent doesn't: the agent's own
    considered answer, already in the payload. Falling back to an arbitrary sensor would
    let a renamed or missing sensor silently swap a real 91 °C package reading for a
    28 °C board probe, and every overheat alert on that machine would quietly stop
    firing. Degrade to today's behaviour instead.

    `explicit` (a per-machine override chosen from a dropdown of real sensor names) is
    matched exactly; `preferred` (the fleet-wide list) is matched as a substring. The
    asymmetry is deliberate: the operator picked the override from names this machine
    actually reports, whereas the preference list is a fuzzy heuristic that has to span
    Intel and AMD naming ("cpu package" vs "core (tctl/tdie)").
    """
    candidates = _cpu_temp_candidates(sensors)
    if not candidates:
        return None

    if explicit:
        want = str(explicit).strip().lower()
        for name, value in candidates:
            if name == want:
                return value
        return None      # the named sensor is gone -- defer to the agent, don't guess

    for wanted in (preferred or []):
        for name, value in candidates:
            if wanted in name:
                return value
    return None


def list_cpu_temp_sensor_names(sensors):
    """Distinct CPU temperature sensor names in a reported block, for the UI dropdown."""
    seen = []
    for name, _ in _cpu_temp_candidates(sensors):
        if name not in seen:
            seen.append(name)
    return seen


# machine -> explicit primary sensor name, mirroring machine_info.primary_sensor_name.
# Cached because it's consulted on every sensor-bearing report; overrides are set by
# hand, so writes are vanishingly rare and a full reload on change is cheaper than a
# per-report SELECT. Same copy-on-write discipline as settings.py: rebind, never mutate.
_primary_sensor_overrides = None
_primary_sensor_overrides_lock = threading.Lock()


def get_primary_sensor_override(machine):
    global _primary_sensor_overrides
    overrides = _primary_sensor_overrides
    if overrides is None:
        with _primary_sensor_overrides_lock:
            if _primary_sensor_overrides is None:
                with get_db_conn() as conn:
                    rows = conn.execute(
                        "SELECT machine, primary_sensor_name FROM machine_info "
                        "WHERE primary_sensor_name IS NOT NULL AND primary_sensor_name != ''"
                    ).fetchall()
                _primary_sensor_overrides = {r["machine"]: r["primary_sensor_name"] for r in rows}
            overrides = _primary_sensor_overrides
    return overrides.get(str(machine).strip())


def set_primary_sensor_override(machine, sensor_name):
    """Set (or clear, with a falsy name) a machine's explicit primary sensor."""
    global _primary_sensor_overrides
    machine_name = str(machine).strip()
    value = str(sensor_name).strip().lower() if sensor_name else None
    with _primary_sensor_overrides_lock:
        with get_db_conn() as conn:
            conn.execute(
                "UPDATE machine_info SET primary_sensor_name = ? WHERE machine = ?",
                (value, machine_name),
            )
        _primary_sensor_overrides = None      # rebuilt on the next read
    return value


def resolve_primary_temp(machine, reported_temp, sensors):
    """The temperature to actually record for this report.

    Falls back to `reported_temp` -- the agent's own pick -- whenever the configured
    sensor isn't present in this block. See pick_primary_temp for why that fallback,
    and not "any CPU temperature", is the safe one.
    """
    if not sensors:
        return reported_temp
    try:
        rederived = pick_primary_temp(
            sensors,
            preferred=settings.get_list(DB_PATH, "computer.primary_sensor_preference"),
            explicit=get_primary_sensor_override(machine),
        )
    except Exception as e:
        # Never let sensor selection fail an ingest; the agent's value is always valid.
        print(f"[sensors] Re-derivation failed for {machine!r}: {e}")
        return reported_temp
    return rederived if rederived is not None else reported_temp


def _find_sensor_strict(sensors, sensor_type, name_substrs, hardware_substrs=None):
    """Like _find_sensor_value, but identifies a metric by its sensor NAME rather than
    by hardware category, and returns None when no name matches -- never a blind
    first-candidate fallback.

    Used for disk usage and network throughput, where the hardware identifier isn't a
    single stable substring (storage is "/nvme/","/hdd/","/ssd/"...) but the sensor name
    is distinctive ("Used Space", "Download Speed"). `hardware_substrs`, when given,
    additionally requires the hardware to match one of the fragments -- e.g. "nic" to keep
    disk read/write throughput from being mistaken for network throughput (both are
    SensorType.Throughput). First match wins; on a multi-NIC host that is whichever adapter
    LHM lists first."""
    for s in sensors:
        if s.get("type") != sensor_type:
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if hardware_substrs:
            haystack = f"{s.get('hardware_id') or ''} {s.get('hardware') or ''}".lower()
            if not any(h in haystack for h in hardware_substrs):
                continue
        name = str(s.get("name") or "").lower()
        if any(w in name for w in name_substrs):
            return value
    return None


def _find_sensor_exact(sensors, sensor_type, exact_name, hardware_substrs=None):
    """Match a sensor by its EXACT (lowercased) name. Needed where a substring match would
    over-reach: "memory used" is a substring of "virtual memory used", so the RAM-in-use
    reading must be pinned to the exact name."""
    want = exact_name.strip().lower()
    for s in sensors:
        if s.get("type") != sensor_type:
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if hardware_substrs:
            haystack = f"{s.get('hardware_id') or ''} {s.get('hardware') or ''}".lower()
            if not any(h in haystack for h in hardware_substrs):
                continue
        if str(s.get("name") or "").strip().lower() == want:
            return value
    return None


def _memory_gb(sensors):
    """(used_gb, total_gb) for physical RAM, from LHM's "Memory Used"/"Memory Available"
    Data sensors (GB). total = used + available; both None when the block doesn't carry
    them. Exact names so virtual-memory sensors don't leak in. total is effectively a
    machine constant, which is what lets the UI say what 100% of the Memory chart means."""
    used = _find_sensor_exact(sensors, "Data", "memory used", ["ram"])
    avail = _find_sensor_exact(sensors, "Data", "memory available", ["ram"])
    if used is None or avail is None:
        return None, None
    return round(float(used), 1), round(float(used) + float(avail), 1)


def extract_diagnostics(sensors):
    """Pulls the specific fields the UI shows out of a raw flattened LHM sensor
    list (see companion.py's flatten_sensors). Every field is None when not
    found -- e.g. no discrete GPU, or an older companion that sent no sensors."""
    if not sensors:
        return {
            "cpu_load_pct": None, "cpu_clock_mhz": None,
            "gpu_temp": None, "gpu_load_pct": None, "gpu_clock_mhz": None,
            "memory_load_pct": None, "mem_used_gb": None, "mem_total_gb": None,
            "disk_load_pct": None, "net_rx_bps": None, "net_tx_bps": None,
        }
    mem_used_gb, mem_total_gb = _memory_gb(sensors)
    return {
        "cpu_load_pct": _find_sensor_value(sensors, "cpu", "Load", ["cpu total", "total cpu"]),
        "cpu_clock_mhz": _find_sensor_value(sensors, "cpu", "Clock", ["core average", "cpu core #1", "bus speed"]),
        "gpu_temp": _find_sensor_value(sensors, "gpu", "Temperature", ["gpu core", "gpu hot spot", "gpu package"]),
        "gpu_load_pct": _find_sensor_value(sensors, "gpu", "Load", ["gpu core", "d3d 3d"]),
        "gpu_clock_mhz": _find_sensor_value(sensors, "gpu", "Clock", ["gpu core", "gpu shader"]),
        "memory_load_pct": _find_sensor_value(sensors, "ram", "Load", ["memory"]),
        # Absolute RAM (GB) so the Memory chart can say what 100% is and show GB-in-use on
        # hover. total is a machine constant; used is exact at report time.
        "mem_used_gb": mem_used_gb,
        "mem_total_gb": mem_total_gb,
        # "Used Space" is unique to storage devices, so name alone identifies it.
        "disk_load_pct": _find_sensor_strict(sensors, "Load", ["used space"]),
        # Network throughput shares SensorType.Throughput with disk read/write rate, so
        # pin to NIC hardware ("/nic/...") to avoid mixing them up.
        "net_rx_bps": _find_sensor_strict(sensors, "Throughput", ["download"], ["nic"]),
        "net_tx_bps": _find_sensor_strict(sensors, "Throughput", ["upload"], ["nic"]),
    }


# ---- Per-reading metric columns -------------------------------------------------------
# The chartable numeric metrics promoted out of the sensor blob into their own columns on
# `readings`, so history bucketing can AVG/MIN/MAX them as cheaply as `temp` (rather than
# JSON-parsing every row). Single source of truth: the schema migration, the INSERT, and
# the ingest all key off this tuple. Each entry is BOTH the column name AND the
# extract_diagnostics() key. Clock metrics are intentionally not stored -- they aren't
# charted.
READING_METRIC_COLUMNS = (
    "cpu_load_pct", "memory_load_pct", "gpu_temp", "gpu_load_pct",
    "disk_load_pct", "net_rx_bps", "net_tx_bps",
)

# Which collection toggle (settings.py `metrics.*`) gates each column at ingest. When a
# toggle is off, the column is stored NULL -- "off" means "not recorded", matching the
# "what sensor should be read" intent. `metrics.collect_network` also drives the agent.
METRIC_COLUMN_TOGGLE = {
    "cpu_load_pct": "metrics.collect_cpu_load",
    "memory_load_pct": "metrics.collect_memory",
    "gpu_temp": "metrics.collect_gpu",
    "gpu_load_pct": "metrics.collect_gpu",
    "disk_load_pct": "metrics.collect_disk",
    "net_rx_bps": "metrics.collect_network",
    "net_tx_bps": "metrics.collect_network",
}

# Friendly metric keys used by the /api/history `metric` param and the multi-metric
# per-machine endpoint, mapped to their `readings` column. A whitelist -- callers never
# choose a raw column name, so nothing user-supplied is interpolated into SQL.
HISTORY_METRIC_COLUMNS = {
    "temp": "temp",
    "cpu_load": "cpu_load_pct",
    "memory": "memory_load_pct",
    "gpu_temp": "gpu_temp",
    "gpu_load": "gpu_load_pct",
    "disk": "disk_load_pct",
    "net_rx": "net_rx_bps",
    "net_tx": "net_tx_bps",
}
_ALLOWED_HISTORY_COLUMNS = frozenset(HISTORY_METRIC_COLUMNS.values())

# A readings row is (ts_text, ts_epoch, machine, temp, sensors_json, *metric columns).
# Built from constants only -- no user input reaches the column list.
_READINGS_INSERT_SQL = (
    "INSERT OR IGNORE INTO readings(ts_text, ts_epoch, machine, temp, sensors_json"
    + "".join(f", {c}" for c in READING_METRIC_COLUMNS)
    + ") VALUES (" + ", ".join(["?"] * (5 + len(READING_METRIC_COLUMNS))) + ")"
)


def _metric_values_tuple(metrics):
    """Metric column values in READING_METRIC_COLUMNS order, for the INSERT. Missing or
    toggled-off metrics become NULL."""
    metrics = metrics or {}
    return tuple(metrics.get(col) for col in READING_METRIC_COLUMNS)


def metrics_for_storage(sensors):
    """The metric-column values to record for a reading: each chartable metric extracted
    from the sensor block, but only when its collection toggle is on. A toggled-off metric
    is None so it is stored NULL -- "off" means "not recorded"."""
    diagnostics = extract_diagnostics(sensors)
    return {
        col: (diagnostics.get(col)
              if settings.get_bool(DB_PATH, METRIC_COLUMN_TOGGLE[col]) else None)
        for col in READING_METRIC_COLUMNS
    }


def enabled_history_metrics():
    """Which /api/history metric keys are currently being collected, so the machine
    dashboard renders a panel only for metrics whose toggle is on. Temperature has no
    toggle and is always on."""
    enabled = {}
    for key, column in HISTORY_METRIC_COLUMNS.items():
        toggle = METRIC_COLUMN_TOGGLE.get(column)
        enabled[key] = True if toggle is None else bool(settings.get_bool(DB_PATH, toggle))
    return enabled

def load_cached_live_status(machine_name):
    """DB-backed fallback for get_latest_temp/get_latest_uptime right after a hub
    restart, when the in-memory dicts above are empty. Only trusts a row up to
    hub.live_status_cache_seconds old -- see the comment above."""
    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT last_temp, last_uptime_seconds, updated_at FROM machine_info WHERE machine = ?",
            (machine_name,),
        ).fetchone()
    if not row or not row["updated_at"]:
        return {}
    updated_at = parse_request_datetime(row["updated_at"])
    max_age = settings.get_int(DB_PATH, "hub.live_status_cache_seconds")
    if updated_at is None or (datetime.now() - updated_at).total_seconds() > max_age:
        return {}
    return {"temp": row["last_temp"], "uptime_seconds": row["last_uptime_seconds"]}

def derive_machine_status(updated_at):
    """'online' | 'offline' for the Dashboard and Asset Inventory, derived purely from
    how recently the machine reported (machine_info.updated_at). Note we deliberately do
    NOT treat presence in the in-memory latest_temp cache as "online": that cache is
    never evicted, so a machine that reported once this process lifetime would read
    online forever."""
    if not updated_at:
        return "offline"
    parsed = parse_request_datetime(updated_at) if isinstance(updated_at, str) else None
    if parsed is None:
        return "offline"
    # Called once per machine per /api/machines request, so this read has to be cheap:
    # settings.get() is a dict lookup off a copy-on-write cache, no DB round-trip.
    window = settings.get_int(DB_PATH, "fleet.dashboard_online_window_seconds")
    return "online" if (datetime.now() - parsed).total_seconds() <= window else "offline"

# ================================
# VERSION WATCHER  --  lets clients self-update promptly instead of waiting for
# their own weekly GitHub poll. We periodically check the same sources they
# update from, and echo the newest version *that client should be running* back
# in /api/report's response; both companion.py and the agent check for an update
# as soon as they see a number ahead of their own.
#
# The fleet runs two trains that share the companion_version field:
#   * companion.py (2.x), which self-updates from the raw script on main, and
#   * TempMonitorAgent (3.x), the C# service, which self-updates from a signed
#     manifest.
# A companion can only reach the agent by first updating to 2.10.1 -- that's the
# release whose migration path installs the service and decommissions itself. So
# 2.x clients are climbed to 2.10.1 and then left alone, 3.x clients get the
# latest agent. Advertising one global number strands one train or the other.
# ================================
COMPANION_SOURCE_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/companion.py"
AGENT_MANIFEST_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/agent.manifest.json"
# The hub reads its own latest version straight out of app.py on main -- same source-of-truth
# and raw-GitHub trust as the client version hints above. Used only by the opt-in self-updater.
HUB_SOURCE_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/app.py"
HUB_UPDATE_CHECK_INTERVAL = 15 * 60  # 15 minutes
COMPANION_VERSION_CHECK_INTERVAL = 15 * 60  # 15 minutes
# First version of the C# agent. A client reporting >= this is on the agent train
# and must never be pointed back at a 2.x companion number.
AGENT_TRAIN_MIN_VERSION = "3.0.0"
# Last stop on the companion train: the release that installs the agent and
# decommissions itself. A companion that reaches this is done taking version
# hints -- it now waits to be replaced by the agent, on its own migration
# schedule. Only bump this if a 2.10.x hotfix ever has to reach the machines
# that haven't migrated yet; companion.py is otherwise end-of-life.
COMPANION_FINAL_VERSION = "2.10.1"

latest_companion_version = None
latest_agent_version = None
latest_version_lock = threading.Lock()

def version_tuple(v):
    """Tolerant version parse: reads the leading dotted-numeric prefix and ignores
    any suffix (e.g. '2.8.0-rc1' -> (2, 8, 0)). Never raises. Mirrors the
    identically-named helper in companion.py."""
    match = re.match(r"\s*(\d+(?:\.\d+)*)", str(v))
    if not match:
        return (0,)
    return tuple(int(p) for p in match.group(1).split("."))

def cmp_versions(a, b):
    """Return 1 if a > b, -1 if a < b, 0 if equal. Pads to equal length so that
    '2.8' and '2.8.0' compare as equal rather than '2.8' < '2.8.0'."""
    ta, tb = version_tuple(a), version_tuple(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)

def get_latest_companion_version():
    with latest_version_lock:
        return latest_companion_version

def get_latest_agent_version():
    with latest_version_lock:
        return latest_agent_version

def get_advertised_version(reported_version):
    """The version to echo back to a client currently running `reported_version`.

    Agent-train clients (3.x) get the latest agent. Companions get climbed to
    COMPANION_FINAL_VERSION and then deliberately go quiet: once a companion is
    there it has everything it needs to install the agent, so we stop hinting and
    let its migration replace it with 3.x. Clients too old to report a version at
    all are treated as companions.

    Returns None when there is nothing useful to say -- a companion waiting on
    migration, or a train we haven't read yet -- in which case /api/report omits
    latest_version entirely and the client falls back to its own poll."""
    if reported_version and cmp_versions(reported_version, AGENT_TRAIN_MIN_VERSION) >= 0:
        return get_latest_agent_version()
    if reported_version and cmp_versions(reported_version, COMPANION_FINAL_VERSION) >= 0:
        return None
    return get_latest_companion_version()

def refresh_latest_companion_version():
    global latest_companion_version
    try:
        resp = requests.get(COMPANION_SOURCE_URL, timeout=10)
        resp.raise_for_status()
        match = re.search(r'^VERSION\s*=\s*["\']([\d.]+)["\']', resp.text, re.MULTILINE)
        if match:
            with latest_version_lock:
                latest_companion_version = match.group(1)
    except Exception as e:
        print(f"[companion-version] Could not refresh latest version: {e}")

def refresh_latest_agent_version():
    """Read the agent's version straight out of the signed release manifest, so the
    hub advertises exactly what the agent's own updater would install. We don't
    verify the signature here -- the agent does that before it installs anything,
    and this number is only ever a hint to go check."""
    global latest_agent_version
    try:
        resp = requests.get(AGENT_MANIFEST_URL, timeout=10)
        resp.raise_for_status()
        version = (resp.json() or {}).get("version")
        if version:
            with latest_version_lock:
                latest_agent_version = str(version)
    except Exception as e:
        print(f"[agent-version] Could not refresh latest version: {e}")

def companion_version_watcher():
    while True:
        refresh_latest_companion_version()
        refresh_latest_agent_version()
        time.sleep(COMPANION_VERSION_CHECK_INTERVAL)

companion_version_watcher_thread = None
companion_version_watcher_lock = threading.Lock()

def start_companion_version_watcher():
    global companion_version_watcher_thread
    with companion_version_watcher_lock:
        if companion_version_watcher_thread and companion_version_watcher_thread.is_alive():
            return
        companion_version_watcher_thread = threading.Thread(
            target=companion_version_watcher, daemon=True, name="companion_version_watcher"
        )
        companion_version_watcher_thread.start()

# ================================
# HUB SELF-UPDATE  --  opt-in (HUB_AUTO_UPDATE=1). Update in place, then exit; the
# supervising service relaunches waitress, which re-imports the new code.
#
# Two source strategies, chosen by what's on disk:
#   .git present  -> fetch + reset --hard origin/main (developer clones, and hubs
#                    installed before the installer stopped cloning)
#   no .git       -> download the branch archive and replace the runtime file set
#                    (what install.ps1 now produces: ~0.3 MB of hub files, no repo)
#
# Both trust GitHub over HTTPS plus push access to main; NEITHER touches the separate
# Ed25519 fleet-update trust root that gates agent binaries.
# ================================
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Source archive for the no-git path. codeload serves a branch zip directly.
HUB_ARCHIVE_URL = "https://codeload.github.com/aw08-2004/Temp_Monitor/zip/refs/heads/main"

# The files that constitute a hub install. Anything outside this set (the agent tree,
# tests, docs) is deliberately not shipped to a server.
# MUST stay in sync with $HubRuntimeFiles/$HubRuntimeDirs in install.ps1.
HUB_RUNTIME_FILES = (
    "app.py", "wsgi.py", "fleet.py", "fleet_web.py",
    "settings.py", "settings_web.py", "permissions.py", "permissions_web.py",
    "alerts.py", "requirements.txt",
)
HUB_RUNTIME_DIRS = ("templates", "static")

def parse_hub_version(text):
    """Pull the HUB_VERSION string out of an app.py source blob, or None. Pure; mirrors
    the VERSION parse in refresh_latest_companion_version()."""
    match = re.search(r'^HUB_VERSION\s*=\s*["\']([\d.]+)["\']', str(text or ""), re.MULTILINE)
    return match.group(1) if match else None

def fetch_remote_hub_version():
    """Latest HUB_VERSION on main, or None on any error (logged, never raises)."""
    try:
        resp = requests.get(HUB_SOURCE_URL, timeout=10)
        resp.raise_for_status()
        return parse_hub_version(resp.text)
    except Exception as e:
        print(f"[hub-update] Could not read remote hub version: {e}")
        return None

def _run_git(args, cwd):
    """Run a git command, returning (ok, combined_output). Never raises -- a missing git
    binary or a timeout comes back as ok=False so the caller just skips this cycle."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()
    except Exception as e:
        return False, str(e)

def _install_requirements(repo_root):
    """Best-effort dependency install after an update: a release that adds a dependency
    shouldn't crash-loop the restart, so failure here is logged and tolerated."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r",
             os.path.join(repo_root, "requirements.txt"), "--quiet"],
            cwd=repo_root, capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        print(f"[hub-update] pip install after update failed (continuing): {e}")


def _perform_hub_update_git(repo_root):
    """Bring the clone at repo_root up to origin/main via fetch + hard reset. Returns
    True only if fetch AND reset succeeded -- the caller restarts only then. Discards
    local drift by design (operator-confirmed)."""
    ok, out = _run_git(["fetch", "origin", "main"], repo_root)
    if not ok:
        print(f"[hub-update] git fetch failed, skipping: {out}")
        return False
    ok, out = _run_git(["reset", "--hard", "origin/main"], repo_root)
    if not ok:
        print(f"[hub-update] git reset failed, skipping: {out}")
        return False
    print(f"[hub-update] Updated working tree to origin/main: {out}")
    _install_requirements(repo_root)
    return True


def _stage_hub_archive(staging):
    """Download and unpack the branch archive into `staging`, returning the path to the
    unpacked tree, or None on any failure (logged, never raises).

    Everything lands in staging and is checked for completeness BEFORE the caller
    touches the live install -- a truncated download or a moved file upstream must fail
    the update, not leave a hub with half its templates."""
    try:
        resp = requests.get(HUB_ARCHIVE_URL, timeout=120)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(staging)
    except Exception as e:
        print(f"[hub-update] could not fetch the source archive: {e}")
        return None

    # codeload wraps everything in a single <repo>-<branch>/ directory.
    roots = [d for d in os.listdir(staging) if os.path.isdir(os.path.join(staging, d))]
    if len(roots) != 1:
        print(f"[hub-update] unexpected archive layout ({len(roots)} top-level dirs), skipping")
        return None
    src = os.path.join(staging, roots[0])

    missing = [n for n in HUB_RUNTIME_FILES + HUB_RUNTIME_DIRS
               if not os.path.exists(os.path.join(src, n))]
    if missing:
        print(f"[hub-update] archive is missing {', '.join(missing)} -- refusing to update")
        return None
    return src


def _perform_hub_update_archive(repo_root):
    """Replace the hub runtime file set from the branch archive. Used when there's no
    git clone to reset (the layout install.ps1 now produces)."""
    staging = tempfile.mkdtemp(prefix="hub-update-")
    try:
        src = _stage_hub_archive(staging)
        if src is None:
            return False

        # Swap only once everything is staged and verified. Directories are mirrored
        # rather than merged so a template deleted upstream disappears here too;
        # .env, logs/ and the service wrapper live outside this set and are untouched.
        for name in HUB_RUNTIME_FILES:
            shutil.copy2(os.path.join(src, name), os.path.join(repo_root, name))
        for name in HUB_RUNTIME_DIRS:
            target = os.path.join(repo_root, name)
            if os.path.isdir(target):
                shutil.rmtree(target)
            shutil.copytree(os.path.join(src, name), target)

        print(f"[hub-update] Replaced hub files in {repo_root} from {HUB_ARCHIVE_URL}")
        _install_requirements(repo_root)
        return True
    except Exception as e:
        # A failure part-way through the swap leaves the tree inconsistent, so say so
        # loudly -- but still don't restart, since restarting is what would turn a
        # broken tree into a crash-loop.
        print(f"[hub-update] update failed while replacing files ({e}); hub left as-is")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def perform_hub_update(repo_root):
    """Update the hub in place. Returns True only if the caller should now restart.

    Prefers git when the install is a clone: a developer running from a checkout should
    not have their working tree overwritten by an archive, and pre-sparse-install hubs
    keep the behaviour they were deployed with."""
    if os.path.isdir(os.path.join(repo_root, ".git")):
        return _perform_hub_update_git(repo_root)
    return _perform_hub_update_archive(repo_root)

def restart_hub():
    """Exit non-zero so the supervisor treats it as a failure and relaunches waitress with
    the new code. Under the WinSW service that's `onfailure action="restart"` (~5s); under
    the legacy SYSTEM Scheduled Task the 2-min repetition trigger relaunches regardless of
    exit code. Abrupt by design -- WAL + per-batch commits make this as safe as the crash
    the supervisor already recovers from."""
    print("[hub-update] New version applied -- exiting for the service to relaunch.")
    sys.stdout.flush()
    os._exit(1)

def hub_auto_update_enabled():
    """Whether the hub may update itself. Tri-state, resolved in this order:

      hub.auto_update = True/False  -> explicit operator override from the Settings tab
      hub.auto_update = None        -> fall back to HUB_AUTO_UPDATE in .env (the default)

    Keeping unset distinct from false is what lets Settings default to "whatever this
    deployment was already configured to do" rather than silently overriding .env the
    first time anyone opens the page.
    """
    override = settings.get_bool(DB_PATH, "hub.auto_update")
    return HUB_AUTO_UPDATE_ENV if override is None else bool(override)


def hub_update_watcher():
    while True:
        try:
            # Re-read every tick: an operator toggling this in Settings must take effect
            # without a hub restart (and a restart is exactly what this thread causes).
            if hub_auto_update_enabled():
                remote = fetch_remote_hub_version()
                if remote and cmp_versions(remote, HUB_VERSION) > 0:
                    print(f"[hub-update] main is {remote} (running {HUB_VERSION}); updating.")
                    if perform_hub_update(REPO_ROOT):
                        restart_hub()
        except Exception as e:
            print(f"[hub-update] watcher error (continuing): {e}")
        time.sleep(HUB_UPDATE_CHECK_INTERVAL)

hub_update_watcher_thread = None
hub_update_watcher_lock = threading.Lock()

def start_hub_update_watcher():
    """Always starts the watcher; the loop itself decides whether to act.

    This used to return early when the feature was off, but the toggle is now settable
    at runtime -- and a thread that was never started can't notice being switched on.
    An idle tick is one cached dict lookup every 15 minutes, so running it unconditionally
    costs nothing and a dev clone with the setting off still never self-resets.
    """
    global hub_update_watcher_thread
    with hub_update_watcher_lock:
        if hub_update_watcher_thread and hub_update_watcher_thread.is_alive():
            return
        hub_update_watcher_thread = threading.Thread(
            target=hub_update_watcher, daemon=True, name="hub_update_watcher"
        )
        hub_update_watcher_thread.start()
        state = "enabled" if hub_auto_update_enabled() else "disabled"
        print(f"[hub-update] Watcher started -- hub self-update currently {state}.")

# ================================
# AUTH CONFIG (Google sign-in)
# ================================
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
# The BREAK-GLASS SUPERUSER LIST, not the perimeter it once was. Membership grants
# every capability over every machine, bypassing permission groups entirely -- which
# is what bootstraps a hub (someone has to create the first group) and what keeps a
# broken group config from locking everyone out. Everyone else signs in on the
# strength of their permission-group membership; see permissions.py.
ALLOWED_EMAILS = {
    email.strip().lower()
    for email in os.environ.get("ALLOWED_EMAILS", "").split(",")
    if email.strip()
}

if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and FLASK_SECRET_KEY):
    raise RuntimeError(
        "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and FLASK_SECRET_KEY must all be set "
        "(as env vars, or in a .env file) to run the hub -- see README."
    )
if not ALLOWED_EMAILS:
    raise RuntimeError(
        "ALLOWED_EMAILS must list at least one break-glass superuser email (comma-separated)."
    )

# ================================
# FLEET (command channel) CONFIG
# ================================
# OPTIONAL so existing telemetry-only deployments keep booting. Enrollment fails
# closed until set: with no enrollment secret, no agent can enroll.
#   AGENT_ENROLLMENT_SECRET -- shared secret an agent presents to enroll
#
# Commands themselves carry no signature. Every command type dispatches on an
# authenticated, allow-listed console session alone, so any operator in
# ALLOWED_EMAILS can act on the fleet without holding an offline key. That makes
# ALLOWED_EMAILS the entire perimeter for arbitrary code execution as SYSTEM, and
# the append-only audit_log (which records the issuer and the full params) the
# accountability control. Release/self-update signing is a SEPARATE, RETAINED
# trust root -- see sign_release.py --sign-agent and AgentConfig.UpdatePublicKeyHex.
AGENT_ENROLLMENT_SECRET = os.environ.get("AGENT_ENROLLMENT_SECRET", "")
if not AGENT_ENROLLMENT_SECRET:
    print("[fleet] AGENT_ENROLLMENT_SECRET unset -- agent enrollment disabled (fail closed).")

# ================================
# WEB & WEBSOCKET SETUP
# ================================
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
# Session cookie hardening. A console session can run arbitrary code as SYSTEM on
# any enrolled machine, so a CSRF against a signed-in operator would be fleet-wide
# RCE. Today that is blocked only incidentally: the command endpoints read their
# body with request.get_json(silent=True), which requires Content-Type:
# application/json -- not a CORS-safelisted type, so a cross-origin fetch always
# preflights and fails (no ACAO on these routes), and an HTML form (the one
# cross-site POST needing no preflight) cannot produce that content type. That
# defence evaporates if anyone adds force=True, a form-encoded fallback, or
# permissive CORS, so pin the real control here:
#   SameSite=Lax -- Flask sets NO SameSite attribute by default, leaving this to
#     the browser's Lax-by-default (Chrome/Edge yes, Firefox still not by default,
#     and Chrome exempts cookies <2min old from it on top-level POSTs). Lax, not
#     Strict: the Google OAuth callback is a top-level cross-site GET redirect and
#     needs the cookie to find its state.
#   Secure -- derived from HUB_URL so http://localhost dev still signs in.
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=HUB_URL.startswith("https://"),
)
# Trust one hop of X-Forwarded-* from nginx, so url_for(_external=True) builds
# HUB_URL (e.g. https://your.domain.com/...) instead of the local bind address/scheme.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
print(f"[hub] Configured public URL: {HUB_URL}")
# cors_allowed_origins is pinned to our own origin, NOT "*". engine.io does not send a
# literal "*": on "*" it reflects the caller's Origin back in Access-Control-Allow-Origin
# and pairs it with Access-Control-Allow-Credentials: true (see engineio's
# base_server._cors_headers), which is exactly the permissive-CORS case the session-cookie
# comment above warns is fleet-wide RCE if it ever lets a cross-origin page ride an
# operator's session. SameSite=Lax happens to withhold the cookie from those requests
# today, but that is the browser's default doing the work, not ours. The socket carries
# live telemetry for the whole fleet and is same-origin in every real deployment.
socketio = SocketIO(
    app,
    cors_allowed_origins=[HUB_URL.rstrip("/")],
    async_mode="threading",
    transports=["polling"],
    allow_upgrades=False
)

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def login_required(view):
    """Gate a route behind an authenticated + allow-listed session. Never applied to /api/report."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# The second enforcement layer. login_required answers "is there a session"; `access`
# answers "does that session hold this capability, over this machine". One instance,
# shared by every blueprint, so there is exactly one implementation of the rule --
# the same reason login_required is passed around rather than re-declared.
access = create_access(DB_PATH, ALLOWED_EMAILS)

# Fleet command-channel endpoints (agent-facing token auth + console-facing
# login_required). Registered here, once login_required exists to hand in.
app.register_blueprint(create_fleet_blueprint(
    DB_PATH, AGENT_ENROLLMENT_SECRET, login_required, access
))
# Settings endpoints (console-facing only). Same reason for being registered here.
app.register_blueprint(create_settings_blueprint(DB_PATH, login_required, access))
# Permission-group administration.
app.register_blueprint(create_permissions_blueprint(DB_PATH, login_required, access))
# Package definitions, deployments, and the agent-facing payload download. LOG_DIR is
# handed in because the blob store lives beside the database (see packages.blob_root),
# and HUB_URL because the agent's download URL has to be absolute.
app.register_blueprint(create_packages_blueprint(
    DB_PATH, LOG_DIR, login_required, access, hub_url=HUB_URL
))


@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/login/google")
def login_google():
    # Anchor the callback to HUB_URL rather than url_for(_external=True): behind a TLS
    # terminator (nginx/Cloudflare) the request can reach waitress as plain http, so the
    # _external form emits http://.../auth/callback -- which Google rejects as a
    # redirect_uri mismatch. HUB_URL is the authoritative public origin (https://...).
    redirect_uri = HUB_URL.rstrip("/") + url_for("auth_callback")
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo") or oauth.google.userinfo(token=token)
    email = (user_info.get("email") or "").strip().lower()

    if not user_info.get("email_verified", True):
        return "Google account email is not verified.", 403
    # Break-glass superuser, or a member of at least one permission group. A valid
    # Google account that is in neither is refused outright rather than admitted to an
    # empty dashboard -- see Access.login_allowed for why, and for where roadmap #4
    # (Entra) will change it.
    if not access.login_allowed(email):
        return f"Access denied: {email} is not authorized for this dashboard.", 403

    session["user"] = {
        "email": email,
        "name": user_info.get("name") or email,
        "picture": user_info.get("picture"),
    }
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# Live telemetry is scoped by socket ROOM, because a broadcast can't be filtered after
# the fact. Two disjoint audiences, so every reading is emitted exactly once per
# listener: unrestricted operators sit in FLEET_ROOM, scoped ones sit in one room per
# machine they can see.
#
# Room membership is decided at CONNECT time, so a scope change (added to a group, a
# machine added to their group) reaches an already-open tab on its next reconnect, not
# instantly. That is the right trade for a telemetry feed -- the alternative is
# re-resolving permissions on every emit, i.e. once per machine per few seconds -- but
# it does mean a REVOKED operator keeps seeing live temperatures on an open tab until
# they reload. Every actual action they could take is re-checked server-side per
# request, so this is a stale view, not stale authority.
FLEET_ROOM = "fleet:all"


def machine_room(machine):
    return f"machine:{str(machine).strip()}"


@socketio.on("connect")
def handle_socket_connect():
    if not session.get("user"):
        return False  # reject the connection; browser falls back to no live updates
    current = access.current()
    if not permissions.has_capability(current, permissions.VIEW):
        return False
    if current["machines"] is None:
        join_room(FLEET_ROOM)
    else:
        for machine in current["machines"]:
            join_room(machine_room(machine))

# ================================
# HELPERS
# ================================
def today_str():
    return datetime.now().strftime("%Y-%m-%d")

def get_log_path(date=None):
    if not date:
        date = today_str()
    return os.path.join(LOG_DIR, f"temp_v2_{date}.csv")

def normalize_datetime(value):
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value

def to_timestamp_str(value):
    return normalize_datetime(value).strftime("%Y-%m-%d %H:%M:%S")

def to_epoch_seconds(value):
    return int(normalize_datetime(value).timestamp())

def parse_request_datetime(value):
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace("T", " ")
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
        return normalize_datetime(parsed)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None

def parse_int_arg(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))

def parse_history_limit(value):
    cleaned = "" if value is None else str(value).strip().lower()
    if not cleaned:
        return DEFAULT_HISTORY_LIMIT
    if cleaned in {"all", "full", "none", "0"}:
        return None
    try:
        parsed = int(cleaned)
    except ValueError:
        return DEFAULT_HISTORY_LIMIT
    if parsed <= 0:
        return None
    return max(100, min(MAX_HISTORY_POINTS_PER_MACHINE, parsed))

def pick_resolution(requested_resolution, span_seconds):
    if requested_resolution in VALID_RESOLUTIONS:
        return requested_resolution
    if span_seconds <= 3 * 3600:
        return "raw"
    if span_seconds <= 24 * 3600:
        return "10s"
    if span_seconds <= 72 * 3600:
        return "1m"
    return "5m"

def get_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn

def get_oldest_reading_datetime():
    with get_db_conn() as conn:
        row = conn.execute("SELECT MIN(ts_epoch) AS min_epoch FROM readings").fetchone()
    min_epoch = row["min_epoch"] if row else None
    if min_epoch is None:
        return None
    return datetime.fromtimestamp(int(min_epoch))

def init_db():
    with get_db_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_text TEXT NOT NULL,
                ts_epoch INTEGER NOT NULL,
                machine TEXT NOT NULL,
                temp REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts_epoch ON readings(ts_epoch)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_machine_ts ON readings(machine, ts_epoch)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_unique ON readings(ts_epoch, machine, temp)"
        )
        existing_reading_columns = {row["name"] for row in conn.execute("PRAGMA table_info(readings)")}
        if "sensors_json" not in existing_reading_columns:
            conn.execute("ALTER TABLE readings ADD COLUMN sensors_json TEXT")
        # Typed metric columns (see READING_METRIC_COLUMNS). Nullable REAL, added the same
        # ALTER-per-column way as sensors_json/companion_version. Old rows read back NULL;
        # column names come from a hardcoded constant, so the f-string is injection-safe.
        for _metric_col in READING_METRIC_COLUMNS:
            if _metric_col not in existing_reading_columns:
                conn.execute(f"ALTER TABLE readings ADD COLUMN {_metric_col} REAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_days (
                day TEXT PRIMARY KEY
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_info (
                machine TEXT PRIMARY KEY,
                asset_tag TEXT,
                serial_number TEXT,
                model TEXT,
                updated_at TEXT
            )
            """
        )
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(machine_info)")}
        if "companion_version" not in existing_columns:
            conn.execute("ALTER TABLE machine_info ADD COLUMN companion_version TEXT")
        if "last_temp" not in existing_columns:
            conn.execute("ALTER TABLE machine_info ADD COLUMN last_temp REAL")
        if "last_uptime_seconds" not in existing_columns:
            conn.execute("ALTER TABLE machine_info ADD COLUMN last_uptime_seconds INTEGER")
        # Per-machine override for which CPU sensor is THE temperature, beating the
        # fleet-wide computer.primary_sensor_preference list. Lives here rather than in
        # the settings table because it is per-machine state like asset_tag -- a global
        # key/value store stops being one the moment it holds per-machine rows.
        if "primary_sensor_name" not in existing_columns:
            conn.execute("ALTER TABLE machine_info ADD COLUMN primary_sensor_name TEXT")

def write_readings_batch(records):
    if not records:
        return
    with get_db_conn() as conn:
        conn.executemany(_READINGS_INSERT_SQL, records)

db_write_queue = queue.Queue(maxsize=20000)
db_writer_thread = None
db_writer_lock = threading.Lock()

def db_writer():
    while True:
        first_item = db_write_queue.get()
        batch = [first_item]
        flush_deadline = time.time() + DB_WRITE_FLUSH_SECONDS

        while len(batch) < DB_WRITE_BATCH_SIZE:
            remaining = flush_deadline - time.time()
            if remaining <= 0:
                break
            try:
                batch.append(db_write_queue.get(timeout=remaining))
            except queue.Empty:
                break

        try:
            write_readings_batch(batch)
        except Exception as e:
            print(f"Error writing readings batch to SQLite: {e}")

def ensure_db_writer_running():
    global db_writer_thread
    with db_writer_lock:
        if db_writer_thread and db_writer_thread.is_alive():
            return
        db_writer_thread = threading.Thread(target=db_writer, daemon=True, name="db_writer")
        db_writer_thread.start()

def append_csv_archive(timestamp_str, machine, temp):
    log_file = get_log_path()
    if not os.path.exists(log_file):
        with open(log_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "machine", "temperature"])
    with open(log_file, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([timestamp_str, machine, temp])

def ensure_day_loaded_from_csv(date):
    if not date:
        return

    with get_db_conn() as conn:
        already_loaded = conn.execute(
            "SELECT 1 FROM imported_days WHERE day = ?",
            (date,),
        ).fetchone()
    if already_loaded:
        return

    log_file = get_log_path(date)
    if not os.path.exists(log_file):
        with get_db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO imported_days(day) VALUES (?)", (date,))
        return

    records = []
    with open(log_file, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamp_str = row.get("timestamp")
            machine = (row.get("machine") or "").strip()
            temp_raw = row.get("temperature")
            parsed_ts = parse_request_datetime(timestamp_str)
            if parsed_ts is None or not machine:
                continue
            try:
                temp = float(temp_raw)
            except (TypeError, ValueError):
                continue
            # CSV archive holds temperature only; the metric columns backfill as NULL.
            records.append(
                (to_timestamp_str(parsed_ts), to_epoch_seconds(parsed_ts), machine, temp, None)
                + _metric_values_tuple(None)
            )

    write_readings_batch(records)
    with get_db_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO imported_days(day) VALUES (?)", (date,))

def enqueue_reading(timestamp_str, timestamp_epoch, machine, temp, sensors_json=None,
                    metrics=None):
    ensure_db_writer_running()
    record = ((timestamp_str, timestamp_epoch, machine, float(temp), sensors_json)
              + _metric_values_tuple(metrics))
    try:
        db_write_queue.put_nowait(record)
    except queue.Full:
        print("WARNING: SQLite queue is full; writing synchronously.")
        write_readings_batch([record])

# How often persist_live_status actually hits SQLite per machine. Reports come in
# every few seconds, but the cache only needs to be fresh to within
# hub.live_status_cache_seconds, so there's no need to write anywhere near that often.
LIVE_STATUS_PERSIST_INTERVAL_SECONDS = 30
_last_live_status_persist = {}
_last_live_status_persist_lock = threading.Lock()

def persist_live_status(machine, temp, uptime_seconds):
    """Mirror the latest temp/uptime into machine_info so get_latest_temp/
    get_latest_uptime (via load_cached_live_status) can serve them for a while
    after a hub restart, instead of going blank until the machine reports again."""
    machine_name = str(machine).strip()
    if not machine_name:
        return

    now = time.time()
    with _last_live_status_persist_lock:
        last = _last_live_status_persist.get(machine_name, 0)
        if now - last < LIVE_STATUS_PERSIST_INTERVAL_SECONDS:
            return
        _last_live_status_persist[machine_name] = now

    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO machine_info(machine, last_temp, last_uptime_seconds, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(machine) DO UPDATE SET
                last_temp = excluded.last_temp,
                last_uptime_seconds = excluded.last_uptime_seconds,
                updated_at = excluded.updated_at
            """,
            (machine_name, temp, uptime_seconds, to_timestamp_str(datetime.now())),
        )

def save_and_emit_temp(machine, temp, uptime_seconds=None, sensors=None, timestamp_epoch=None,
                       companion_version=None):
    machine_name = str(machine).strip()
    if not machine_name:
        raise ValueError("Machine name cannot be empty.")

    # Re-derive the primary temperature from the reported sensor block, so the operator's
    # sensor choice applies to every agent immediately -- including ones too old to
    # receive config. Reports without a sensor block keep the agent's own pick.
    temp_value = float(resolve_primary_temp(machine_name, float(temp), sensors))
    now = datetime.now()
    # A reading may carry the companion's own timestamp (client_ts) -- e.g. a
    # backfilled reading that was buffered while the hub was unreachable. Store it
    # under its real time; only treat "current" readings as live status.
    if timestamp_epoch is not None:
        reading_dt = datetime.fromtimestamp(int(timestamp_epoch))
    else:
        reading_dt = now
    is_historical = (now - reading_dt).total_seconds() > 60

    timestamp_str = to_timestamp_str(reading_dt)
    timestamp_epoch = to_epoch_seconds(reading_dt)

    if WRITE_CSV_ARCHIVE:
        append_csv_archive(timestamp_str, machine_name, temp_value)

    sensors_json = json.dumps(sensors) if sensors else None
    # Promote the chartable metrics from THIS report's sensor block into their own columns,
    # each gated by its collection toggle -- a toggled-off metric is recorded as NULL.
    reading_metrics = metrics_for_storage(sensors)
    enqueue_reading(timestamp_str, timestamp_epoch, machine_name, temp_value, sensors_json,
                    metrics=reading_metrics)

    # Backfilled (historical) readings go into history only; they must not clobber
    # the "current" live-status caches with a stale value.
    if not is_historical:
        set_latest_uptime(machine_name, uptime_seconds)
        set_latest_temp(machine_name, temp_value)
        set_latest_sensors(machine_name, sensors)
        persist_live_status(machine_name, temp_value, uptime_seconds)

    # Emit via WebSocket. Diagnostics come from the freshest cached sensors, not
    # this report's raw `sensors`, so a report that arrived without a sensor block
    # (an older companion, or a second stale instance double-reporting for the same
    # machine) doesn't blank out CPU/GPU Load & Clock in the UI every other update.
    # set_latest_sensors() above only overwrites the cache when sensors are present.
    payload = {
        'machine': machine_name,
        'timestamp': timestamp_str,
        'timestamp_epoch': timestamp_epoch,
        'temp': temp_value,
        'threshold': settings.get_int(DB_PATH, "hub.overheat_threshold"),
        'low_load_threshold': settings.get_int(DB_PATH, "hub.low_load_threshold"),
        'uptime_seconds': get_latest_uptime(machine_name),
        'diagnostics': extract_diagnostics(get_latest_sensors(machine_name)),
    }
    # The version the client just reported, so the machine page's version card
    # follows a self-update without a refresh. Omitted (not sent as null) when the
    # report didn't carry one -- an older client's silence must not blank a version
    # the UI already knows, same reasoning as the diagnostics cache above.
    if companion_version:
        payload['companion_version'] = str(companion_version)
    # Two rooms, disjoint by construction (see handle_socket_connect): everyone who
    # may see the whole fleet, plus everyone scoped to this specific machine.
    socketio.emit('new_temp', payload, room=FLEET_ROOM)
    socketio.emit('new_temp', payload, room=machine_room(machine_name))

def save_machine_info(machine, asset_tag, serial_number, model, companion_version=None):
    machine_name = str(machine).strip()
    asset_tag = (str(asset_tag).strip() or None) if asset_tag else None
    serial_number = (str(serial_number).strip() or None) if serial_number else None
    model = (str(model).strip() or None) if model else None
    companion_version = (str(companion_version).strip() or None) if companion_version else None
    if not machine_name or not any([asset_tag, serial_number, model, companion_version]):
        return

    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO machine_info(machine, asset_tag, serial_number, model, companion_version, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine) DO UPDATE SET
                asset_tag = COALESCE(excluded.asset_tag, machine_info.asset_tag),
                serial_number = COALESCE(excluded.serial_number, machine_info.serial_number),
                model = COALESCE(excluded.model, machine_info.model),
                companion_version = COALESCE(excluded.companion_version, machine_info.companion_version),
                updated_at = excluded.updated_at
            """,
            (machine_name, asset_tag, serial_number, model, companion_version, to_timestamp_str(datetime.now())),
        )

# ================================
# DUPLICATE-SERIAL DEDUP / MERGE
# ================================
# machine_info is keyed by hostname, but the same physical box reappears under a new
# hostname when an agent upgrade renames/re-cases it (e.g. OpenClaw -> OPENCLAW), leaving
# two rows that share one BIOS serial. We collapse those, always preferring the record
# that is still reporting; two genuinely-live machines on one serial are left alone (a
# real conflict for the operator to resolve manually).

# BIOS/OEM placeholder serials many machines share -- never key identity on these, or
# unrelated whiteboxes/VMs would be merged into one record.
_JUNK_SERIALS = {
    "", "0", "none", "null", "n/a", "na", "not specified", "not applicable",
    "to be filled by o.e.m.", "to be filled by o.e.m", "default string",
    "system serial number", "chassis serial number", "unknown", "invalid",
    "empty", "123456789", "0123456789",
}

def is_valid_serial(serial):
    """True only for a serial distinct enough to key identity on. Rejects blanks and
    common BIOS placeholder strings so unrelated machines are never merged."""
    if not serial:
        return False
    return str(serial).strip().lower() not in _JUNK_SERIALS


# Anything that can reach the hub may POST /api/report under a name of its choosing --
# that endpoint is unauthenticated by design (open telemetry ingress). So the name is
# untrusted input that then flows into every console view, and it is stored, meaning a
# bad one keeps re-rendering long after the report. The console builds its DOM with
# textContent and Jinja autoescapes, so this is the second layer, not the only one;
# it exists so a future innerHTML slip isn't immediately exploitable.
#
# Deliberately a rejection of characters that cannot appear in a real hostname, not an
# allow-list of the ones that can: an allow-list here would silently drop legitimate
# machines from a fleet that already has odd names in it, and the point is defence in
# depth, not naming policy.
MACHINE_NAME_MAX_CHARS = 128
_MACHINE_NAME_FORBIDDEN = re.compile(r'[<>"\'&\x00-\x1f\x7f-\x9f]')

def is_valid_machine_name(machine):
    """True if `machine` is safe to store and render as a machine identifier."""
    name = str(machine or "").strip()
    if not name or len(name) > MACHINE_NAME_MAX_CHARS:
        return False
    return _MACHINE_NAME_FORBIDDEN.search(name) is None


def _evict_live_status(machine_name):
    """Drop a machine's in-memory live caches so a removed hostname doesn't linger on
    the Dashboard/Inventory. Shared by hard-delete and duplicate-merge."""
    with latest_temp_lock:
        latest_temp.pop(machine_name, None)
    with latest_uptime_lock:
        latest_uptime.pop(machine_name, None)
    with latest_sensors_lock:
        latest_sensors.pop(machine_name, None)
    with _last_live_status_persist_lock:
        _last_live_status_persist.pop(machine_name, None)


def merge_machines(survivor, dropped, actor="system:dedup"):
    """Absorb `dropped` into `survivor` -- the same physical machine seen under an old
    hostname. Re-points the dropped host's readings onto the survivor so temperature
    history stays continuous, backfills any identity field the survivor is missing from
    the dropped row, then removes the dropped identity row and its stale fleet
    enrollment. Irreversible."""
    survivor = str(survivor or "").strip()
    dropped = str(dropped or "").strip()
    if not survivor or not dropped or survivor == dropped:
        return
    with get_db_conn() as conn:
        # Preserve history: the dropped hostname's readings belong to the same box.
        conn.execute("UPDATE readings SET machine = ? WHERE machine = ?", (survivor, dropped))
        d = conn.execute(
            "SELECT asset_tag, serial_number, model, companion_version "
            "FROM machine_info WHERE machine = ?",
            (dropped,),
        ).fetchone()
        if d is not None:
            conn.execute(
                """
                UPDATE machine_info SET
                    asset_tag = COALESCE(asset_tag, ?),
                    serial_number = COALESCE(serial_number, ?),
                    model = COALESCE(model, ?),
                    companion_version = COALESCE(companion_version, ?)
                WHERE machine = ?
                """,
                (d["asset_tag"], d["serial_number"], d["model"], d["companion_version"], survivor),
            )
        conn.execute("DELETE FROM machine_info WHERE machine = ?", (dropped,))
    fleet.delete_machine(DB_PATH, dropped)
    # The survivor IS the dropped box, so a permission group scoped to the old
    # hostname must keep granting access. Without this, a rename-driven merge would
    # silently drop machines out of operators' scopes -- a permission change nobody
    # made, discovered only when someone couldn't reach a PC they'd always managed.
    permissions.rename_machine(DB_PATH, dropped, survivor)
    # Same reasoning for deployment targets: the merged-away hostname is the survivor, so
    # a deploy aimed at the old name must follow it rather than stall forever on a
    # machine that no longer exists.
    packages.rename_machine(DB_PATH, dropped, survivor)
    _evict_live_status(dropped)
    fleet.audit(DB_PATH, actor, "machine.merge", dropped, {"survivor": survivor})


def resolve_serial_group(serial, actor="system:dedup"):
    """Collapse duplicate machine_info rows that share `serial`, preferring live records:
      - exactly one online  -> merge the offline duplicate(s) into it
      - all offline         -> merge into the most recently updated row
      - two or more online   -> leave them separate (a genuine conflict)
    Returns the machines still present for that serial afterwards."""
    if not is_valid_serial(serial):
        return []
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT machine, updated_at FROM machine_info "
            "WHERE serial_number = ? COLLATE NOCASE",
            (str(serial).strip(),),
        ).fetchall()
    if len(rows) <= 1:
        # No collision (any more) -- clear a stale open alert if one lingered.
        alerts.resolve_for_serial(DB_PATH, serial)
        return [r["machine"] for r in rows]

    online = [r for r in rows if derive_machine_status(r["updated_at"]) == "online"]
    if len(online) >= 2:
        # Two live machines claim one serial -- refuse to auto-merge and raise a
        # duplicate_serial alert so an operator can pick a survivor and merge manually.
        alerts.upsert_duplicate(DB_PATH, serial, [r["machine"] for r in rows])
        return [r["machine"] for r in rows]

    if online:
        survivor = online[0]["machine"]
    else:
        # All offline: keep the most recently updated row (updated_at is fixed-width
        # "YYYY-MM-DD HH:MM:SS", so a lexicographic max is a chronological max).
        survivor = max(rows, key=lambda r: r["updated_at"] or "")["machine"]

    for r in rows:
        if r["machine"] != survivor:
            merge_machines(survivor, r["machine"], actor=actor)
    # Collision collapsed to a single record -- clear any alert it had raised.
    alerts.resolve_for_serial(DB_PATH, serial)
    return [survivor]


def resolve_all_duplicate_serials(actor="system:dedup:startup"):
    """One-shot startup sweep: collapse every set of duplicate rows sharing a valid
    serial. Cleans up duplicates that predate this feature, including all-offline ones
    no live report would otherwise trigger a merge for."""
    with get_db_conn() as conn:
        rows = conn.execute(
            """
            SELECT serial_number
            FROM machine_info
            WHERE serial_number IS NOT NULL AND TRIM(serial_number) <> ''
            GROUP BY serial_number COLLATE NOCASE
            HAVING COUNT(*) > 1
            """
        ).fetchall()
    for row in rows:
        serial = row["serial_number"]
        if not is_valid_serial(serial):
            continue
        try:
            resolve_serial_group(serial, actor=actor)
        except Exception as e:
            print(f"[dedup] Failed to resolve duplicates for serial {serial!r}: {e}")

def _history_column(column):
    """Guard: only a whitelisted `readings` metric column may be interpolated into the
    history SQL below. Callers resolve it from HISTORY_METRIC_COLUMNS, but validate here
    too so a stray caller can never inject a column name."""
    if column not in _ALLOWED_HISTORY_COLUMNS:
        raise ValueError(f"Unknown history column: {column!r}")
    return column

def _scope_clause(allowed_machines):
    """SQL fragment + params restricting a readings query to a caller's machine scope.

    `allowed_machines` is None for an unrestricted caller (no clause at all) and a
    collection otherwise. An EMPTY collection must still produce a clause -- "IN ()"
    is not valid SQL, so it becomes a literal 0=1. Getting that wrong would turn "this
    operator can see nothing" into "this operator can see everything", which is
    exactly the wrong direction to fail.

    The filter has to be IN THE QUERY rather than applied to its results, because
    these queries are LIMITed: filtering afterwards would let out-of-scope rows eat
    the row budget and hand a scoped operator a mysteriously short chart.
    """
    if allowed_machines is None:
        return "", []
    names = list(allowed_machines)
    if not names:
        return " AND 0 = 1", []
    return f" AND machine IN ({','.join('?' for _ in names)})", names


def query_raw_history(start_epoch, end_epoch, machine, limit, column="temp",
                      allowed_machines=None):
    column = _history_column(column)
    # Metric columns are nullable (older readings, or a toggled-off metric); skip NULLs so
    # a panel shows a gap rather than a fabricated 0. `temp` is NOT NULL, so this is a no-op
    # for the default.
    sql = f"""
        SELECT machine, ts_text, {column} AS value
        FROM readings
        WHERE ts_epoch >= ? AND ts_epoch <= ? AND {column} IS NOT NULL
    """
    params = [start_epoch, end_epoch]
    if machine:
        sql += " AND machine = ?"
        params.append(machine)
    scope_sql, scope_params = _scope_clause(allowed_machines)
    sql += scope_sql
    params.extend(scope_params)

    sql += " ORDER BY ts_epoch DESC"
    if limit is not None:
        max_rows = limit if machine else limit * MAX_HISTORY_MACHINE_MULTIPLIER
        sql += " LIMIT ?"
        params.append(max_rows)

    history = defaultdict(deque) if limit is None else defaultdict(lambda: deque(maxlen=limit))
    with get_db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    for row in rows:
        history[row["machine"]].appendleft({
            "x": row["ts_text"],
            "y": round(float(row["value"]), 1),
        })
    return {machine_name: list(points) for machine_name, points in history.items()}

def query_bucketed_history(start_epoch, end_epoch, machine, limit, bucket_seconds,
                           column="temp", allowed_machines=None):
    column = _history_column(column)
    # AVG/MIN/MAX ignore NULLs, but a bucket of only NULLs would yield NULL; the IS NOT NULL
    # filter drops those buckets entirely so they never reach float().
    sql = f"""
        SELECT
            machine,
            CAST((ts_epoch / ?) AS INTEGER) * ? AS bucket_epoch,
            AVG({column}) AS avg_value,
            MIN({column}) AS min_value,
            MAX({column}) AS max_value,
            COUNT({column}) AS sample_count
        FROM readings
        WHERE ts_epoch >= ? AND ts_epoch <= ? AND {column} IS NOT NULL
    """
    params = [bucket_seconds, bucket_seconds, start_epoch, end_epoch]
    if machine:
        sql += " AND machine = ?"
        params.append(machine)
    scope_sql, scope_params = _scope_clause(allowed_machines)
    sql += scope_sql
    params.extend(scope_params)
    sql += " GROUP BY machine, bucket_epoch ORDER BY bucket_epoch DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit if machine else limit * MAX_HISTORY_MACHINE_MULTIPLIER)

    history = defaultdict(deque) if limit is None else defaultdict(lambda: deque(maxlen=limit))
    with get_db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    for row in rows:
        bucket_time = datetime.fromtimestamp(int(row["bucket_epoch"]))
        history[row["machine"]].appendleft({
            "x": to_timestamp_str(bucket_time),
            "y": round(float(row["avg_value"]), 1),
            "min": round(float(row["min_value"]), 1),
            "max": round(float(row["max_value"]), 1),
            "count": int(row["sample_count"]),
        })
    return {machine_name: list(points) for machine_name, points in history.items()}

# ================================
# RETENTION  --  keep the readings table bounded to data.retention_days
# ================================
def prune_old_readings_once():
    """Delete readings older than data.retention_days, in batches so the first big prune
    (potentially millions of rows) never holds a single long write lock that would
    stall the reading writer. Returns the number of rows removed."""
    retention_days = settings.get_int(DB_PATH, "data.retention_days")
    cutoff = int(time.time()) - retention_days * 86400
    total = 0
    while True:
        with get_db_conn() as conn:
            cur = conn.execute(
                "DELETE FROM readings WHERE id IN "
                "(SELECT id FROM readings WHERE ts_epoch < ? LIMIT ?)",
                (cutoff, RETENTION_PRUNE_BATCH),
            )
            deleted = cur.rowcount or 0
        total += deleted
        if deleted < RETENTION_PRUNE_BATCH:
            break
        time.sleep(0.2)  # let other writers/readers through between batches
    if total:
        print(f"[retention] Pruned {total} reading(s) older than {retention_days} days.")
    return total


def prune_command_output_once():
    """Drop live-terminal scrollback for commands that finished long ago. The durable
    record (command_results.output) is untouched -- these rows only exist so an operator
    can watch a command stream, and 256KB per command adds up otherwise."""
    cutoff = int(time.time()) - settings.get_int(
        DB_PATH, "data.command_output_retention_seconds")
    removed = fleet.prune_command_output(DB_PATH, cutoff)
    if removed:
        print(f"[retention] Pruned {removed} command output chunk(s).")
    return removed


# How often the pruner wakes to check whether it's due. Deliberately much shorter than
# the prune interval itself: sleeping the whole interval in one call would mean an
# operator's change to data.prune_interval_seconds didn't take effect until the next
# prune (up to a week away), which reads as "the setting doesn't work".
PRUNE_TICK_SECONDS = 30


def retention_pruner():
    # monotonic(), not time(), so an NTP correction or a DST step can't strand the
    # pruner for hours or fire it in a tight loop.
    last_run = None
    while True:
        interval = settings.get_int(DB_PATH, "data.prune_interval_seconds")
        if last_run is None or (time.monotonic() - last_run) >= interval:
            try:
                prune_old_readings_once()
            except Exception as e:
                print(f"[retention] Prune failed: {e}")
            # Separate try: a failure pruning chunks must not stop readings being pruned,
            # and vice versa -- the readings table is the one that grows unboundedly.
            try:
                prune_command_output_once()
            except Exception as e:
                print(f"[retention] Command-output prune failed: {e}")
            last_run = time.monotonic()
        time.sleep(PRUNE_TICK_SECONDS)


def start_retention_pruner():
    threading.Thread(target=retention_pruner, daemon=True, name="retention_pruner").start()


def deploy_scheduler():
    """Advance package deployments: read finished attempts back, dispatch due ones.

    One thread, one pass at a time, so there is never a second tick racing the first to
    dispatch the same target -- packages.dispatch_once only ever moves a row out of
    `pending`, but two concurrent passes could both read it as pending. Being single-
    threaded is the cheapest way to make that impossible, and the work is a couple of
    indexed queries against a table with one row per machine per deploy.

    Errors are caught and logged, never allowed to kill the thread: a deployment that
    stops advancing because one malformed package raised is a silent failure, and the
    operator's only symptom would be a progress bar that never moves.
    """
    while True:
        interval = settings.get_int(DB_PATH, "deploy.scheduler_interval_seconds")
        try:
            reconciled, dispatched = packages.tick(
                DB_PATH,
                ttl_seconds=settings.get_int(DB_PATH, "fleet.command_ttl_seconds"),
                hub_url=HUB_URL,
            )
            if reconciled or dispatched:
                print(f"[deploy] Reconciled {reconciled}, dispatched {dispatched}.")
        except Exception as e:
            print(f"[deploy] Scheduler pass failed: {e}")
        time.sleep(interval)


def start_deploy_scheduler():
    threading.Thread(target=deploy_scheduler, daemon=True, name="deploy_scheduler").start()


init_db()
fleet.init_fleet_db(DB_PATH)
alerts.init_alerts_db(DB_PATH)
settings.init_settings_db(DB_PATH)
permissions.init_permissions_db(DB_PATH)
packages.init_packages_db(DB_PATH)
# Collapse any duplicate-serial rows left by past agent-upgrade renames before serving.
try:
    resolve_all_duplicate_serials()
except Exception as e:
    print(f"[dedup] Startup duplicate sweep failed: {e}")
start_companion_version_watcher()
start_hub_update_watcher()
start_retention_pruner()
start_deploy_scheduler()

# ================================
# LOCAL TEMP READ & LOGGING THREAD
# ================================
def get_cpu_temp():
    try:
        response = requests.get(LHM_URL, timeout=3)
        data = response.json()

        def find_cpu_package_temp(node):
            if isinstance(node, dict):
                # Match EXACT sensor you want
                if (
                    node.get("Type") == "Temperature" and
                    node.get("Text") == "CPU Package"
                ):
                    raw = node.get("Value", "")
                    return float(raw.replace("°C", "").strip())

                # Search children
                for child in node.get("Children", []):
                    result = find_cpu_package_temp(child)
                    if result is not None:
                        return result

            return None

        temp = find_cpu_package_temp(data)

        if temp is not None:
            return round(temp, 1)

    except Exception as e:
        print(f"Error reading REST API temp: {e}")

    return None

last_temp = None
logger_thread = None
logger_lock = threading.Lock()

def local_logger():
    global last_temp
    
    # 2. Initialize COM for this specific background thread
    pythoncom.CoInitialize() 
    
    try:
        while True:
            temp = get_cpu_temp() 
            
            if temp is not None:
                if last_temp and abs(temp - last_temp) >= SPIKE_THRESHOLD:
                    print(f"WARNING SPIKE: {last_temp} -> {temp}")
                if temp >= settings.get_int(DB_PATH, "hub.overheat_threshold"):
                    print(f"OVERHEATING: {temp}°C")
                
                save_and_emit_temp(LOCAL_MACHINE, temp, get_uptime_seconds())
                last_temp = temp
                
            time.sleep(CHECK_INTERVAL)
    finally:
        pythoncom.CoUninitialize()

def start_local_logger():
    global logger_thread
    with logger_lock:
        if logger_thread and logger_thread.is_alive():
            return
        logger_thread = threading.Thread(target=local_logger, daemon=True, name="local_logger")
        logger_thread.start()

# ================================
# API FOR REMOTE MACHINES
# ================================
@app.route('/api/report', methods=['POST'])
def report_temp():
    """Endpoint for other machines to send their temps via POST request"""
    data = request.json
    if not data or 'machine' not in data or 'temp' not in data:
        return jsonify({"error": "Invalid payload"}), 400

    machine = data['machine']
    if not is_valid_machine_name(machine):
        return jsonify({"error": "Invalid machine name"}), 400
    machine = str(machine).strip()
    # float() on a non-numeric temp would otherwise surface as an unhandled 500.
    try:
        temp_value = float(data['temp'])
    except (TypeError, ValueError):
        return jsonify({"error": "temp must be a number"}), 400
    try:
        uptime_seconds = int(data['uptime_seconds']) if data.get('uptime_seconds') is not None else None
    except (TypeError, ValueError):
        uptime_seconds = None
    sensors = data.get('sensors')
    if not isinstance(sensors, list):
        sensors = None
    # Optional companion-supplied timestamp (used to backfill readings buffered
    # while the hub was down). Ignore values that are in the future or older than
    # our retention window -- those are clock-skew garbage, fall back to now().
    client_ts = data.get('client_ts')
    try:
        client_ts = int(client_ts) if client_ts is not None else None
    except (TypeError, ValueError):
        client_ts = None
    if client_ts is not None:
        now_epoch = int(time.time())
        # Bounded by data.ingest_max_backdate_days, NOT by the retention window. They
        # default to the same 30 days but are deliberately separate: shortening retention
        # must not start silently flattening reconnect backfills. This code nulls
        # client_ts rather than rejecting the report, so an over-tight bound would stamp
        # a week of buffered readings with a single arrival time.
        max_backdate = settings.get_int(DB_PATH, "data.ingest_max_backdate_days")
        if client_ts > now_epoch + 300 or client_ts < now_epoch - max_backdate * 86400:
            client_ts = None
    reported_version = data.get('companion_version')
    save_and_emit_temp(machine, temp_value, uptime_seconds, sensors,
                       timestamp_epoch=client_ts, companion_version=reported_version)
    # Keep an enrolled agent's online/offline status fresh off its ordinary temp
    # reports too, so it doesn't read offline between dedicated heartbeats.
    fleet.touch_last_seen(DB_PATH, machine)
    save_machine_info(
        machine,
        data.get('asset_tag'),
        data.get('serial_number'),
        data.get('model'),
        reported_version,
    )
    # Now that this machine's identity is fresh (and online), collapse any offline
    # duplicate reporting the same BIOS serial -- the OpenClaw -> OPENCLAW rename case.
    # Never let a dedup hiccup fail the report itself.
    reported_serial = data.get('serial_number')
    if is_valid_serial(reported_serial):
        try:
            resolve_serial_group(reported_serial)
        except Exception as e:
            print(f"[dedup] Duplicate-serial resolution failed for {machine!r}: {e}")

    response_payload = {"status": "success"}
    latest_version = get_advertised_version(reported_version)
    if latest_version:
        response_payload["latest_version"] = latest_version
    return jsonify(response_payload), 200

@app.route('/api/machines')
@login_required
@access.require(permissions.VIEW)
def get_machines():
    """Machine identity info (asset tag / serial number / model / companion version)
    reported by companions, plus their latest known live temp and uptime.

    Filtered to the caller's machine scope: an HR operator must not even SEE Hospital
    machines here, since this list is what the Dashboard and Asset Inventory render."""
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT machine, asset_tag, serial_number, model, companion_version, updated_at "
            "FROM machine_info ORDER BY machine ASC"
        ).fetchall()
    result = [dict(row) for row in rows]
    known_machines = {row['machine'] for row in result}
    # Also surface machines that have reported temps but no identity fields yet
    # (e.g. an older companion, or the very first report before a DB write lands).
    for machine in list(latest_temp.keys()) + list(latest_uptime.keys()):
        if machine not in known_machines:
            result.append({
                'machine': machine, 'asset_tag': None, 'serial_number': None,
                'model': None, 'companion_version': None, 'updated_at': None,
            })
            known_machines.add(machine)
    # Narrow BEFORE enriching -- there is no reason to read sensors for machines the
    # caller will never be shown.
    result = access.filter_rows(result)
    for row in result:
        row['uptime_seconds'] = get_latest_uptime(row['machine'])
        row['temp'] = get_latest_temp(row['machine'])
        row['diagnostics'] = extract_diagnostics(get_latest_sensors(row['machine']))
        row['status'] = derive_machine_status(row['updated_at'])
    result.sort(key=lambda row: row['machine'])
    return jsonify(result)


@app.route('/api/machines/<machine>')
@login_required
@access.require_machine(permissions.VIEW)
def get_machine(machine):
    """Single machine's identity info + latest live temp/uptime, for its detail page."""
    machine_name = str(machine).strip()
    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT machine, asset_tag, serial_number, model, companion_version, updated_at "
            "FROM machine_info WHERE machine = ?",
            (machine_name,),
        ).fetchone()
    uptime_seconds = get_latest_uptime(machine_name)
    temp = get_latest_temp(machine_name)
    if row is None and uptime_seconds is None and temp is None:
        return jsonify({"error": "Unknown machine"}), 404

    result = dict(row) if row else {
        'machine': machine_name, 'asset_tag': None, 'serial_number': None,
        'model': None, 'companion_version': None, 'updated_at': None,
    }
    result['uptime_seconds'] = uptime_seconds
    result['temp'] = temp
    result['diagnostics'] = extract_diagnostics(get_latest_sensors(machine_name))
    result['status'] = derive_machine_status(result.get('updated_at'))
    result['primary_sensor_name'] = get_primary_sensor_override(machine_name)
    return jsonify(result)


def _recent_sensors_for(machine_name):
    """This machine's freshest sensor block: the in-memory cache, falling back to the
    newest stored block so the picker still works right after a hub restart."""
    sensors = get_latest_sensors(machine_name)
    if sensors:
        return sensors
    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT sensors_json FROM readings WHERE machine = ? AND sensors_json IS NOT NULL "
            "ORDER BY ts_epoch DESC LIMIT 1",
            (machine_name,),
        ).fetchone()
    if not row or not row["sensors_json"]:
        return None
    try:
        return json.loads(row["sensors_json"])
    except (TypeError, ValueError):
        return None


@app.route('/api/machines/<machine>/sensors')
@login_required
@access.require_machine(permissions.VIEW)
def get_machine_sensors(machine):
    """CPU temperature sensors this machine is actually reporting, for the primary-sensor
    picker. Returns current values too, so an operator chooses by recognition ("CPU
    Package -- 61.0 °C") instead of typing a name that has to match exactly."""
    machine_name = str(machine).strip()
    sensors = _recent_sensors_for(machine_name)
    available = [
        {"name": name, "value": value}
        for name, value in _cpu_temp_candidates(sensors)
    ]
    return jsonify({
        "machine": machine_name,
        "sensors": available,
        "primary_sensor_name": get_primary_sensor_override(machine_name),
        "preference": settings.get_list(DB_PATH, "computer.primary_sensor_preference"),
    })


@app.route('/api/machines/<machine>/primary_sensor', methods=['PUT'])
@login_required
@access.require_machine(permissions.MANAGE_SETTINGS)
def put_machine_primary_sensor(machine):
    """Pin this machine's primary temperature to one named sensor, or clear the pin
    (null/empty) to fall back to the fleet-wide preference order."""
    machine_name = str(machine).strip()
    # silent=True, never force=True -- same CSRF reasoning as fleet_web/settings_web.
    data = request.get_json(silent=True) or {}
    name = data.get("primary_sensor_name")
    if name is not None and not isinstance(name, str):
        return jsonify({"error": "primary_sensor_name must be a string or null"}), 400

    with get_db_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM machine_info WHERE machine = ?", (machine_name,)).fetchone()
    if not exists:
        return jsonify({"error": "Unknown machine"}), 404

    applied = set_primary_sensor_override(machine_name, name)
    fleet.audit(DB_PATH, (session.get("user") or {}).get("email", "unknown"),
                "machine.primary_sensor", machine_name, {"to": applied})
    return jsonify({"status": "saved", "primary_sensor_name": applied})


@app.route('/api/machines/<machine>', methods=['DELETE'])
@login_required
@access.require_machine(permissions.MANAGE_SETTINGS)
def delete_machine(machine):
    """Hard-delete a decommissioned machine: its identity row, all temperature history,
    and its fleet agent enrollment. Irreversible. If the machine's companion is still
    running it will re-enroll and reappear on its next report -- this is meant for
    machines that are actually gone."""
    machine_name = str(machine).strip()
    if not machine_name:
        return jsonify({"error": "Machine name required"}), 400
    with get_db_conn() as conn:
        conn.execute("DELETE FROM readings WHERE machine = ?", (machine_name,))
        conn.execute("DELETE FROM machine_info WHERE machine = ?", (machine_name,))
    fleet.delete_machine(DB_PATH, machine_name)
    # Drop the hostname from every permission group's scope too. If the name is later
    # reused by a different physical box, it must not silently inherit this one's
    # access grants -- a stale grant is invisible until it's abused.
    permissions.forget_machine(DB_PATH, machine_name)
    # And drop its deployment targets, so a deploy isn't left stuck at 9/10 waiting on a
    # machine whose command rows fleet.delete_machine has just removed.
    packages.forget_machine(DB_PATH, machine_name)
    # Drop any in-memory live status so a deleted machine doesn't linger on the Dashboard.
    _evict_live_status(machine_name)
    actor = (session.get("user") or {}).get("email", "unknown")
    fleet.audit(DB_PATH, actor, "machine.delete", machine_name)
    return jsonify({"status": "deleted"}), 200


@app.route('/api/alerts')
@login_required
@access.require(permissions.VIEW)
def get_alerts():
    """Open alerts for the Alerts tab. Each duplicate_serial alert is enriched with the
    current status/model of every machine involved, so the UI can show which are still
    online and let the operator pick a survivor to merge into."""
    open_alerts = alerts.list_open(DB_PATH)
    with get_db_conn() as conn:
        info = {r["machine"]: r for r in conn.execute(
            "SELECT machine, model, updated_at FROM machine_info"
        ).fetchall()}
    keep = access.machine_filter()
    visible = []
    for alert in open_alerts:
        involved = alert.get("machines", [])
        in_scope = involved if keep is None else [m for m in involved if keep(m)]
        # An alert touching none of the caller's machines isn't theirs to see.
        if involved and not in_scope:
            continue
        enriched = []
        for machine in in_scope:
            row = info.get(machine)
            enriched.append({
                "machine": machine,
                "present": row is not None,
                "status": derive_machine_status(row["updated_at"]) if row else "offline",
                "model": (row["model"] if row else None),
                "updated_at": (row["updated_at"] if row else None),
            })
        alert["machines"] = enriched
        # A duplicate-serial alert can straddle a scope boundary (that is rather the
        # point of a serial collision). Report the count of machines withheld rather
        # than their names, so the operator understands why the merge control is
        # unavailable to them without learning hostnames outside their scope.
        alert["hidden_machines"] = len(involved) - len(in_scope)
        visible.append(alert)
    return jsonify(visible)


@app.route('/api/machines/merge', methods=['POST'])
@login_required
@access.require(permissions.MANAGE_SETTINGS)
def merge_machines_endpoint():
    """Operator-triggered merge of duplicate machines. Body: {survivor, victims:[...]}.
    Absorbs each victim into the survivor (history preserved) and resolves any open
    duplicate_serial alert for the survivor's serial."""
    data = request.json or {}
    survivor = str(data.get("survivor") or "").strip()
    victims = data.get("victims") or []
    if not survivor or not isinstance(victims, list):
        return jsonify({"error": "survivor and a victims list are required"}), 400
    victims = [str(v).strip() for v in victims if str(v).strip() and str(v).strip() != survivor]
    if not victims:
        return jsonify({"error": "no valid victims to merge"}), 400

    names = [survivor] + victims
    # EVERY machine involved must be in scope, not just the survivor. A merge destroys
    # one identity row and re-points its history onto another; being able to do that to
    # a machine you can't see would be a way to reach outside your scope.
    out_of_scope = [n for n in names if not access.in_scope(n)]
    if out_of_scope:
        return jsonify({"error": "one or more machines are outside your access scope"}), 403
    with get_db_conn() as conn:
        found = {r["machine"]: r["serial_number"] for r in conn.execute(
            f"SELECT machine, serial_number FROM machine_info "
            f"WHERE machine IN ({','.join('?' for _ in names)})",
            names,
        ).fetchall()}
    if survivor not in found:
        return jsonify({"error": f"unknown survivor '{survivor}'"}), 404
    missing = [v for v in victims if v not in found]
    if missing:
        return jsonify({"error": f"unknown machine(s): {', '.join(missing)}"}), 404

    actor = (session.get("user") or {}).get("email", "unknown")
    for victim in victims:
        merge_machines(survivor, victim, actor=actor)
    if found.get(survivor):
        alerts.resolve_for_serial(DB_PATH, found[survivor])
    return jsonify({"status": "merged", "survivor": survivor, "victims": victims}), 200


@app.route('/api/alerts/<int:alert_id>/dismiss', methods=['POST'])
@login_required
@access.require(permissions.MANAGE_SETTINGS)
def dismiss_alert(alert_id):
    if not alerts.dismiss(DB_PATH, alert_id):
        return jsonify({"error": "no open alert with that id"}), 404
    actor = (session.get("user") or {}).get("email", "unknown")
    fleet.audit(DB_PATH, actor, "alert.dismiss", str(alert_id))
    return jsonify({"status": "dismissed"}), 200

def _resolve_history_window(args):
    """Parse the shared date/from/to/resolution/limit query params into
    (start_epoch, end_epoch, resolution, limit). Raises ValueError with a user-facing
    message on a bad date. Shared by /api/history and the per-machine history endpoint."""
    date = args.get("date")
    from_raw = args.get("from")
    to_raw = args.get("to")
    limit = parse_history_limit(args.get("limit"))
    requested_resolution = (args.get("resolution") or "auto").strip().lower()

    if date:
        day_start = parse_request_datetime(date)
        if day_start is None:
            raise ValueError("Invalid date format; use YYYY-MM-DD.")
        day_start = day_start.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        start_dt = parse_request_datetime(from_raw) or day_start
        end_dt = parse_request_datetime(to_raw) or day_end
        start_dt = max(start_dt, day_start)
        end_dt = min(end_dt, day_end)
        ensure_day_loaded_from_csv(date)
    else:
        end_dt = parse_request_datetime(to_raw) or datetime.now()
        start_dt = parse_request_datetime(from_raw)
        if start_dt is None:
            default_window = settings.get_int(DB_PATH, "hub.live_default_window_hours")
            start_dt = get_oldest_reading_datetime() or (end_dt - timedelta(hours=default_window))

    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    start_epoch = to_epoch_seconds(start_dt)
    end_epoch = to_epoch_seconds(end_dt)
    span_seconds = max(1, end_epoch - start_epoch)
    resolution = pick_resolution(requested_resolution, span_seconds)
    return start_epoch, end_epoch, resolution, limit


def _query_history_series(start_epoch, end_epoch, machine, limit, resolution, column,
                          allowed_machines=None):
    """Dispatch to the raw or bucketed query for one metric column."""
    if resolution == "raw":
        return query_raw_history(start_epoch, end_epoch, machine, limit, column,
                                 allowed_machines)
    return query_bucketed_history(
        start_epoch, end_epoch, machine, limit, VALID_RESOLUTIONS[resolution], column,
        allowed_machines)


@app.route('/api/history')
@login_required
@access.require(permissions.VIEW)
def get_history():
    """Provide history data with optional range/machine/resolution/metric controls.
    `metric` (default "temp") selects which column to chart; see HISTORY_METRIC_COLUMNS."""
    machine = (request.args.get("machine") or "").strip() or None
    metric = (request.args.get("metric") or "temp").strip().lower()
    column = HISTORY_METRIC_COLUMNS.get(metric)
    if column is None:
        return jsonify({"error": f"Unknown metric: {metric}"}), 400
    # A named machine outside the caller's scope is refused outright; the fleet-wide
    # form is narrowed in SQL instead (see _scope_clause).
    if machine and not access.in_scope(machine):
        return jsonify({"error": f"You do not have access to {machine!r}."}), 403
    try:
        start_epoch, end_epoch, resolution, limit = _resolve_history_window(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    history = _query_history_series(start_epoch, end_epoch, machine, limit, resolution,
                                   column, access.current()["machines"])
    return jsonify(history)


@app.route('/api/machines/<machine>/history')
@login_required
@access.require_machine(permissions.VIEW)
def get_machine_history(machine):
    """Multi-metric history for ONE machine, backing the per-machine dashboard panels.

    Returns {"machine", "resolution", "metrics": {metric_key: [{x, y[, min, max, count]}]}}
    so the page fetches every panel in a single round trip. `metrics` is an optional
    comma-separated list of keys (default: all); unknown keys are ignored. Internally this
    runs one indexed query per metric over the same (machine, ts_epoch) index -- cheap for a
    single machine; collapse to one GROUP-BY pass if profiling ever shows it matters."""
    machine_name = str(machine).strip()
    if not machine_name:
        return jsonify({"error": "machine required"}), 400

    requested = request.args.get("metrics")
    if requested:
        keys = [k.strip().lower() for k in requested.split(",") if k.strip()]
        keys = [k for k in keys if k in HISTORY_METRIC_COLUMNS]
    else:
        keys = list(HISTORY_METRIC_COLUMNS.keys())

    try:
        start_epoch, end_epoch, resolution, limit = _resolve_history_window(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    metrics = {}
    for key in keys:
        # The route decorator already established machine_name is in scope, so no
        # allowed_machines filter is needed on top of the machine = ? predicate.
        series = _query_history_series(
            start_epoch, end_epoch, machine_name, limit, resolution, HISTORY_METRIC_COLUMNS[key])
        metrics[key] = series.get(machine_name, [])
    return jsonify({"machine": machine_name, "resolution": resolution, "metrics": metrics})

@app.route('/api/daily_summary')
@login_required
@access.require(permissions.VIEW)
def get_daily_summary():
    """Provide daily averages and reading counts for selected date."""
    date = request.args.get("date") or today_str()
    ensure_day_loaded_from_csv(date)

    day_start = parse_request_datetime(date)
    if day_start is None:
        return jsonify({"error": "Invalid date format; use YYYY-MM-DD."}), 400
    day_start = day_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    start_epoch = to_epoch_seconds(day_start)
    end_epoch = to_epoch_seconds(day_end)
    # Same scope narrowing as the history queries: a fleet-wide average that silently
    # included machines the caller cannot see would leak their existence.
    scope_sql, scope_params = _scope_clause(access.current()["machines"])

    with get_db_conn() as conn:
        summary = conn.execute(
            f"""
            SELECT AVG(temp) AS overall_avg, COUNT(*) AS reading_count
            FROM readings
            WHERE ts_epoch >= ? AND ts_epoch < ?{scope_sql}
            """,
            (start_epoch, end_epoch, *scope_params),
        ).fetchone()

        if not summary or int(summary["reading_count"]) == 0:
            return jsonify({
                "date": date,
                "overall_avg": None,
                "machine_averages": {},
                "machine_count": 0,
                "reading_count": 0
            })

        machine_rows = conn.execute(
            f"""
            SELECT machine, AVG(temp) AS avg_temp
            FROM readings
            WHERE ts_epoch >= ? AND ts_epoch < ?{scope_sql}
            GROUP BY machine
            ORDER BY machine ASC
            """,
            (start_epoch, end_epoch, *scope_params),
        ).fetchall()

    machine_averages = {
        row["machine"]: round(float(row["avg_temp"]), 1)
        for row in machine_rows
    }

    return jsonify({
        "date": date,
        "overall_avg": round(float(summary["overall_avg"]), 1),
        "machine_averages": machine_averages,
        "machine_count": len(machine_averages),
        "reading_count": int(summary["reading_count"])
    })

@app.context_processor
def inject_nav_context():
    """Feed the sidebar on every page render: the Alerts badge, and which nav links the
    caller may actually follow.

    Hiding a link is presentation, not security -- every route behind it is gated
    server-side regardless. But a sidebar full of links that 403 is worse than useless,
    so the template needs the capability set.

    The alert badge is scoped too: a count that included machines the caller cannot see
    would say "3 alerts" and then show them one. It walks the (small) open-alert list
    rather than COUNTing, since scoping needs the machine names. Never let any of this
    break a page -- it renders on login too, where there is no session at all.
    """
    context = {"open_alert_count": 0, "user_capabilities": set(),
               "is_superuser": False, "cap": permissions}
    if not session.get("user"):
        return context
    try:
        current = access.current()
        context["user_capabilities"] = current["capabilities"]
        context["is_superuser"] = current["superuser"]
        keep = access.machine_filter()
        if keep is None:
            context["open_alert_count"] = alerts.count_open(DB_PATH)
        else:
            context["open_alert_count"] = sum(
                1 for a in alerts.list_open(DB_PATH)
                if not a.get("machines") or any(keep(m) for m in a["machines"])
            )
    except Exception:
        pass
    return context

@app.route("/")
@login_required
@access.require(permissions.VIEW)
def index():
    return render_template("index.html", hub_version=HUB_VERSION,
                           overheat_threshold=settings.get_int(DB_PATH, "hub.overheat_threshold"),
                           low_load_threshold=settings.get_int(DB_PATH, "hub.low_load_threshold"),
                           latest_companion_version=get_latest_companion_version(),
                           latest_agent_version=get_latest_agent_version())

@app.route("/history")
@login_required
@access.require(permissions.VIEW)
def history_page():
    return render_template("history.html", hub_version=HUB_VERSION,
                           latest_companion_version=get_latest_companion_version(),
                           latest_agent_version=get_latest_agent_version())

@app.route("/inventory")
@login_required
@access.require(permissions.VIEW)
def inventory_page():
    return render_template("inventory.html", hub_version=HUB_VERSION,
                           latest_companion_version=get_latest_companion_version(),
                           latest_agent_version=get_latest_agent_version())

@app.route("/alerts")
@login_required
@access.require(permissions.VIEW)
def alerts_page():
    return render_template("alerts.html", hub_version=HUB_VERSION,
                           latest_companion_version=get_latest_companion_version(),
                           latest_agent_version=get_latest_agent_version())

@app.route("/settings")
@login_required
@access.require(permissions.MANAGE_SETTINGS)
def settings_page():
    return render_template("settings.html", hub_version=HUB_VERSION,
                           latest_companion_version=get_latest_companion_version(),
                           latest_agent_version=get_latest_agent_version())

@app.route("/permissions")
@login_required
@access.require(permissions.MANAGE_PERMISSION_GROUPS)
def permissions_page():
    return render_template("permissions.html", hub_version=HUB_VERSION,
                           latest_companion_version=get_latest_companion_version(),
                           latest_agent_version=get_latest_agent_version())

@app.route("/machine/<machine>")
@login_required
@access.require_machine(permissions.VIEW)
def machine_page(machine):
    return render_template(
        "machine.html", machine=machine,
        overheat_threshold=settings.get_int(DB_PATH, "hub.overheat_threshold"),
        low_load_threshold=settings.get_int(DB_PATH, "hub.low_load_threshold"),
        enabled_metrics=enabled_history_metrics(),
        hub_version=HUB_VERSION,
        latest_companion_version=get_latest_companion_version(),
        latest_agent_version=get_latest_agent_version()
    )

# ================================
# START
# ================================
application = app

if __name__ == "__main__":
    # Local self-reporting is intentionally disabled: the companion agent runs on
    # the hub machine too and reports this host with full sensor data, so starting
    # local_logger here would double-report the hostname and make the dashboard's
    # Load/Clock flicker. See wsgi.py for how to re-enable on a companion-less box.
    # start_local_logger()

    # Use socketio.run instead of app.run
    print(f"Starting hub on {LOCAL_MACHINE}...")
    socketio.run(app, host="0.0.0.0", port=3001, debug=False, allow_unsafe_werkzeug=True)
