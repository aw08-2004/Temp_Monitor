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
from flask import Flask, render_template, request, jsonify, redirect, session, url_for, g
from flask_socketio import SocketIO, join_room
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
import requests

import fleet
import alerts
import settings
import terminal
import permissions
import users
import packages
import backups
import remote
import directory
import bios
import firmware
import wake
import processes
import live
import authconfig
import apitokens
import i18n
import permissions_web
from fleet_web import create_fleet_blueprint
from settings_web import create_settings_blueprint
from permissions_web import create_access, create_permissions_blueprint
from users_web import create_users_blueprint
from audit_web import create_audit_blueprint
from packages_web import create_packages_blueprint
from backups_web import create_backups_blueprint
from remote_web import create_remote_blueprint
from bios_web import create_bios_blueprint
from wake_web import create_wake_blueprint
from processes_web import create_processes_blueprint
from directory_web import create_directory_blueprint
from auth_web import create_auth_blueprint
from apitokens_web import create_apitokens_blueprint

# The hub's code lives in a `hub/` subdirectory; its mutable state (.env, logs/, the
# telemetry DB) lives one level up in the install root. Keeping the two apart is what lets
# the self-updater mirror the whole code dir wholesale without an allowlist -- a code
# refresh never has to step around operator data, because none of it is in the code dir.
HUB_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
# The install/repo root: the parent of the code dir. Overridable via HUB_STATE_DIR for odd
# deployments, but the default matches both what install.ps1 lays down (…\Hub\hub -> …\Hub)
# and a dev checkout (repo/hub -> repo), so .env and logs/ resolve exactly where they always
# did before the move -- behaviour-preserving for an in-place upgrade.
STATE_ROOT = os.path.abspath(os.environ.get("HUB_STATE_DIR") or os.path.dirname(HUB_CODE_DIR))

# Load .env from the install root rather than the cwd -- under the Windows service the working
# directory is the code dir, not where the config lives -- and with utf-8-sig so a UTF-8 BOM
# (which PowerShell and Windows editors happily prepend) doesn't corrupt the first key and
# blank out the config. The path must stay stable: backups.py appends BACKUP_MASTER_KEY to
# this exact file, and guessing it a second time from a different base is how the key ends up
# written somewhere the next restart doesn't read.
ENV_PATH = os.path.join(STATE_ROOT, ".env")
load_dotenv(ENV_PATH, encoding="utf-8-sig")

# ================================
# CONFIG
# ================================
# Bump on every push to main and restart the hub service -- shown in the
# dashboard header so a stale/un-restarted deployment is obvious at a glance.
HUB_VERSION = "1.77.0"
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
# default sits in the install root (STATE_ROOT), which under the WinSW service is the hub
# install dir -- exactly where the old cwd-relative "logs" resolved to, and unchanged by the
# move of the code into hub/, so an in-place upgrade keeps reading the same logs/temp_v2.db.
LOG_DIR = os.path.abspath(
    os.environ.get("HUB_LOG_DIR")
    or os.path.join(STATE_ROOT, "logs")
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

# When each machine last had a full sensor BLOB written into its readings row, so a machine
# reporting at 1 Hz (see live.py -- somebody has its page open) does not multiply the size of
# the readings table by twelve for as long as they are looking. The blob is ~36 KB and the
# whole of it is stored per row; at a second apart that is ~130 MB an hour for ONE machine,
# for a page that reads none of it.
#
# What the charts read is the typed metric columns beside it, and those are a handful of
# REALs -- they keep being written on every single reading, so the fast cadence is fully
# represented in history. The blob is only ever read back as "the newest one" (the sensor
# picker's fallback after a restart), which does not care whether it is one second old or
# ten. So it is throttled to the cadence it always had.
_last_sensor_blob_epoch = {}
_last_sensor_blob_lock = threading.Lock()
# The agent's own unwatched sensor cadence (AgentConfig.SensorIntervalSeconds). Storing a
# blob more often than a machine normally produces one buys nothing.
SENSOR_BLOB_MIN_SECONDS = 10


def _should_store_sensor_blob(machine_name, timestamp_epoch):
    """Has enough time passed since this machine's last stored sensor blob? Records the
    decision, so callers must only ask when they are about to write the row."""
    with _last_sensor_blob_lock:
        last = _last_sensor_blob_epoch.get(machine_name)
        if last is not None and 0 <= timestamp_epoch - last < SENSOR_BLOB_MIN_SECONDS:
            return False
        _last_sensor_blob_epoch[machine_name] = timestamp_epoch
        return True

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
    preferred-name-first-match style as the agent's SensorReader.PreferredSensors,
    since sensor naming varies across CPU/GPU vendors."""
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
    28 °C board probe, and every high-temperature alert on that machine would quietly stop
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


def _find_sensor_strict(sensors, sensor_type, name_substrs):
    """Like _find_sensor_value, but identifies a metric by its sensor NAME rather than
    by hardware category, and returns None when no name matches -- never a blind
    first-candidate fallback.

    Used for disk usage, where the hardware identifier isn't a single stable substring
    (storage is "/nvme/","/hdd/","/ssd/"...) but the sensor name is distinctive
    ("Used Space"). First match wins. Network throughput does NOT go through here --
    picking the first match is exactly the bug _network_throughput exists to avoid."""
    for s in sensors:
        if s.get("type") != sensor_type:
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        name = str(s.get("name") or "").lower()
        if any(w in name for w in name_substrs):
            return value
    return None


def _network_throughput(sensors):
    """(download_bps, upload_bps) for this machine's busiest network adapter.

    Windows exposes a LOT of NICs, and LHM reports every one of them: Bluetooth,
    disconnected Wi-Fi, Hyper-V/WSL virtual switches, and one pseudo-adapter per NDIS
    filter bound to a real NIC ("Ethernet", "Ethernet-QoS Packet Scheduler-0000",
    "Ethernet-WFP Native MAC Layer LightWeight Filter-0000"...). Fleet machines routinely
    report 20-60 adapters, and the idle ones are enumerated ahead of the live one -- so
    taking the first NIC in the block charted a flat 0 on every machine whose real adapter
    didn't happen to come first. That is the bug this function exists to fix.

    Summing is not the answer either: those filter pseudo-adapters mirror their parent's
    counters, so a sum reports the same traffic four or five times over. Instead pick the
    single busiest adapter by rx+tx and report ITS pair. The mirrors tie with their parent
    on the same value, so the winner is the real number counted exactly once, and both
    directions come from one adapter rather than being mixed across two.

    (None, None) when the block carries no NIC throughput at all -- no NIC hardware, or an
    agent with network collection switched off. A genuinely idle machine reports (0.0, 0.0),
    which is a real reading and charts as such, not as a gap.
    """
    # {hardware_id: [download, upload]} -- grouped per adapter so the pair stays coherent.
    per_nic = {}
    for s in sensors:
        # Disk read/write rate shares SensorType.Throughput with network, so pin to NIC
        # hardware ("/nic/...") to avoid mixing them up.
        if s.get("type") != "Throughput":
            continue
        hardware_id = str(s.get("hardware_id") or "")
        if "nic" not in hardware_id.lower():
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        name = str(s.get("name") or "").lower()
        if "download" in name:
            slot = 0
        elif "upload" in name:
            slot = 1
        else:
            continue
        pair = per_nic.setdefault(hardware_id, [None, None])
        # An adapter reports each direction once; keep the larger if a block ever repeats
        # one, so a stray 0 can't displace a real reading.
        if pair[slot] is None or float(value) > pair[slot]:
            pair[slot] = float(value)

    if not per_nic:
        return None, None
    rx, tx = max(per_nic.values(), key=lambda p: (p[0] or 0.0) + (p[1] or 0.0))
    return rx, tx


def _disk_throughput(sensors):
    """(read_bps, write_bps) summed across every physical disk in this machine.

    The mirror problem that forces _network_throughput to pick a single adapter does not
    exist here -- LHM reports each storage device once, with no filter pseudo-devices -- so
    summing is both correct and what an operator means by "disk I/O on this PC": one number
    per direction covering all drives, matching the single pair of history columns behind it.

    Matched on sensor NAME rather than hardware identifier. Storage identifiers vary
    ("/nvme/", "/hdd/", "/ssd/"), but disks are the only hardware reporting Throughput
    sensors called "Read Rate"/"Write Rate" -- NICs call theirs Download/Upload Speed. NIC
    hardware is excluded anyway, belt and braces, so a future adapter that borrowed the name
    could not leak into the disk chart.

    (None, None) when the block carries no disk throughput at all. An idle disk reports
    (0.0, 0.0), which is a real reading and charts as one.
    """
    # {hardware_id: [read, write]} -- per disk first, so a block that repeats a sensor
    # counts that disk once rather than adding the duplicate into the total.
    per_disk = {}
    for s in sensors:
        if s.get("type") != "Throughput":
            continue
        hardware_id = str(s.get("hardware_id") or "")
        if "nic" in hardware_id.lower():
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        name = str(s.get("name") or "").strip().lower()
        if name == "read rate":
            slot = 0
        elif name == "write rate":
            slot = 1
        else:
            continue
        pair = per_disk.setdefault(hardware_id, [None, None])
        if pair[slot] is None or float(value) > pair[slot]:
            pair[slot] = float(value)

    if not per_disk:
        return None, None
    read = sum(p[0] for p in per_disk.values() if p[0] is not None)
    write = sum(p[1] for p in per_disk.values() if p[1] is not None)
    return read, write


def _disk_volumes(sensors):
    """Per-volume space usage: [{name, used_gb, total_gb, used_pct}, ...], drive letter order.

    Built from the synthetic "/volume/..." sensors the agent appends (VolumeReader.cs) --
    LHM itself reports used space only as a percentage, with the absolute size nowhere in
    its sensor set, so GB has to come from the agent's own DriveInfo walk.

    Falls back to LHM's per-device "Used Space" Load sensors for a machine that isn't
    sending volume sensors (an agent older than 3.10.0). Those carry a
    percentage and no size, so used_gb/total_gb come back None and the UI shows the bar
    without the GB line -- degraded, not broken.
    """
    volumes = {}
    for s in sensors:
        hardware_id = str(s.get("hardware_id") or "")
        if not hardware_id.lower().startswith("/volume/"):
            continue
        if s.get("type") != "Data":
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        name = str(s.get("name") or "").strip().lower()
        if name not in ("total space", "used space"):
            continue
        vol = volumes.setdefault(hardware_id, {"name": str(s.get("hardware") or hardware_id),
                                               "used_gb": None, "total_gb": None})
        vol["used_gb" if name == "used space" else "total_gb"] = float(value)

    if volumes:
        result = []
        for _, vol in sorted(volumes.items()):
            used, total = vol["used_gb"], vol["total_gb"]
            # A volume missing either half can't yield a percentage; report what we have
            # rather than dropping the disk off the page entirely.
            vol["used_pct"] = round(used / total * 100, 1) if used is not None and total else None
            result.append(vol)
        return result

    # Fallback: one entry per storage device reporting a "Used Space" percentage.
    fallback = []
    for s in sensors:
        if s.get("type") != "Load":
            continue
        if str(s.get("name") or "").strip().lower() != "used space":
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        fallback.append({
            "name": str(s.get("hardware") or "Disk"),
            "used_gb": None, "total_gb": None, "used_pct": round(float(value), 1),
        })
    return fallback


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


# Wording that appears on a fan's CONTROL sensor but not on the fan itself, so the two can
# be paired by name: "Fan #2" and "Fan Control #2" both reduce to "fan #2".
_FAN_CONTROL_WORDS = ("control", "pwm")


def _fan_key(name):
    """A fan's identity within one piece of hardware, with the control-sensor wording
    stripped. Used only for pairing -- the name shown to the operator is the fan's own."""
    return " ".join(w for w in str(name or "").lower().split()
                    if w not in _FAN_CONTROL_WORDS)


def _fans(sensors):
    """Every fan this machine reports: [{name, hardware, rpm, control_pct}, ...].

    A list rather than a single number, for the same reason `disks` is one: how many fans a
    PC has is per-machine (a laptop reports one, a workstation six plus a GPU pair), and an
    operator asking "is this thing still cooling itself" wants to see each of them.

    Each fan is paired with its Control sensor -- the duty cycle the board is ASKING for --
    when the two can be matched inside the same hardware. RPM alone doesn't separate "idle,
    ramped down" from "commanded to 100% and seized", and that difference is the whole
    reason to look. control_pct stays None when nothing matches, which is normal: plenty of
    GPUs expose fan speed and no duty cycle at all.

    0 RPM is kept, not dropped. A modern GPU stops its fans entirely below ~50 °C, and a
    header with nothing plugged into it reads 0 too -- both are real answers to "what is
    this fan doing", unlike the 0 °C that _cpu_temp_candidates rejects (a CPU is never
    actually at 0 °C, so there it means "could not read").
    """
    controls = {}
    for s in sensors:
        if s.get("type") != "Control":
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        key = (str(s.get("hardware_id") or ""), _fan_key(s.get("name")))
        controls.setdefault(key, round(float(value), 1))

    fans = []
    for s in sensors:
        if s.get("type") != "Fan":
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        name = str(s.get("name") or "").strip()
        fans.append({
            "name": name,
            "hardware": str(s.get("hardware") or ""),
            "rpm": round(float(value), 1),
            "control_pct": controls.get((str(s.get("hardware_id") or ""), _fan_key(name))),
        })
    return fans


def _fastest_fan_rpm(fans):
    """The single chartable fan number: the fastest fan on the machine.

    Max, not average: a case with five fans idling and one screaming is exactly the machine
    you want the chart to show, and averaging hides it. It also keeps the series meaningful
    when the fan COUNT changes (a GPU's fans stop and drop to 0), which an average would
    make jump for a reason that has nothing to do with cooling.
    """
    speeds = [f["rpm"] for f in fans if isinstance(f.get("rpm"), (int, float))]
    return max(speeds) if speeds else None


def _package_power(sensors, hardware_substr, preferred_name_substrs):
    """Whole-chip power draw in watts for CPU or GPU hardware.

    Preferred names first, like every other pick here. The fallback is the LARGEST Power
    sensor on that hardware rather than the first one listed, which is what
    _find_sensor_value would do: a chip reports its package alongside subsets of itself
    ("CPU Cores", "CPU Graphics", "CPU Memory"), and the package is by definition the
    biggest of them -- whereas "first in the block" charts the graphics rail on one vendor
    and the package on another, and nothing on the page would say which you were looking at.
    """
    candidates = []
    for s in sensors:
        if s.get("type") != "Power":
            continue
        haystack = f"{s.get('hardware_id') or ''} {s.get('hardware') or ''}".lower()
        if hardware_substr not in haystack:
            continue
        value = s.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        candidates.append((str(s.get("name") or "").lower(), float(value)))
    if not candidates:
        return None
    for wanted in preferred_name_substrs:
        for name, value in candidates:
            if wanted in name:
                return value
    return max(value for _, value in candidates)


def extract_diagnostics(sensors):
    """Pulls the specific fields the UI shows out of a raw flattened LHM sensor
    list (see the agent's SensorReader flattening). Every field is None when not
    found -- e.g. no discrete GPU, or an older client that sent no sensors."""
    if not sensors:
        return {
            # "we have never seen this machine's sensors" -- distinct from "it reported and
            # has no GPU". The page hides absent hardware, and hiding on the strength of a
            # block we simply don't have would empty the overview of a machine that is
            # merely offline. Every other key here is None, so this flag is the only way to
            # tell the two apart.
            "has_sensors": False,
            "cpu_load_pct": None, "cpu_clock_mhz": None,
            "gpu_temp": None, "gpu_load_pct": None, "gpu_clock_mhz": None,
            "memory_load_pct": None, "mem_used_gb": None, "mem_total_gb": None,
            "disk_load_pct": None, "net_rx_bps": None, "net_tx_bps": None,
            "disk_read_bps": None, "disk_write_bps": None, "disks": [],
            "fan_rpm": None, "fans": [],
            "cpu_power_w": None, "gpu_power_w": None,
        }
    mem_used_gb, mem_total_gb = _memory_gb(sensors)
    net_rx_bps, net_tx_bps = _network_throughput(sensors)
    disk_read_bps, disk_write_bps = _disk_throughput(sensors)
    fans = _fans(sensors)
    return {
        "has_sensors": True,
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
        # Busiest NIC, not the first one listed -- see _network_throughput.
        "net_rx_bps": net_rx_bps,
        "net_tx_bps": net_tx_bps,
        # Summed across every disk, unlike network -- see _disk_throughput.
        "disk_read_bps": disk_read_bps,
        "disk_write_bps": disk_write_bps,
        # Per-volume space usage for the Storage cards. A list, not a chartable scalar:
        # it is live state, and how many disks a machine has varies per machine.
        "disks": _disk_volumes(sensors),
        # Cooling. `fans` is the live per-fan list behind the Cooling cards (same shape
        # argument as `disks`); fan_rpm is the one number that can be charted -- see
        # _fastest_fan_rpm for why it is the maximum.
        "fans": fans,
        "fan_rpm": _fastest_fan_rpm(fans),
        # Package power. Reported by nearly every modern CPU and discrete GPU, and it is
        # the metric that explains a temperature chart: a package pulling 140 W in an
        # office PC is a fan/thermal problem waiting to happen, whatever °C it reads now.
        "cpu_power_w": _package_power(sensors, "cpu", ["cpu package", "package", "cpu ppt"]),
        "gpu_power_w": _package_power(sensors, "gpu",
                                      ["gpu package", "gpu power", "board power", "gpu ppt"]),
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
    "disk_read_bps", "disk_write_bps",
    "fan_rpm", "cpu_power_w", "gpu_power_w",
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
    # Separate from collect_disk: used space is a slow-moving capacity number, disk I/O is
    # a per-second rate. An operator who finds the I/O panels noisy should be able to drop
    # them without also losing the "is C: filling up" history.
    "disk_read_bps": "metrics.collect_disk_io",
    "disk_write_bps": "metrics.collect_disk_io",
    "fan_rpm": "metrics.collect_fans",
    # CPU and GPU package power share one toggle: they are the same measurement on two
    # chips, and an operator who doesn't care about watts doesn't care about either.
    "cpu_power_w": "metrics.collect_power",
    "gpu_power_w": "metrics.collect_power",
}

# Friendly metric keys used by the per-machine history endpoint's `metrics` param, mapped
# to their `readings` column. A whitelist -- callers never choose a raw column name, so
# nothing user-supplied is interpolated into SQL.
HISTORY_METRIC_COLUMNS = {
    "temp": "temp",
    "cpu_load": "cpu_load_pct",
    "memory": "memory_load_pct",
    "gpu_temp": "gpu_temp",
    "gpu_load": "gpu_load_pct",
    "disk": "disk_load_pct",
    "net_rx": "net_rx_bps",
    "net_tx": "net_tx_bps",
    "disk_read": "disk_read_bps",
    "disk_write": "disk_write_bps",
    "fan_rpm": "fan_rpm",
    "cpu_power": "cpu_power_w",
    "gpu_power": "gpu_power_w",
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
    """Which history metric keys are currently being collected, so the machine dashboard
    renders a panel only for metrics whose toggle is on. Temperature has no toggle and is
    always on."""
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
# VERSION WATCHER  --  lets agents self-update promptly instead of waiting for
# their own weekly GitHub poll. We periodically check the same source they update
# from, and echo the newest version *that client should be running* back in
# /api/report's response; the agent checks for an update as soon as it sees a
# number ahead of its own.
#
# One train remains: TempMonitorAgent (3.x), the C# service, which self-updates
# from a signed manifest. The Python companion (2.x) that used to share the
# `companion_version` field is gone from the repo, so there is no 2.x release
# left to advertise -- see get_advertised_version for what pre-agent clients get
# instead. The wire field keeps its name: renaming it would break every agent
# already in the field.
# ================================
AGENT_MANIFEST_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/agent.manifest.json"
# The hub reads its own latest version straight out of app.py on main -- same source-of-truth
# and raw-GitHub trust as the client version hints above. Used only by the opt-in self-updater.
HUB_SOURCE_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/hub/app.py"
HUB_UPDATE_CHECK_INTERVAL = 15 * 60  # 15 minutes
AGENT_VERSION_CHECK_INTERVAL = 15 * 60  # 15 minutes
# First version of the C# agent. A client reporting >= this is on the agent train.
AGENT_TRAIN_MIN_VERSION = "3.0.0"
# Last companion release ever published: the one whose migration path installs the
# agent and decommissions itself. Pinned here purely as the documented end of that
# train -- nothing is served from it, since companion.py no longer exists on main.
COMPANION_FINAL_VERSION = "2.10.1"

latest_agent_version = None
latest_version_lock = threading.Lock()

def version_tuple(v):
    """Tolerant version parse: reads the leading dotted-numeric prefix and ignores
    any suffix (e.g. '2.8.0-rc1' -> (2, 8, 0)). Never raises. Mirrors the agent's
    VersionUtil (see agent/src/TempMonitorAgent/Update/VersionUtil.cs)."""
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

def get_latest_agent_version():
    with latest_version_lock:
        return latest_agent_version

def get_advertised_version(reported_version):
    """The version to echo back to a client currently running `reported_version`.

    Agent-train clients (3.x) get the latest agent. Anything below that -- a
    surviving 2.x companion, or a client too old to report a version at all --
    deliberately gets nothing: companion.py is gone from main so there is no 2.x
    release left to serve, and handing one a 3.x number would make it try to
    install an agent build as if it were a Python script. Those machines have to
    be moved over with install.ps1 by hand.

    Returns None when there is nothing useful to say -- a pre-agent client, or a
    manifest we haven't read yet -- in which case /api/report omits latest_version
    entirely and the client falls back to its own poll."""
    if reported_version and cmp_versions(reported_version, AGENT_TRAIN_MIN_VERSION) >= 0:
        return get_latest_agent_version()
    return None

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

def agent_version_watcher():
    while True:
        refresh_latest_agent_version()
        time.sleep(AGENT_VERSION_CHECK_INTERVAL)

agent_version_watcher_thread = None
agent_version_watcher_lock = threading.Lock()

def start_agent_version_watcher():
    global agent_version_watcher_thread
    with agent_version_watcher_lock:
        if agent_version_watcher_thread and agent_version_watcher_thread.is_alive():
            return
        agent_version_watcher_thread = threading.Thread(
            target=agent_version_watcher, daemon=True, name="agent_version_watcher"
        )
        agent_version_watcher_thread.start()

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
# Source archive for the no-git path. codeload serves a branch zip directly.
HUB_ARCHIVE_URL = "https://codeload.github.com/aw08-2004/Temp_Monitor/zip/refs/heads/main"
# Within the repo (and so within that archive), all of the hub's code and assets live under
# this one directory. The self-updater mirrors it wholesale into HUB_CODE_DIR -- there is
# deliberately no per-file allowlist anymore. That list was decided by the *running* version,
# so a release that added a module (users.py/users_web.py in 1.35.0) shipped app.py without
# it and crash-looped on the next boot; the whole hub/ layout exists to kill that failure
# mode by making the archive, not a hand-kept tuple, authoritative about the file set.
HUB_ARCHIVE_SUBDIR = "hub"

def parse_hub_version(text):
    """Pull the HUB_VERSION string out of an app.py source blob, or None. Pure; mirrors
    the version parse in refresh_latest_agent_version()."""
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

def _install_requirements(code_dir):
    """Best-effort dependency install after an update: a release that adds a dependency
    shouldn't crash-loop the restart, so failure here is logged and tolerated. `code_dir`
    is the hub code directory -- requirements.txt lives beside the modules, under hub/."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r",
             os.path.join(code_dir, "requirements.txt"), "--quiet"],
            cwd=code_dir, capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        print(f"[hub-update] pip install after update failed (continuing): {e}")


def _perform_hub_update_git(worktree_root):
    """Bring the checkout at worktree_root up to origin/main via fetch + hard reset. The
    reset covers the whole tree, so the hub/ code dir comes along with it. Returns True only
    if fetch AND reset succeeded -- the caller restarts only then. Discards local drift by
    design (operator-confirmed)."""
    ok, out = _run_git(["fetch", "origin", "main"], worktree_root)
    if not ok:
        print(f"[hub-update] git fetch failed, skipping: {out}")
        return False
    ok, out = _run_git(["reset", "--hard", "origin/main"], worktree_root)
    if not ok:
        print(f"[hub-update] git reset failed, skipping: {out}")
        return False
    print(f"[hub-update] Updated working tree to origin/main: {out}")
    _install_requirements(os.path.join(worktree_root, HUB_ARCHIVE_SUBDIR))
    return True


def _stage_hub_archive(staging):
    """Download and unpack the branch archive into `staging`, returning the path to the
    archive's hub/ directory, or None on any failure (logged, never raises).

    Everything lands in staging and is sanity-checked BEFORE the caller touches the live
    install -- a truncated download or a moved layout upstream must fail the update, not
    leave a hub with half its files. The check is a sanity floor (the entrypoints exist),
    not the old per-file allowlist: the archive's hub/ is authoritative about the rest."""
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
    src_hub = os.path.join(staging, roots[0], HUB_ARCHIVE_SUBDIR)

    essentials = ("app.py", "wsgi.py", "requirements.txt")
    missing = [n for n in essentials if not os.path.isfile(os.path.join(src_hub, n))]
    if not os.path.isdir(src_hub) or missing:
        print(f"[hub-update] archive {HUB_ARCHIVE_SUBDIR}/ missing "
              f"{', '.join(missing) or HUB_ARCHIVE_SUBDIR + '/'} -- refusing to update")
        return None
    return src_hub


def _perform_hub_update_archive(code_dir):
    """Mirror the archive's hub/ directory into the live code dir. Used when there's no git
    checkout to reset (the layout install.ps1 produces). Files are overwritten in place and
    subdirectories are mirrored; entries the archive dropped are pruned, so a deleted module
    can't linger and shadow. Operator state (.env, logs/, the service wrapper) lives one
    level up in STATE_ROOT and is never in code_dir, so the mirror can't touch it."""
    staging = tempfile.mkdtemp(prefix="hub-update-")
    try:
        src_hub = _stage_hub_archive(staging)
        if src_hub is None:
            return False

        wanted = set(os.listdir(src_hub))
        for name in wanted:
            src = os.path.join(src_hub, name)
            dst = os.path.join(code_dir, name)
            if os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        # Prune what upstream removed. Guarded to a dedicated code dir that is NOT the state
        # root, so a misconfigured flat install can never have its .env/logs/db pruned.
        if os.path.abspath(code_dir) != os.path.abspath(STATE_ROOT):
            for name in os.listdir(code_dir):
                if name in wanted or name == "__pycache__":
                    continue
                victim = os.path.join(code_dir, name)
                if os.path.isdir(victim):
                    shutil.rmtree(victim, ignore_errors=True)
                else:
                    os.remove(victim)

        print(f"[hub-update] Mirrored {HUB_ARCHIVE_SUBDIR}/ into {code_dir} from {HUB_ARCHIVE_URL}")
        _install_requirements(code_dir)
        return True
    except Exception as e:
        # A failure part-way through the mirror leaves the tree inconsistent, so say so
        # loudly -- but still don't restart, since restarting is what would turn a broken
        # tree into a crash-loop.
        print(f"[hub-update] update failed while mirroring {HUB_ARCHIVE_SUBDIR}/ ({e}); hub left as-is")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def perform_hub_update(code_dir):
    """Update the hub in place. Returns True only if the caller should now restart.

    Prefers git when the install is a checkout: a developer running from a clone should not
    have their working tree overwritten by an archive. The .git that decides this lives at
    the worktree root, one level up from the code dir (the hub/ subdirectory)."""
    worktree_root = os.path.dirname(os.path.abspath(code_dir))
    if os.path.isdir(os.path.join(worktree_root, ".git")):
        return _perform_hub_update_git(worktree_root)
    return _perform_hub_update_archive(code_dir)

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
                    if perform_hub_update(HUB_CODE_DIR):
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
# AUTH CONFIG (single sign-on)
# ================================
# Two interchangeable OpenID Connect providers, either or both of which may be configured:
#
#   * GOOGLE_*  -- Google Workspace, the original and still the common case.
#   * OIDC_*    -- any other OIDC issuer (Microsoft Entra ID, Okta, Authentik, Keycloak,
#                  Auth0...). Discovery does the work, so there is no per-vendor code here
#                  and adding a provider is configuration, not a release.
#
# Both land in the SAME place: an email, checked against the same permission groups and the
# same break-glass list. The identity provider decides who you are; permissions.py decides
# what that gets you. Nothing downstream knows or cares which button was pressed.
#
# ⚠️ If you configure both, they are both doors to the same rooms. An operator's access is
# whatever the WEAKER issuer will assert about their email address -- so do not enable a
# second issuer that lets users self-assert an email you have granted access to.
#
# These are no longer read once at import: the break-glass admins can edit them from
# Settings -> Sign-in, and configure_oauth() below rebinds every name here and re-registers
# the Authlib clients when they do. The module-level names remain the live values (routes
# read them at call time), so nothing downstream had to change -- but treat them as
# variables, not constants, and never capture them in a default argument or a closure.
AUTH_CONFIG = authconfig.load(os.environ)

GOOGLE_CLIENT_ID = AUTH_CONFIG["google_client_id"]
GOOGLE_CLIENT_SECRET = AUTH_CONFIG["google_client_secret"]

OIDC_CLIENT_ID = AUTH_CONFIG["oidc_client_id"]
OIDC_CLIENT_SECRET = AUTH_CONFIG["oidc_client_secret"]
# Either the issuer (the well-known path is appended) or a full discovery document URL, so
# both of the forms an admin is likely to have on hand work. authconfig.load resolves it.
OIDC_ISSUER = AUTH_CONFIG["oidc_issuer"]
OIDC_METADATA_URL = AUTH_CONFIG["oidc_metadata_url"]
# What the button says. Worth setting -- "Sign in with Microsoft" is a much clearer prompt
# than "Sign in with SSO" when someone is looking at an unfamiliar login page.
OIDC_DISPLAY_NAME = AUTH_CONFIG["oidc_display_name"]
OIDC_SCOPES = AUTH_CONFIG["oidc_scopes"]

GOOGLE_ENABLED = authconfig.google_enabled(AUTH_CONFIG)
OIDC_ENABLED = authconfig.oidc_enabled(AUTH_CONFIG)

# How many of a session's claimed directory groups the Permission Groups page will offer
# back to an admin as a "your own sign-in carried these" hint (roadmap #4). Display only;
# see the session write in _complete_login. Small because it rides in the session cookie
# and its only job is to let someone recognise and copy their own group's identifier.
DIRECTORY_GROUPS_SHOWN = 25

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")

# How long a signed-in session lasts, in days. ROLLING: every request pushes the expiry
# back, so somebody using the console daily is never signed out, while a browser left
# untouched for longer than this has to sign in again.
#
# This is a real security control, not a convenience knob: a console session can run
# arbitrary code as SYSTEM on any enrolled machine, so the cookie IS the perimeter (see
# ALLOWED_EMAILS below). A week is the default because it covers a normal working pattern
# without leaving an unattended browser as an indefinite foothold. Shorten it if operators
# sign in from machines they don't control.
try:
    SESSION_LIFETIME_DAYS = max(1, int(os.environ.get("SESSION_LIFETIME_DAYS", "7")))
except ValueError:
    SESSION_LIFETIME_DAYS = 7
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

if not FLASK_SECRET_KEY:
    raise RuntimeError(
        "FLASK_SECRET_KEY must be set (as an env var, or in a .env file) to run the hub "
        "-- see README."
    )
# At least one way in. Refusing to boot with none is deliberate: a hub that started with no
# configured provider would serve a login page with no buttons, which looks like a bug in
# the hub rather than a missing setting.
if not (GOOGLE_ENABLED or OIDC_ENABLED):
    raise RuntimeError(
        "No sign-in provider is configured. Set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET, "
        "and/or OIDC_CLIENT_ID + OIDC_CLIENT_SECRET + OIDC_ISSUER -- see README."
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
#   HttpOnly -- Flask's default, pinned here so it is visible next to the others. No
#     script needs to read this cookie, and one that could would be reading the perimeter.
#   PERMANENT_SESSION_LIFETIME + SESSION_REFRESH_EACH_REQUEST -- the session survives
#     closing the browser and expires SESSION_LIFETIME_DAYS after last use, rather than
#     when the browser closes. Flask's default is a browser-session cookie, which meant
#     signing in again every morning; that is not more secure in any way that matters
#     (it survives sleep/restore and tab restore anyway) and it trained people to click
#     through the sign-in prompt without reading it. Rolling, so daily users stay in and
#     an abandoned browser still ages out. The expiry is inside the SIGNED cookie value,
#     so an operator cannot extend their own session by editing it.
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=HUB_URL.startswith("https://"),
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_LIFETIME_DAYS),
    SESSION_REFRESH_EACH_REQUEST=True,
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
#
# THIS CONFIG COSTS ONE WAITRESS THREAD PER OPEN TAB. polling + threading means engineio
# serves a poll by blocking a worker on a queue until a packet arrives or the ping cycle
# expires (~25s), and index.html/machine.html both open a socket -- so the serving thread
# pool has to be sized by concurrent operator tabs, not by request rate. waitress's default
# of 4 starves the hub outright; install.ps1 passes --threads explicitly and explains why.
# Don't drop the transport pin to "fix" this: waitress has no WebSocket support, so
# allowing upgrades here just breaks the socket.
socketio = SocketIO(
    app,
    cors_allowed_origins=[HUB_URL.rstrip("/")],
    async_mode="threading",
    transports=["polling"],
    allow_upgrades=False
)

oauth = None


def configure_oauth(config=None, announce=True):
    """(Re)build the Authlib clients from `config`, and rebind the module-level provider
    names to match. Called once at import, and again whenever an admin saves a change on
    Settings -> Sign-in.

    A FRESH OAuth object each time rather than mutating the old one's registry: re-binding
    one name is a poke at Authlib internals (`_registry` and a separate `_clients` cache,
    either of which going stale would leave the hub using the previous issuer while the
    console reported the new one), whereas building a new instance uses only the public
    API. The old object is simply dropped.

    Raises whatever Authlib raises on a bad registration. The caller is expected to have
    kept the previous configuration so it can put it back -- see auth_web.py. This function
    does NOT validate; authconfig.validate does, and must have run first.
    """
    global oauth, AUTH_CONFIG
    global GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_ENABLED
    global OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_ISSUER, OIDC_METADATA_URL
    global OIDC_DISPLAY_NAME, OIDC_SCOPES, OIDC_ENABLED

    config = config or authconfig.load(os.environ)
    new_oauth = OAuth(app)
    if authconfig.google_enabled(config):
        new_oauth.register(
            name="google",
            client_id=config["google_client_id"],
            client_secret=config["google_client_secret"],
            server_metadata_url=authconfig.GOOGLE_METADATA_URL,
            client_kwargs={"scope": authconfig.GOOGLE_SCOPES},
        )
    if authconfig.oidc_enabled(config):
        # No vendor-specific code: discovery supplies the endpoints and the signing keys,
        # which is what makes "add Entra" or "add Okta" configuration, not a release.
        new_oauth.register(
            name="oidc",
            client_id=config["oidc_client_id"],
            client_secret=config["oidc_client_secret"],
            server_metadata_url=config["oidc_metadata_url"],
            client_kwargs={"scope": config["oidc_scopes"]},
        )

    # Rebind only after every registration has succeeded, so a failure part-way through
    # leaves the hub signing people in with the configuration it had a moment ago.
    oauth = new_oauth
    AUTH_CONFIG = config
    GOOGLE_CLIENT_ID = config["google_client_id"]
    GOOGLE_CLIENT_SECRET = config["google_client_secret"]
    GOOGLE_ENABLED = authconfig.google_enabled(config)
    OIDC_CLIENT_ID = config["oidc_client_id"]
    OIDC_CLIENT_SECRET = config["oidc_client_secret"]
    OIDC_ISSUER = config["oidc_issuer"]
    OIDC_METADATA_URL = config["oidc_metadata_url"]
    OIDC_DISPLAY_NAME = config["oidc_display_name"]
    OIDC_SCOPES = config["oidc_scopes"]
    OIDC_ENABLED = authconfig.oidc_enabled(config)

    if announce:
        print("[auth] Sign-in providers: " + (", ".join(
            (["Google"] if GOOGLE_ENABLED else []) +
            ([f"{OIDC_DISPLAY_NAME} ({OIDC_METADATA_URL})"] if OIDC_ENABLED else []))
            or "none"))
    return config


configure_oauth(AUTH_CONFIG)
print(f"[auth] Sessions last {SESSION_LIFETIME_DAYS} day(s), rolling.")


# ================================
# CSRF
# ================================
# A console session can run arbitrary code as SYSTEM on any enrolled machine, so a CSRF
# against a signed-in operator is fleet-wide RCE. Two controls carry that, and this is the
# second one (the first is SESSION_COOKIE_SAMESITE="Lax", pinned above).
#
# Every blueprint's docstring says the JSON content type is what stops a cross-site POST:
# it is not CORS-safelisted, so a cross-origin fetch preflights and fails, and an HTML form
# -- the one cross-site POST needing no preflight -- cannot produce it. That reasoning is
# right, but until this was enforced here it was only INCIDENTALLY true. Bodies are read
# with request.get_json(silent=True), which returns None rather than refusing when the
# content type is wrong, so the requirement held only for views that went on to fail over a
# missing field. Around fifteen state-changing endpoints read no body at all -- every
# /cancel, /retry, /refresh, /sync and /run route, plus the backup-key routes whose
# `request.get_json(silent=True)` line was a no-op with a comment claiming otherwise -- and
# for those the documented control did not exist.
#
# Enforced in login_required rather than a global before_request because that is exactly
# the set of requests it applies to: CSRF rides an AMBIENT credential, and the session
# cookie is the only ambient credential here. The agent-facing /api/agent/* endpoints
# authenticate with a bearer token that no browser attaches on its own, so they are not
# CSRF-able and correctly do not pass through this.
#
# POST is the only method checked, and that is the whole rule rather than an oversight: a
# cross-site HTML form is the one request that reaches us without a preflight, and a form
# can only issue GET or POST. A cross-origin PUT, PATCH or DELETE has to go through
# fetch/XHR, which preflights and fails here (no ACAO on these routes), so requiring a
# content type on those would break working callers to defend against a request no browser
# will send.
CSRF_CHECKED_METHODS = frozenset({"POST"})
# The two console endpoints that legitimately post something other than JSON: a file.
# multipart/form-data IS form-producible, so these stay reachable cross-site -- but both
# are deliberately inert (they store bytes and return a digest, creating no package, no
# payload record and no deployment), and the JSON call that would give those bytes meaning
# is covered. Named explicitly rather than allowing multipart everywhere, so a future
# endpoint does not inherit the exemption by accident.
CSRF_UPLOAD_ENDPOINTS = frozenset({
    "packages.upload_package_file",
    "bios.upload_firmware_image",
})


def _csrf_content_type_ok():
    """May this state-changing, cookie-authenticated request proceed?"""
    if request.method not in CSRF_CHECKED_METHODS:
        return True
    if request.endpoint in CSRF_UPLOAD_ENDPOINTS:
        return True
    # mimetype, not the raw header: it strips any charset parameter, so
    # "application/json; charset=utf-8" is accepted as the same thing.
    return request.mimetype == "application/json"


def _device_identity():
    """Resolve a device token from this request's Authorization header, or None.

    The native client (roadmap #11) has no cookie and cannot get one -- sign-in is
    OAuth-only, so there is nothing for an app to type. It presents
    `Bearer tmu_<token_id>:<secret>` instead, minted by the pairing flow in
    apitokens_web.py.

    Re-checked on EVERY request rather than trusted for the life of the token:
    `login_allowed` is the same gate the sign-in path runs, so an operator removed from
    every permission group loses their paired devices the moment the group changes, not
    whenever the token happens to expire. (What the device may then DO is narrowed a
    second time by permissions_web._narrow_to_device.)
    """
    identity = apitokens.authenticate(DB_PATH, request.headers.get("Authorization"))
    if identity is None:
        return None
    if not access.login_allowed(identity["email"], identity.get("directory_groups") or ()):
        return None
    return identity


def login_required(view):
    """Gate a route behind an authenticated + allow-listed caller, and require a JSON
    content type on anything that changes state with an AMBIENT credential (see the CSRF
    note above). Never applied to /api/report.

    Two ways in, one gate: the signed session cookie a browser carries, and a device
    token a native client presents. They differ in exactly one respect below -- see the
    CSRF comment inside.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        identity = _device_identity()
        if identity is not None:
            # NOT subject to the content-type rule, and this is the rule being applied
            # rather than an exception to it: the note above says CSRF rides an ambient
            # credential, and a bearer header is not ambient -- no browser attaches one on
            # its own. It is the same reasoning that (correctly) leaves /api/agent/*
            # outside this check. Requiring JSON here would defend against a request that
            # cannot be made, at the cost of breaking ordinary API callers.
            permissions_web.set_request_identity(identity)
            return view(*args, **kwargs)

        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login"))
        if not _csrf_content_type_ok():
            return jsonify({"error": "This endpoint requires Content-Type: "
                                     "application/json."}), 415
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
# Registered-users directory (roadmap #8). Gated by manage_users, kept separate from
# permission-group administration so a profile edit isn't the same trust as a grant.
app.register_blueprint(create_users_blueprint(DB_PATH, login_required, access))
# The Audit Log tab -- the read path over the trail every other blueprint writes to.
# view_audit_log is the perimeter; view_security_audit widens which LEVELS come back, and
# that widening is applied in SQL, never in the page (see audit_web.py).
app.register_blueprint(create_audit_blueprint(DB_PATH, login_required, access))
# Package definitions, deployments, and the agent-facing payload download. LOG_DIR is
# handed in because the blob store lives beside the database (see packages.blob_root),
# and HUB_URL because the agent's download URL has to be absolute.
app.register_blueprint(create_packages_blueprint(
    DB_PATH, LOG_DIR, login_required, access, hub_url=HUB_URL
))
# Backup destinations, the encryption key, and the hub-database backup itself. LOG_DIR
# holds the encrypted credential store and the scratch space a snapshot is built in;
# ENV_PATH is where the master key is written, and must be the same file load_dotenv read.
# HUB_URL is needed for the same reason packages does: a WebDAV restore hands the agent an
# absolute URL back to the hub, since WebDAV has no pre-signed download of its own.
app.register_blueprint(create_backups_blueprint(
    DB_PATH, LOG_DIR, ENV_PATH, login_required, access, hub_version=HUB_VERSION,
    hub_url=HUB_URL,
    # Wrapped in a lambda, not passed directly: backup_machine_roster is defined further
    # down this file, so naming it here would be a NameError at import. The lambda defers
    # the lookup to request time, when it exists.
    machine_roster=lambda: backup_machine_roster(),
))
# Remote view/control (roadmap #2): agent-facing WebRTC signaling (bearer token) + console-
# facing session control (remote_control capability + machine scope). Same login_required and
# access seam as every other blueprint.
app.register_blueprint(create_remote_blueprint(DB_PATH, login_required, access, env_path=ENV_PATH))

# Active Directory sync (roadmap #4): status, the OU list the group scope picker uses,
# and a synchronous "sync now". Same login_required + access seam as everything above.
app.register_blueprint(create_directory_blueprint(DB_PATH, login_required, access))

# BIOS/firmware inventory and writes (roadmap #9): what a machine's firmware is set to, a
# "re-read it now" command, and the `set_bios_settings` write half behind its own
# `manage_firmware` capability. LOG_DIR is passed because the BIOS setup password lives in
# the same master-key-wrapped secret file the backup destinations use -- never in `settings`,
# which is rendered into a form and partly shipped to agents.
# HUB_URL rides along for the same reason packages needs it: an agent is handed the URL
# it downloads a BIOS image from, and that URL has to be the hub's public address rather
# than whatever Host header reached it.
app.register_blueprint(create_bios_blueprint(DB_PATH, LOG_DIR, login_required, access,
                                             hub_url=HUB_URL))

# Wake-on-LAN (roadmap #10): a machine's NIC inventory and wakeability diagnosis behind
# `view`, and waking/preparing behind `issue_commands` -- no new capability, because waking
# a PC is strictly less dangerous than the `shutdown` that gate already covers.
# The roster is the same one the backup and firmware schedulers use, wrapped in a lambda for
# the same reason: backup_machine_roster is defined further down this file. It is what makes
# "online" mean one thing across the hub, and its `last_seen` is what lets a wake be
# confirmed against the moment its packet went out rather than against mere online-ness.
app.register_blueprint(create_wake_blueprint(
    DB_PATH, login_required, access, machine_roster=lambda: backup_machine_roster()))

# The machine Processes card: reading the live process list behind `view` (it is inventory,
# like the sensor tree the same page already shows in full), and ending or restarting a
# process behind `issue_commands` -- no new capability, because this is strictly less
# dangerous than the `shutdown` and the SYSTEM shell that gate already covers.
app.register_blueprint(create_processes_blueprint(DB_PATH, login_required, access))

# Sign-in provider configuration. Gated on ALLOWED_EMAILS membership rather than any
# capability -- see auth_web.py for why this one is not delegable via manage_settings.
app.register_blueprint(create_auth_blueprint(
    DB_PATH, login_required, access, ENV_PATH, configure_oauth))

# Device pairing and the Download Client page (roadmap #11). The pairing routes sit behind
# the ORDINARY session gate -- pairing a device is something a signed-in operator does in a
# browser, and the token it mints is what the app uses afterwards. HUB_CODE_DIR is handed in
# because the signed client manifest ships beside the code, like the agent's does.
app.register_blueprint(create_apitokens_blueprint(
    DB_PATH, login_required, access, code_dir=HUB_CODE_DIR))


@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template(
        "login.html",
        google_enabled=GOOGLE_ENABLED,
        oidc_enabled=OIDC_ENABLED,
        oidc_display_name=OIDC_DISPLAY_NAME,
    )


def _complete_login(user_info, provider):
    """Everything after an identity provider has vouched for someone. Deliberately shared
    by every provider: the authorization decision, the audit identity and the users
    directory must not be able to differ depending on which button was pressed."""
    email = permissions.email_from_claims(user_info)
    if not email:
        return (f"{provider} did not provide an email address for this account, so it "
                f"cannot be matched to a permission group."), 403

    # `email_verified` absent is NOT the same as false. Google always sends it; plenty of
    # issuers (Entra among them) never do, and refusing those would rule out exactly the
    # providers this feature was added for. Present-and-false is a refusal, though: that
    # is an issuer telling us it does not stand behind the address.
    if user_info.get("email_verified") is False:
        return f"{provider} reports this account's email address is unverified.", 403

    # Directory groups the issuer asserted (roadmap #4), narrowed to the ones some
    # permission group actually maps. Only the intersection goes in the session: a user
    # in 200 Entra groups would otherwise carry ~7 KB of GUIDs in a 4 KB signed cookie,
    # breaking sign-in for precisely the most-privileged accounts, and a claimed group
    # nothing maps can never affect authorization anyway.
    claimed_groups = permissions.directory_groups_from_claims(user_info)
    directory_groups = access.mapped_directory_groups(claimed_groups)

    # Break-glass superuser, a member of a permission group by email, or a member of a
    # mapped directory group. A valid account that is none of those is refused outright
    # rather than admitted to an empty dashboard -- see Access.login_allowed for why.
    if not access.login_allowed(email, directory_groups):
        # An issuer that withheld the group list because the user is in too many of them
        # produces a refusal identical to "this user is in no mapped group", and only one
        # of those is a configuration error. Say which, or an admin debugs a correct
        # mapping that appears to do nothing.
        if permissions.has_group_claim_overage(user_info):
            print(f"[auth] {provider} withheld the group claim for {email} (too many "
                  f"groups -- the issuer sent _claim_names instead). This hub cannot "
                  f"resolve that, so no directory mapping could be applied.")
            return (f"Access denied: {provider} did not send this account's group "
                    f"membership because the account is in too many groups, so its "
                    f"directory-group mapping could not be applied. Grant access by "
                    f"email address instead, or reduce the account's group count."), 403
        return f"Access denied: {email} is not authorized for this dashboard.", 403

    # Sessions outlive the browser (see PERMANENT_SESSION_LIFETIME). `permanent` is what
    # opts this session into that lifetime -- without it Flask issues a cookie that dies
    # when the browser closes, no matter what the lifetime says.
    session.permanent = True
    session["user"] = {
        "email": email,
        "name": user_info.get("name") or email,
        "picture": user_info.get("picture"),
        "provider": provider,
        # Authorization input, not decoration: every request re-resolves this session's
        # permissions from it. It is fixed at sign-in because that is the only moment the
        # hub hears from the directory -- a membership revoked in Entra takes effect here
        # when the session ends, not the instant it is revoked.
        "directory_groups": directory_groups,
        # DISPLAY ONLY -- never read by effective_permissions, and deliberately a
        # different key so that stays true. The Permission Groups page shows an admin the
        # tokens their own sign-in carried, which is the only practical way to discover
        # what shape this tenant emits (Entra sends bare GUIDs) without a trip to the
        # provider's portal. Truncated because the authorization list above is bounded by
        # the hub's own mappings while this one is bounded only by how many groups the
        # user is in, and the session is a ~4 KB signed cookie.
        "directory_groups_seen": claimed_groups[:DIRECTORY_GROUPS_SHOWN],
    }
    # Auto-register / stamp the last login in the users directory (roadmap #8). Never
    # let a directory write break sign-in: the session is already established above, so
    # a failure here must not turn a valid login into an error page.
    try:
        users.upsert_from_login(DB_PATH, email, user_info.get("name"))
    except Exception as e:
        print(f"[users] upsert_from_login failed for {email}: {e}")
    return redirect(url_for("index"))


def _callback_url(endpoint):
    # Anchor the callback to HUB_URL rather than url_for(_external=True): behind a TLS
    # terminator (nginx/Cloudflare) the request can reach waitress as plain http, so the
    # _external form emits http://.../auth/callback -- which the provider rejects as a
    # redirect_uri mismatch. HUB_URL is the authoritative public origin (https://...).
    return HUB_URL.rstrip("/") + url_for(endpoint)


@app.route("/login/google")
def login_google():
    if not GOOGLE_ENABLED:
        return "Google sign-in is not configured on this hub.", 404
    return oauth.google.authorize_redirect(_callback_url("auth_callback"))


@app.route("/auth/callback")
def auth_callback():
    if not GOOGLE_ENABLED:
        return "Google sign-in is not configured on this hub.", 404
    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo") or oauth.google.userinfo(token=token)
    return _complete_login(user_info, "Google")


@app.route("/login/oidc")
def login_oidc():
    if not OIDC_ENABLED:
        return "SSO is not configured on this hub.", 404
    return oauth.oidc.authorize_redirect(_callback_url("auth_oidc_callback"))


@app.route("/auth/oidc/callback")
def auth_oidc_callback():
    if not OIDC_ENABLED:
        return "SSO is not configured on this hub.", 404
    token = oauth.oidc.authorize_access_token()
    # `userinfo` is parsed from the ID token by Authlib when the issuer returns one. Some
    # issuers put a thinner set of claims there than at the userinfo endpoint (Entra omits
    # `email` from the ID token in several tenant configurations), so fall back to asking
    # rather than refusing a sign-in over a missing claim we could simply go and fetch.
    user_info = token.get("userinfo") or {}
    if not permissions.email_from_claims(user_info):
        try:
            fetched = oauth.oidc.userinfo(token=token)
            if fetched:
                user_info = {**fetched, **{k: v for k, v in user_info.items() if v}}
        except Exception as e:
            print(f"[auth] userinfo lookup failed for the OIDC provider: {e}")
    return _complete_login(user_info, OIDC_DISPLAY_NAME)


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
        # Service Tag -- a second BIOS/chassis identifier alongside serial_number, added
        # for the Inventory search/sort work (roadmap #6). Reported by the agent the same
        # way asset_tag/serial_number are; old rows read back NULL.
        if "service_tag" not in existing_columns:
            conn.execute("ALTER TABLE machine_info ADD COLUMN service_tag TEXT")
        # System manufacturer (Win32_ComputerSystem.Manufacturer), on the service_tag
        # precedent above. Not an identifier -- it is the branch every BIOS/firmware code
        # path takes first (roadmap #9), since Dell, HP and Lenovo share no management
        # API. Collected by both agents; null on an older one, which reads as "we do not
        # know what this is yet" rather than "unsupported".
        if "manufacturer" not in existing_columns:
            conn.execute("ALTER TABLE machine_info ADD COLUMN manufacturer TEXT")

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
    # A reading may carry the client's own timestamp (client_ts) -- e.g. a
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

    # The blob rides at most one reading every SENSOR_BLOB_MIN_SECONDS -- see
    # _should_store_sensor_blob for why, and why the charts do not notice. The METRICS below
    # are extracted from every report regardless, so a machine reporting at 1 Hz because
    # somebody is watching it stores 1 Hz history.
    sensors_json = (json.dumps(sensors)
                    if sensors and _should_store_sensor_blob(machine_name, timestamp_epoch)
                    else None)
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
    # (an older client, or a second stale instance double-reporting for the same
    # machine) doesn't blank out CPU/GPU Load & Clock in the UI every other update.
    # set_latest_sensors() above only overwrites the cache when sensors are present.
    payload = {
        'machine': machine_name,
        'timestamp': timestamp_str,
        'timestamp_epoch': timestamp_epoch,
        'temp': temp_value,
        'threshold': settings.get_int(DB_PATH, "hub.high_temp_threshold"),
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

def save_machine_info(machine, asset_tag, serial_number, model, companion_version=None,
                      service_tag=None, manufacturer=None):
    machine_name = str(machine).strip()
    asset_tag = (str(asset_tag).strip() or None) if asset_tag else None
    serial_number = (str(serial_number).strip() or None) if serial_number else None
    model = (str(model).strip() or None) if model else None
    companion_version = (str(companion_version).strip() or None) if companion_version else None
    service_tag = (str(service_tag).strip() or None) if service_tag else None
    manufacturer = (str(manufacturer).strip() or None) if manufacturer else None
    if not machine_name or not any([asset_tag, serial_number, model, companion_version,
                                    service_tag, manufacturer]):
        return

    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO machine_info(machine, asset_tag, serial_number, model, companion_version, service_tag, manufacturer, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine) DO UPDATE SET
                asset_tag = COALESCE(excluded.asset_tag, machine_info.asset_tag),
                -- WRITE-ONCE, unlike every other field here, and the argument order is the
                -- whole difference: the EXISTING value wins. A BIOS serial is immutable
                -- hardware identity -- that is the entire premise of keying dedup on it --
                -- and this function's only caller is /api/report, which is unauthenticated
                -- by design. Last-write-wins let anyone who could reach the hub retype a
                -- real machine's serial to collide with another's and drive
                -- resolve_serial_group into merging them, which destroys an identity row
                -- and re-points its permission-group scope. Filling a blank is still
                -- allowed: that is the first report of a machine describing itself, not an
                -- overwrite of something already established.
                serial_number = COALESCE(machine_info.serial_number, excluded.serial_number),
                model = COALESCE(excluded.model, machine_info.model),
                companion_version = COALESCE(excluded.companion_version, machine_info.companion_version),
                service_tag = COALESCE(excluded.service_tag, machine_info.service_tag),
                manufacturer = COALESCE(excluded.manufacturer, machine_info.manufacturer),
                updated_at = excluded.updated_at
            """,
            (machine_name, asset_tag, serial_number, model, companion_version, service_tag,
             manufacturer, to_timestamp_str(datetime.now())),
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
    with _last_sensor_blob_lock:
        _last_sensor_blob_epoch.pop(machine_name, None)


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
        #
        # OR IGNORE, then delete the leftovers, because readings carries a UNIQUE index on
        # (ts_epoch, machine, temp). The two names ARE one physical machine, so a second in
        # which both reported the same temperature is not a freak coincidence -- it is the
        # normal shape of the rename/re-enroll window that creates duplicates in the first
        # place. A bare UPDATE raises IntegrityError there and aborts the whole merge
        # mid-transaction, so the merge that most needs to work is the one that fails.
        #
        # A skipped row is by definition redundant: same second, same temperature as a row
        # the survivor already has. Dropping it loses nothing but a duplicate.
        conn.execute("UPDATE OR IGNORE readings SET machine = ? WHERE machine = ?",
                     (survivor, dropped))
        conn.execute("DELETE FROM readings WHERE machine = ?", (dropped,))
        d = conn.execute(
            "SELECT asset_tag, serial_number, service_tag, manufacturer, model, companion_version "
            "FROM machine_info WHERE machine = ?",
            (dropped,),
        ).fetchone()
        if d is not None:
            conn.execute(
                """
                UPDATE machine_info SET
                    asset_tag = COALESCE(asset_tag, ?),
                    serial_number = COALESCE(serial_number, ?),
                    service_tag = COALESCE(service_tag, ?),
                    manufacturer = COALESCE(manufacturer, ?),
                    model = COALESCE(model, ?),
                    companion_version = COALESCE(companion_version, ?)
                WHERE machine = ?
                """,
                (d["asset_tag"], d["serial_number"], d["service_tag"], d["manufacturer"],
                 d["model"], d["companion_version"], survivor),
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
    # And its backup configuration + run history, so a merged machine keeps backing up
    # under the surviving name instead of silently dropping off the schedule. Existing
    # archives stay under the old name's folder and key -- the envelope header records
    # which machine it was sealed for, so they remain restorable.
    backups.rename_machine(DB_PATH, dropped, survivor)
    # Firmware inventory follows the hostname too. The survivor has usually reported its own
    # (same hardware, so the same attributes), which is why bios.rename_machine keeps the
    # survivor's row rather than overwriting it with the dropped name's older reading.
    bios.rename_machine(DB_PATH, dropped, survivor)
    # Firmware update targets follow too, and the survivor's own row wins a collision --
    # both rows describe one physical machine, and it only needs flashing once.
    firmware.rename_machine(DB_PATH, dropped, survivor)
    # Network adapters and wake history follow (roadmap #10). Doing nothing here would be
    # worse than a stale display: a NIC row under the merged-away hostname keeps offering a
    # machine that no longer exists as the RELAY for its subnet, so every wake routed
    # through it would be queued at a name nothing answers to.
    wake.rename_machine(DB_PATH, dropped, survivor)
    # Processes are dropped rather than renamed: unlike an adapter list or a firmware
    # inventory this is a live sample that the survivor's own agent replaces within seconds
    # of anyone looking, so carrying the merged-away name's copy across would only put a
    # stale list under a hostname that is about to report its own.
    processes.forget_machine(DB_PATH, dropped)
    _evict_live_status(dropped)
    fleet.audit(DB_PATH, actor, "machine.merge", dropped, {"survivor": survivor},
                level=fleet.LEVEL_NOTICE)


def resolve_serial_group(serial, actor="system:dedup"):
    """Collapse duplicate machine_info rows that share `serial`, preferring live records:
      - exactly one online  -> merge the offline duplicate(s) into it
      - all offline         -> merge into the most recently updated row
      - two or more online   -> leave them separate (a genuine conflict)
      - survivor not enrolled -> leave them separate (an unverified identity claim)
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

    # The survivor must be a machine the hub has actually ENROLLED, not merely one that
    # has posted a report. This is what stops the whole dedup from being a weapon: the
    # trigger for it arrives on /api/report, which is unauthenticated by design, so
    # without this check anyone who can reach the hub can invent a hostname, claim a real
    # machine's serial while that machine is offline, and have the merge hand them its
    # identity -- deleting its agent enrollment (fleet.delete_machine) and re-pointing
    # every permission group, deployment, backup and firmware row scoped to it onto the
    # name they chose (permissions.rename_machine and friends below).
    #
    # An attacker cannot enroll without AGENT_ENROLLMENT_SECRET, so requiring it here
    # costs the legitimate rename case nothing: the box that renamed itself is the box
    # that is already enrolled. Refused collisions become the operator-facing alert rather
    # than silence, because an unexplained duplicate is exactly what this would look like.
    if not fleet.is_enrolled(DB_PATH, survivor):
        alerts.upsert_duplicate(DB_PATH, serial, [r["machine"] for r in rows])
        fleet.audit(DB_PATH, actor, "machine.merge_refused", survivor,
                    {"serial": str(serial).strip(),
                     "machines": [r["machine"] for r in rows],
                     "reason": "the surviving hostname has no agent enrollment"},
                    level=fleet.LEVEL_SECURITY)
        return [r["machine"] for r in rows]

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
            # Lapsed process watches. Not a retention question -- is_watched already tests
            # the expiry, so a stale row is never believed -- just housekeeping, so that a
            # table which gains a row per machine anyone ever opened the card on does not
            # keep them all forever. Its own try for the same reason as the two above.
            try:
                processes.prune_watches(DB_PATH)
            except Exception as e:
                print(f"[retention] Process-watch prune failed: {e}")
            # And the live-chart watches, which are the same kind of row for the same kind
            # of reason (see live.py). Its own try, as above.
            try:
                live.prune_watches(DB_PATH)
            except Exception as e:
                print(f"[retention] Live-watch prune failed: {e}")
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
        # Switch on the PCs this window is waiting for (roadmap #10). Its own try block:
        # opting into auto-wake must never be able to stop deployments advancing, and a
        # wake that cannot find a relay is a normal outcome rather than an error here.
        try:
            roster = backup_machine_roster()
            woke = wake_pending_targets(
                packages.pending_target_machines(DB_PATH),
                {e["machine"] for e in roster if e["online"]},
                reason="deployment window")
            if woke:
                print(f"[deploy] Requested a wake for {woke} offline target(s).")
        except Exception as e:
            print(f"[deploy] Target wake pass failed: {e}")
        time.sleep(interval)


def start_deploy_scheduler():
    threading.Thread(target=deploy_scheduler, daemon=True, name="deploy_scheduler").start()


def firmware_scheduler():
    """Advance firmware updates: retire what nobody will answer for, dispatch what is due.

    Its own thread rather than a pass inside deploy_scheduler, because the two disagree
    about time: a deploy tick is 30 seconds and its slowest step is a command result, while
    a flash is confirmed by a machine coming back from a reboot and is given a day. Sharing
    a loop would mean one of the two intervals is wrong.

    Dispatch is limited to machines that are ONLINE. A flash queued at a dark PC would burn
    its command TTL and then be retired as failed -- and this is the one feature with no
    retry, so a machine that was merely switched off would be recorded as a failure nobody
    would dare re-run. Left pending, it is still due the minute it reappears; that is the
    file-backup catch-up discipline, and it matters more here.
    """
    while True:
        interval = settings.get_int(DB_PATH, "firmware.scheduler_interval_seconds")
        try:
            # The same roster the per-PC backup scheduler uses, so "online" means one
            # thing across the hub -- last contact from a non-revoked agent, within
            # fleet.offline_after_seconds -- rather than this scheduler growing a second,
            # subtly different definition.
            online = {entry["machine"] for entry in backup_machine_roster()
                      if entry["online"]}
            expired, dispatched = firmware.tick(
                DB_PATH,
                ttl_seconds=settings.get_int(DB_PATH, "fleet.command_ttl_seconds"),
                online_machines=online,
                flashing_timeout=settings.get_int(
                    DB_PATH, "firmware.flashing_timeout_seconds"),
                confirm_timeout=settings.get_int(
                    DB_PATH, "firmware.confirm_timeout_seconds"),
            )
            if expired or dispatched:
                print(f"[firmware] Retired {expired}, dispatched {dispatched}.")
            # ...and switch on the machines this window is still waiting for (roadmap #10).
            # This is the pairing that makes auto-wake worth having here in particular:
            # dispatch above is deliberately limited to ONLINE machines, so without it a
            # window aimed at an office that is switched off simply waits out its deadline.
            woke = wake_pending_targets(firmware.pending_target_machines(DB_PATH), online,
                                        reason="firmware window")
            if woke:
                print(f"[firmware] Requested a wake for {woke} offline target(s).")
        except Exception as e:
            print(f"[firmware] Scheduler pass failed: {e}")
        time.sleep(interval)


def start_firmware_scheduler():
    threading.Thread(target=firmware_scheduler, daemon=True,
                     name="firmware_scheduler").start()


def wake_scheduler():
    """Advance Wake-on-LAN requests: read relays back, confirm arrivals, retire, dispatch.

    Its own thread and its own (much shorter) interval, for the reason firmware got one:
    the three schedulers disagree about time. A deploy tick waits on a command result and a
    flash waits on a reboot, while a wake is a UDP packet whose whole point is that
    somebody pressed a button and is watching -- so this runs on a ~15-second cadence and
    the others do not.

    Requests deliberately SURVIVE a pass that finds no relay. That is what makes a target on
    an all-asleep subnet get woken by the first peer to come online, and it is why waking is
    bounded by `wake.request_ttl_seconds` rather than by failing on the first attempt.
    """
    while True:
        interval = settings.get_int(DB_PATH, "wake.scheduler_interval_seconds")
        try:
            confirmed, expired, dispatched = wake.tick(
                DB_PATH,
                # The same roster as the backup and firmware schedulers, carrying
                # `last_seen` so a wake is confirmed against the moment its packet went out
                # rather than against a check-in that predates it.
                machines=backup_machine_roster(),
                ttl_seconds=settings.get_int(DB_PATH, "fleet.command_ttl_seconds"),
                confirm_timeout=settings.get_int(DB_PATH, "wake.confirm_timeout_seconds"),
                allow_hub_broadcast=settings.get_bool(DB_PATH, "wake.hub_broadcast"),
            )
            if confirmed or expired or dispatched:
                print(f"[wake] Confirmed {confirmed}, retired {expired}, "
                      f"dispatched {dispatched}.")
        except Exception as e:
            print(f"[wake] Scheduler pass failed: {e}")
        time.sleep(interval)


def start_wake_scheduler():
    threading.Thread(target=wake_scheduler, daemon=True, name="wake_scheduler").start()


def wake_pending_targets(machines, online, reason):
    """Wake the offline machines a maintenance window is about to dispatch into.

    Called from the deploy and firmware schedulers rather than from a scheduler of its own,
    which is the reuse roadmap #10 asked for: a wake is a PRECONDITION of a window, not a
    job kind with windows of its own, and building a third window/target/status machine to
    express "before this deploy runs, switch the PCs on" would have been the second
    scheduler that entry explicitly rejected.

    Opt-in (`wake.auto_wake_targets`), because waking a fleet at 3am is a decision rather
    than a side effect of scheduling a deploy. Never fatal: this pairing failing must not
    stop the deployment it was meant to help.
    """
    sleeping = [m for m in machines if m not in online]
    if not sleeping or not settings.get_bool(DB_PATH, "wake.auto_wake_targets"):
        return 0
    results = wake.request_many(
        DB_PATH, sleeping, requested_by="system", reason=reason, online=online,
        ttl_seconds=settings.get_int(DB_PATH, "wake.request_ttl_seconds"))
    return sum(1 for r in results if r["status"] == wake.STATUS_PENDING)


# How often the temperature-alert evaluator wakes. A machine reports every few seconds and
# the average is over minutes, so a 30-second cadence surfaces a hot machine within a tick of
# the average crossing without churning the alerts table.
HIGH_TEMP_TICK_SECONDS = 30
# How long a firmware change may sit unresolved before the hub gives up on hearing back
# (roadmap #9). Deliberately not the command TTL: the command expiring means the machine never
# CLAIMED it, while this covers the machine that claimed it and then vanished mid-write.
BIOS_CHANGE_TIMEOUT_SECONDS = 60 * 60


def evaluate_high_temp_once(db_path=None, now=None):
    """One pass of the temperature-alert evaluator. Returns (raised, episodes_ended).

    Raises a high-temperature alert for every ONLINE machine whose AVERAGE temperature
    over the configured window is at or above the threshold. The average is what makes a
    brief spike NOT an alert -- the whole point of the feature.

    A machine that has cooled, gone offline or stopped reporting does not have its alert
    resolved -- alerts stay on the tab until an operator dismisses them -- but its EPISODE
    is ended, so the next time it runs hot it accumulates a new alert beside the old one
    instead of overwriting it.

    Pure except for the database; `now` is injectable so tests drive it deterministically
    without sleeping. Scope-agnostic like the duplicate_serial hook -- an operator's
    machine scope is applied when the Alerts tab reads, never when the alert is raised.
    """
    db_path = db_path or DB_PATH
    now = int(time.time() if now is None else now)
    threshold = settings.get_int(db_path, "hub.high_temp_threshold")
    window = settings.get_int(db_path, "hub.high_temp_avg_window_seconds")
    online_window = settings.get_int(db_path, "fleet.dashboard_online_window_seconds")
    cutoff = now - window
    online_cutoff = now - online_window

    conn = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT machine, AVG(temp) AS avg_temp, MAX(ts_epoch) AS last "
            "FROM readings WHERE ts_epoch >= ? GROUP BY machine",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    hot = {}       # machine -> windowed average, for machines online AND at/above threshold
    for row in rows:
        # A machine whose most recent reading is older than the online window is not
        # "currently hot" -- its average is stale, and an alert held open on it would
        # linger after the machine was shut down or decommissioned.
        if row["last"] is None or row["last"] < online_cutoff:
            continue
        if row["avg_temp"] is not None and row["avg_temp"] >= threshold:
            hot[row["machine"]] = row["avg_temp"]

    # Reconcile against the episodes currently running: refresh the still/newly hot, and
    # end the episode of every machine that is no longer hot (cooled, offline, or gone).
    # This covers machines that dropped out of the window entirely, which the query above
    # can't return. Ending an episode does NOT resolve the alert -- it stays on the Alerts
    # tab until an operator dismisses it -- it only means the next heat-up starts a new one.
    active_machines = {a.get("machine") for a in alerts.list_open(db_path)
                       if a["kind"] == alerts.KIND_HIGH_TEMP and a.get("machine")
                       and not a.get("episode_ended_at")}
    for machine, avg_temp in hot.items():
        alerts.upsert_high_temp(db_path, machine, avg_temp, threshold, window, now=now)
    ended = 0
    for machine in active_machines - set(hot):
        if alerts.end_high_temp_episode(db_path, machine, now=now):
            ended += 1
    return len(hot), ended


def high_temp_evaluator():
    """Wake on a fixed cadence and raise/resolve temperature alerts. Same shape and
    failure discipline as retention_pruner: errors are logged, never fatal -- an
    evaluator thread that died in March must not silently stop alerting in July."""
    while True:
        try:
            evaluate_high_temp_once()
        except Exception as e:
            print(f"[high-temp] Evaluation pass failed: {e}")
        # Remote sessions expire on the same heartbeat (roadmap #2), same reasoning as
        # fleet.expire_stale_commands: a browser tab that vanished without a clean stop must
        # not leave a session -- and its minted TURN credential -- live forever.
        try:
            remote.expire_sessions(DB_PATH)
        except Exception as e:
            print(f"[remote] Session expiry sweep failed: {e}")
        # Interactive terminals ride the same sweep, and here the hub is the AUTHORITY
        # rather than a backstop: a session deliberately survives its operator navigating
        # away, so only the hub can tell "gone to Packages for ten minutes" from "closed
        # the browser on Friday" -- it is the only party that sees the console's own polls.
        # The agent's equivalent timer is deliberately much longer (AgentConfig
        # .PtyIdleTimeoutSeconds) so it cannot pre-empt this and reap the very absence the
        # feature exists to support.
        try:
            terminal.reap_sessions(DB_PATH)
        except Exception as e:
            print(f"[terminal] Session reap sweep failed: {e}")
        # Firmware changes whose machine never reported back (roadmap #9). Without this a
        # machine that went down mid-write leaves a running change forever, and the hub
        # refuses every subsequent change to it -- one dead agent would permanently lock its
        # own Firmware tab. Given an hour, which is generous against a write measured in
        # seconds, because closing one early would tell an operator a change failed while the
        # machine was still applying it.
        try:
            bios.expire_stale_changes(DB_PATH, BIOS_CHANGE_TIMEOUT_SECONDS)
        except Exception as e:
            print(f"[bios] Stale change sweep failed: {e}")
        time.sleep(HIGH_TEMP_TICK_SECONDS)


def start_high_temp_evaluator():
    threading.Thread(target=high_temp_evaluator, daemon=True,
                     name="high_temp_evaluator").start()


# How often the backup scheduler wakes to ask whether a backup is due. Same reasoning as
# PRUNE_TICK_SECONDS: sleeping the whole interval would mean an operator who shortens
# "back up every" from weekly to daily sees no effect for up to a week, which reads as
# "the setting doesn't work". A minute of granularity on a job measured in hours is free.
BACKUP_TICK_SECONDS = 60


def backup_machine_roster():
    """Every machine the per-PC scheduler may consider, each with its online status.

    backups.py deliberately does not know how to enumerate the fleet -- machine_info is
    app.py's table -- so the roster is passed in, the same way the scheduler's knobs are.

    The `online` flag is load-bearing rather than informational: files_dispatch_once
    refuses to queue a backup for a machine that cannot answer, which is what makes a
    missed backup resume when the PC comes back instead of silently sliding a full
    interval. Online-ness is derived through fleet.derive_status so that "online" means
    the same thing here as it does on the dashboard -- last contact from a non-revoked
    agent, within fleet.offline_after_seconds.

    `last_seen` rides along for Wake-on-LAN (roadmap #10), which needs the timestamp and
    not just the flag: a wake is confirmed by a check-in NEWER than the magic packet, and
    a machine can read online on a last_seen from eighty seconds BEFORE that packet went
    out. Confirming on the flag alone would report a successful wake for any machine that
    was merely flapping in and out of the offline window.
    """
    now = int(time.time())
    offline_after = settings.get_int(DB_PATH, "fleet.offline_after_seconds")
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT mi.machine AS machine, "
            "       (SELECT MAX(a.last_seen) FROM agents a "
            "          WHERE a.machine = mi.machine AND a.revoked = 0) AS last_seen "
            "FROM machine_info mi ORDER BY mi.machine ASC"
        ).fetchall()
    return [{"machine": row["machine"],
             "last_seen": row["last_seen"],
             "online": fleet.derive_status(row["last_seen"], now=now,
                                           offline_after=offline_after) == "online"}
            for row in rows]


def backup_scheduler():
    """Take the scheduled backups when they are due: the hub database, then PC files.

    Every knob is read fresh each pass and handed to backups as a value, so a schedule
    change takes effect within a minute without a restart -- and so the whole scheduler
    stays testable by calling tick() with an explicit clock.

    The two are in separate try blocks on purpose: a per-PC pass that raises must not stop
    the hub database being backed up, and vice versa. That is the same reasoning as the
    retention pruner's two blocks, and it matters more here -- these are the two halves of
    "is anything backed up at all".

    Errors are caught and logged, never allowed to kill the thread. backups.tick() already
    turns an unreachable destination into a `failed` run row; what this catches is the
    unexpected, and a backup thread that died silently in March is discovered in July.
    """
    while True:
        try:
            run = backups.tick(
                DB_PATH, LOG_DIR,
                enabled=settings.get_bool(DB_PATH, "backup.hub_enabled"),
                destination_id=settings.get(DB_PATH, "backup.hub_destination"),
                interval_hours=settings.get_int(DB_PATH, "backup.hub_interval_hours"),
                keep=settings.get_int(DB_PATH, "backup.hub_keep_generations"),
                hub_version=HUB_VERSION,
            )
            if run is not None:
                if run["status"] == backups.RUN_SUCCEEDED:
                    print(f"[backup] Uploaded {run['object_key']} "
                          f"({run['stored_bytes']} bytes).")
                else:
                    print(f"[backup] Scheduled backup FAILED: {run['error']}")
        except Exception as e:
            print(f"[backup] Hub-database pass failed: {e}")

        try:
            expired, dispatched = backups.files_tick(
                DB_PATH, LOG_DIR,
                fleet_enabled=settings.get_bool(DB_PATH, "backup.files_enabled"),
                fleet_destination=settings.get(DB_PATH, "backup.files_destination"),
                fleet_include=settings.get_list(DB_PATH, "backup.files_include"),
                fleet_exclude=settings.get_list(DB_PATH, "backup.files_exclude"),
                interval_hours=settings.get_int(DB_PATH, "backup.files_interval_hours"),
                full_every=settings.get_int(DB_PATH, "backup.files_full_every"),
                keep_chains=settings.get_int(DB_PATH, "backup.files_keep_chains"),
                limits={
                    "max_file_mb": settings.get_int(DB_PATH, "backup.files_max_file_mb"),
                    "max_set_gb": settings.get_int(DB_PATH, "backup.files_max_set_gb"),
                    "use_vss": settings.get_bool(DB_PATH, "backup.files_use_vss"),
                },
                machines=backup_machine_roster(),
                hub_url=HUB_URL,
                max_concurrent=settings.get_int(DB_PATH, "backup.files_max_concurrent"),
                ttl_seconds=settings.get_int(DB_PATH, "fleet.command_ttl_seconds"),
            )
            if expired or dispatched:
                print(f"[backup] PC files: dispatched {dispatched}, "
                      f"retired {expired} abandoned.")
        except Exception as e:
            print(f"[backup] PC-files pass failed: {e}")

        time.sleep(BACKUP_TICK_SECONDS)


def start_backup_scheduler():
    threading.Thread(target=backup_scheduler, daemon=True, name="backup_scheduler").start()


# ================================
# ACTIVE DIRECTORY SYNC (roadmap #4)
# ================================
# Opt-in and idle by default: the loop wakes on a fixed short tick, checks whether the
# feature is on and whether the configured interval has elapsed, and otherwise does
# nothing. Interval changes therefore take effect without a restart, which matters
# because an admin turning this on for the first time should not have to bounce the
# service to find out whether their bind DN was right.
DIRECTORY_TICK_SECONDS = 60

_directory_last_sync = 0.0


def run_directory_sync():
    """One AD pass. Raises DirectoryError with an operator-readable message.

    `on_change` re-points permission scoping at the new OU data. An ad_ou group's machine
    list is derived when the permissions cache is built, and a directory sync is the only
    thing that can change it -- so this invalidation is what keeps a machine moved between
    OUs from staying in its old group's scope until the next hub restart.
    """
    return directory.sync_once(DB_PATH, directory.config_from_settings(DB_PATH),
                               on_change=permissions.invalidate)


def directory_sync_scheduler():
    global _directory_last_sync
    while True:
        try:
            if settings.get_bool(DB_PATH, "directory.enabled"):
                interval = max(5, settings.get_int(
                    DB_PATH, "directory.sync_interval_minutes")) * 60
                if time.time() - _directory_last_sync >= interval:
                    # Stamped BEFORE the pass, not after: a DC that takes 90 seconds to
                    # time out must not be retried every tick for as long as it stays
                    # down, hammering a domain controller somebody is already fixing.
                    _directory_last_sync = time.time()
                    result = run_directory_sync()
                    print(f"[directory] Synced: {result['objects_found']} objects in AD, "
                          f"{result['matched']} machines matched, "
                          f"{len(result['unmatched'])} unmatched.")
        except directory.DirectoryError as e:
            print(f"[directory] Sync failed: {e}")
        except Exception as e:
            print(f"[directory] Sync pass crashed: {e}")
        time.sleep(DIRECTORY_TICK_SECONDS)


def start_directory_sync_scheduler():
    threading.Thread(target=directory_sync_scheduler, daemon=True,
                     name="directory_sync").start()


init_db()
fleet.init_fleet_db(DB_PATH)
alerts.init_alerts_db(DB_PATH)
settings.init_settings_db(DB_PATH)
permissions.init_permissions_db(DB_PATH)
users.init_users_db(DB_PATH)
packages.init_packages_db(DB_PATH)
backups.init_backups_db(DB_PATH)
remote.init_remote_db(DB_PATH)
bios.init_bios_db(DB_PATH)
firmware.init_firmware_db(DB_PATH)
wake.init_wake_db(DB_PATH)
processes.init_processes_db(DB_PATH)
live.init_live_db(DB_PATH)
terminal.init_pty_db(DB_PATH)
apitokens.init_apitokens_db(DB_PATH)
# Must run AFTER init_db(): it ALTERs machine_info, which init_db() creates.
directory.init_directory_db(DB_PATH)
# Collapse any duplicate-serial rows left by past agent-upgrade renames before serving.
try:
    resolve_all_duplicate_serials()
except Exception as e:
    print(f"[dedup] Startup duplicate sweep failed: {e}")
start_agent_version_watcher()
start_hub_update_watcher()
start_retention_pruner()
start_deploy_scheduler()
start_firmware_scheduler()
start_wake_scheduler()
start_backup_scheduler()
start_high_temp_evaluator()
start_directory_sync_scheduler()

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
                if temp >= settings.get_int(DB_PATH, "hub.high_temp_threshold"):
                    print(f"HIGH TEMPERATURE: {temp}°C")
                
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
    # Optional client-supplied timestamp (used to backfill readings buffered
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
        service_tag=data.get('service_tag'),
        manufacturer=data.get('manufacturer'),
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
    """Machine identity info (asset tag / serial number / model / agent version)
    reported by agents, plus their latest known live temp and uptime.

    Filtered to the caller's machine scope: an HR operator must not even SEE Hospital
    machines here, since this list is what the Dashboard and Asset Inventory render."""
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT machine, asset_tag, serial_number, service_tag, manufacturer, model, "
            "companion_version, updated_at "
            "FROM machine_info ORDER BY machine ASC"
        ).fetchall()
    result = [dict(row) for row in rows]
    known_machines = {row['machine'] for row in result}
    # Also surface machines that have reported temps but no identity fields yet
    # (e.g. an older client, or the very first report before a DB write lands).
    for machine in list(latest_temp.keys()) + list(latest_uptime.keys()):
        if machine not in known_machines:
            result.append({
                'machine': machine, 'asset_tag': None, 'serial_number': None,
                'service_tag': None, 'manufacturer': None, 'model': None,
                'companion_version': None, 'updated_at': None,
            })
            known_machines.add(machine)
    # Narrow BEFORE enriching -- there is no reason to read sensors for machines the
    # caller will never be shown.
    result = access.filter_rows(result)
    # One query for the whole list, not one per row. `enrolled` is what separates a machine
    # the console can ACT on from one it can only watch: an agent that never enrolled (no
    # secret at install time, or a rejected one) still posts telemetry, so it appears here
    # with a temperature and reads as perfectly healthy while every command, terminal,
    # process and backup on it silently does nothing.
    enrolled = fleet.enrolled_machines(DB_PATH)
    for row in result:
        row['uptime_seconds'] = get_latest_uptime(row['machine'])
        row['temp'] = get_latest_temp(row['machine'])
        row['diagnostics'] = extract_diagnostics(get_latest_sensors(row['machine']))
        row['status'] = derive_machine_status(row['updated_at'])
        row['enrolled'] = row['machine'] in enrolled
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
            "SELECT machine, asset_tag, serial_number, service_tag, manufacturer, model, "
            "companion_version, "
            "updated_at, ad_ou, ad_dn, ad_owner, ad_os, ad_disabled, ad_synced_at "
            "FROM machine_info WHERE machine = ?",
            (machine_name,),
        ).fetchone()
    uptime_seconds = get_latest_uptime(machine_name)
    temp = get_latest_temp(machine_name)
    if row is None and uptime_seconds is None and temp is None:
        return jsonify({"error": "Unknown machine"}), 404

    result = dict(row) if row else {
        'machine': machine_name, 'asset_tag': None, 'serial_number': None,
        'service_tag': None, 'manufacturer': None, 'model': None,
        'companion_version': None, 'updated_at': None,
        'ad_ou': None, 'ad_dn': None, 'ad_owner': None, 'ad_os': None,
        'ad_disabled': None, 'ad_synced_at': None,
    }
    result['uptime_seconds'] = uptime_seconds
    result['temp'] = temp
    # _recent_sensors_for, not the in-memory cache alone: the page hides the panels for
    # hardware a machine doesn't have, and the cache is empty for every machine until it
    # reports again after a hub restart. Falling back to the stored block means an offline
    # PC still shows the disks and fans it HAS, rather than looking like it has none.
    result['diagnostics'] = extract_diagnostics(_recent_sensors_for(machine_name))
    result['status'] = derive_machine_status(result.get('updated_at'))
    # Same field the list carries, for the same reason -- see get_machines.
    result['enrolled'] = fleet.is_enrolled(DB_PATH, machine_name)
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


@app.route('/api/machines/<machine>/live/watch', methods=['POST'])
@login_required
@access.require_machine(permissions.VIEW)
def note_live_watch(machine):
    """"Somebody is watching this machine's charts" -- renewed while the page is open.

    Pinging this IS the subscription (see live.py): it tells the machine to report every
    second with a full sensor block instead of every five with one every other time, and it
    lapses on its own about twenty seconds after the browser stops pinging. There is no
    endpoint to cancel it, because a tab that is closed or suspended never gets to call one.

    Gated on `view` + machine scope, the same gate that renders the charts this speeds up --
    it changes how OFTEN an operator sees numbers they can already see, and nothing else.

    Answers with the cadence numbers so the browser doesn't carry its own copy of them.
    """
    machine_name = str(machine).strip()
    live.note_watch(DB_PATH, machine_name, watcher=permissions_web.current_actor())
    return jsonify({
        "machine": machine_name,
        "poll_interval": live.POLL_INTERVAL_SECONDS,
        "watch_ttl": live.WATCH_TTL_SECONDS,
        "interval_seconds": live.FAST_INTERVAL_SECONDS,
    }), 200


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


@app.route('/api/machines/<machine>/sensors/all')
@login_required
@access.require_machine(permissions.VIEW)
def get_machine_all_sensors(machine):
    """EVERY sensor this machine reported, grouped the way LibreHardwareMonitor's own tree
    groups them: hardware -> category -> sensors.

    The curated diagnostics fields exist because a dashboard has to choose what to chart.
    This endpoint deliberately chooses nothing: the agent flattens the entire LHM tree
    (every hardware category, every sub-hardware, several hundred sensors on a workstation)
    and until now the hub stored all of it and showed a dozen. A helpdesk operator chasing
    "why is this PC throttling" wants the VRM temperature and the +12V rail, and no
    hand-picked list is ever going to have guessed at those in advance.

    Rendered from the block as reported -- `group` and `text` are the agent's own
    formatting (SensorReader.GroupFor/FormatText), so a sensor type the hub has never heard
    of still arrives with a sensible heading and a unit. Report order is preserved
    throughout: LHM walks hardware in a deliberate order, and re-sorting it alphabetically
    would scatter "Core #1..#16" and split each chip's temperatures away from its clocks.
    """
    machine_name = str(machine).strip()
    sensors = _recent_sensors_for(machine_name) or []

    hardware = {}          # id -> {name, id, groups: {group -> [sensor, ...]}}
    for s in sensors:
        if not isinstance(s, dict):
            continue
        hardware_id = str(s.get("hardware_id") or "")
        entry = hardware.setdefault(hardware_id, {
            "id": hardware_id,
            "name": str(s.get("hardware") or "") or hardware_id,
            "groups": {},
        })
        # An unknown/absent group still gets a heading rather than silently dropping the
        # sensor -- "no category" is not a reason to hide a reading from the operator.
        group = str(s.get("group") or "").strip() or str(s.get("type") or "").strip() or "Other"
        entry["groups"].setdefault(group, []).append({
            "name": str(s.get("name") or ""),
            "type": str(s.get("type") or ""),
            "value": s.get("value") if isinstance(s.get("value"), (int, float))
                     and not isinstance(s.get("value"), bool) else None,
            "text": s.get("text") if isinstance(s.get("text"), str) else None,
        })

    return jsonify({
        "machine": machine_name,
        "count": sum(len(items) for hw in hardware.values() for items in hw["groups"].values()),
        "hardware": [
            {"id": hw["id"], "name": hw["name"],
             "groups": [{"name": name, "sensors": items} for name, items in hw["groups"].items()]}
            for hw in hardware.values()
        ],
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
    fleet.audit(DB_PATH, permissions_web.current_actor(),
                "machine.primary_sensor", machine_name, {"to": applied},
                level=fleet.LEVEL_NOTICE)
    return jsonify({"status": "saved", "primary_sensor_name": applied})


@app.route('/api/machines/<machine>', methods=['DELETE'])
@login_required
@access.require_machine(permissions.MANAGE_SETTINGS)
def delete_machine(machine):
    """Hard-delete a decommissioned machine: its identity row, all temperature history,
    and its fleet agent enrollment. Irreversible. If the machine's agent is still
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
    # Drop its backup configuration too. Run history and the file manifest deliberately
    # SURVIVE -- deleting a machine record does not mean its archives stopped existing,
    # and those are exactly what someone wants when the deletion turns out to be a mistake.
    backups.forget_machine(DB_PATH, machine_name)
    # And its firmware inventory: a different box reusing this hostname must not inherit an
    # attribute list describing hardware it isn't -- which is exactly what an operator would
    # then be offered a "change this setting" button against.
    bios.forget_machine(DB_PATH, machine_name)
    # Same for any queued or in-flight firmware flash: its targets go, so a fleet-wide
    # update is not left permanently at 39/40 waiting on a machine record that no longer
    # exists. The job's own history stays, because it happened.
    firmware.forget_machine(DB_PATH, machine_name)
    # And its adapters and wake history (roadmap #10), for the relay reason above: a deleted
    # machine that left its NIC rows behind stays a candidate relay for its old subnet, and
    # every wake the hub routed through it would be queued at a hostname nothing answers to.
    wake.forget_machine(DB_PATH, machine_name)
    # And its last process snapshot and any live watch on it. This is transient state that
    # would lapse on its own within the minute, but a deleted machine leaving a table row
    # naming what its users had open is exactly the kind of residue a deletion is for.
    processes.forget_machine(DB_PATH, machine_name)
    # ...and any live-chart watch on it, for the same reason.
    live.forget_machine(DB_PATH, machine_name)
    # Its BIOS setup password override lives in the secret file rather than the database, so
    # bios.forget_machine cannot reach it -- and a stored password surviving its machine would
    # be handed to whatever next takes that hostname.
    backups.delete_secret(LOG_DIR, bios.secret_id_for(machine_name))
    # Drop any in-memory live status so a deleted machine doesn't linger on the Dashboard.
    _evict_live_status(machine_name)
    actor = permissions_web.current_actor()
    fleet.audit(DB_PATH, actor, "machine.delete", machine_name,
                level=fleet.LEVEL_NOTICE)
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
        # A per-machine alert (high temperature) is scoped on its single subject machine; there is
        # nothing to enrich or let the operator merge, so it passes straight through with
        # its `detail` payload once scope allows.
        if alert["kind"] == alerts.KIND_HIGH_TEMP:
            machine = alert.get("machine")
            if machine and keep is not None and not keep(machine):
                continue
            visible.append(alert)
            continue
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

    actor = permissions_web.current_actor()
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
    actor = permissions_web.current_actor()
    fleet.audit(DB_PATH, actor, "alert.dismiss", str(alert_id),
                level=fleet.LEVEL_NOTICE)
    return jsonify({"status": "dismissed"}), 200

def _scoped_open_alert_count():
    """The number the sidebar badge shows: open alerts this caller is allowed to see.

    Shared by the badge's server render (inject_nav_context) and by /api/alerts/count,
    which the badge poller calls -- two implementations of "which alerts are mine" is how
    the number drifts between the page load and the first poll.
    """
    keep = access.machine_filter()
    if keep is None:
        return alerts.count_open(DB_PATH)

    def _in_scope(a):
        # Per-machine alerts (high temperature) scope on their single subject; the
        # duplicate_serial `machines` list scopes if it touches any kept machine. Such an
        # alert carries an empty `machines`, so it must be checked on `machine` first --
        # otherwise "no machines" would read as fleet-wide and leak the count across a
        # scope boundary.
        if a["kind"] == alerts.KIND_HIGH_TEMP:
            return bool(a.get("machine")) and keep(a["machine"])
        return not a.get("machines") or any(keep(m) for m in a["machines"])

    return sum(1 for a in alerts.list_open(DB_PATH) if _in_scope(a))


@app.route('/api/alerts/count')
@login_required
def get_alert_count():
    """Just the badge number, for the poller in common.js. Deliberately not gated on VIEW:
    the badge renders for every signed-in operator, and a poll that 403s where the page
    render succeeded would freeze the count instead of correcting it."""
    try:
        return jsonify({"count": _scoped_open_alert_count()}), 200
    except Exception:
        # Same posture as the nav context: a badge is never worth an error to the caller.
        return jsonify({"count": 0}), 200


def _resolve_history_window(args):
    """Parse the date/from/to/resolution/limit query params into
    (start_epoch, end_epoch, resolution, limit). Raises ValueError with a user-facing
    message on a bad date."""
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
            # A caller that names no start gets the configured default window, not the whole
            # archive. The oldest reading is a FLOOR, not the start: it stops us asking for
            # time that predates the data, but on a hub with months of history it used to
            # make "no from=" mean "everything", which is unbounded and nothing wants.
            window = settings.get_int(DB_PATH, "hub.live_default_window_seconds")
            start_dt = end_dt - timedelta(seconds=window)
            oldest = get_oldest_reading_datetime()
            if oldest is not None and oldest > start_dt:
                start_dt = oldest

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


def current_language():
    """The language for THIS request, resolved once and memoised on `g`.

    Memoised because a single page render calls t() dozens of times and each call would
    otherwise re-read the user's row: the language of a request cannot change halfway
    through it, so one lookup is both cheaper and the only self-consistent answer.

    Never raises. This runs on the login page, where there is no session and no user row
    to have a preference in -- and on a hub whose users table predates the `language`
    column, if one somehow starts before init_users_db(). A console that 500s because it
    could not decide what language to apologise in is not an improvement on English.
    """
    cached = getattr(g, "_hub_language", None)
    if cached:
        return cached
    chosen = None
    fleet_default = None
    accept = None
    try:
        fleet_default = settings.get(DB_PATH, "hub.default_language")
        email = permissions_web.current_identity().get("email")
        if email:
            chosen = users.get_language(DB_PATH, email)
        # Read unconditionally: the fleet default ships as i18n.AUTO, so on a hub where
        # nobody has set one this header IS the answer for anyone who has not chosen.
        accept = request.headers.get("Accept-Language")
    except Exception:
        pass
    language = i18n.resolve(user_language=chosen, fleet_default=fleet_default,
                            accept_language=accept)
    g._hub_language = language
    # Memoised alongside the resolved language because the topbar picker needs the
    # CHOICE, not the outcome -- see i18n.template_context.
    g._hub_chosen_language = chosen
    return language


def chosen_language():
    """The signed-in user's stored language choice, or None if they follow the browser.

    Resolving the language is what discovers this, so it is read back off `g` rather
    than queried a second time; calling `current_language()` first guarantees it is there.
    """
    current_language()
    return getattr(g, "_hub_chosen_language", None)


# Hand the resolver to i18n so the JSON blueprints can localise the user-facing text they
# serve (capability labels, setting labels/help) without a language parameter threaded
# through ten blueprint factories. Templates still get their language passed explicitly.
i18n.set_language_provider(current_language)


@app.route("/api/language", methods=["POST"])
@login_required
def set_language():
    """Record the signed-in user's console language.

    Gated on nothing but having a session, deliberately: this is a personal display
    preference, not fleet configuration. Requiring `manage_settings` to change the
    language of your own console would mean only admins could read the UI in their own
    language, which inverts who the feature is for.
    """
    payload = request.get_json(silent=True) or {}
    language = str(payload.get("language") or "").strip()
    # i18n.AUTO clears the stored choice rather than storing a language, putting the user
    # back on the fleet default / their browser. Without it a choice is one-way: the
    # picker would have no option meaning "follow my browser again".
    if language != i18n.AUTO and not i18n.is_supported(language):
        return jsonify({"error": f"{language!r} is not a supported language."}), 400
    email = permissions_web.current_identity().get("email")
    try:
        users.set_language(DB_PATH, email,
                           None if language == i18n.AUTO else language)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # The response is not what applies it -- the client reloads, and the next render
    # resolves the language from the row just written.
    return jsonify({"language": language})


# ================================
# APP SHELL
# ================================
# base.html has three renderings and this picks between them. The point of the shell is that
# the document holding the sidebar and topbar NEVER reloads: pages load into an iframe inside
# it, so a remote screen survives a trip to Packages. That is not a nicety -- a WebRTC session
# belongs to the document that negotiated it, and in a plain multi-page app clicking any nav
# link tears one down (see static/js/remote.js's pagehide, which stops the hub session
# deliberately rather than leaking a capture helper on the target PC for hours).
#
# The shell and the page it frames answer on the SAME url, told apart by Sec-Fetch-Dest, which
# the browser sets on the request itself:
#
#   document -> "shell"    the chrome, with a frame already pointed at this url
#   iframe   -> "framed"   this page's own content, no chrome around it
#   absent   -> "classic"  the whole page exactly as it rendered before the shell existed
#
# Doing it on the header rather than on a ?frame=1 parameter is what keeps every existing url
# working untouched: bookmarks, url_for(), the ?machine= deep link into Remote, and every link
# already in the templates. A deep link to /packages is a document request, so it gets the
# shell with Packages already in the frame -- one url, no redirect, no hash routing.
#
# "classic" is not a dead branch. It is every non-browser client (the test suite, curl, an
# uptime monitor) and any browser too old to send Sec-Fetch-Dest, and it renders what this app
# always rendered: a complete page, chrome included. Such a browser simply does not get the
# persistence -- nothing else about it changes.
def _shell_mode():
    dest = request.headers.get("Sec-Fetch-Dest")
    if dest == "iframe":
        return "framed"
    if dest == "document":
        return "shell"
    return "classic"


@app.after_request
def _frame_policy(response):
    """Allow our own shell to frame us, and nobody else.

    The shell needs same-origin framing to work at all, so this states that intent rather
    than leaving it to a default -- and denies cross-origin framing while it is there, which
    is the clickjacking defence a console with a Delete button wants regardless.

    Note for deployments: an upstream proxy that adds `X-Frame-Options: DENY` of its own
    wins (browsers apply the most restrictive of the headers they receive) and the shell
    will render an empty frame. Terminate that at the proxy, not here.
    """
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    return response


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
    # Translation is injected here rather than as a jinja_env global so `t` and the
    # language it renders in arrive together and cannot disagree -- a global `t` reading
    # the language separately is how a page ends up with a Spanish sidebar and an English
    # heading. Bound to this request's language, so templates just call t('nav.alerts').
    context = {"open_alert_count": 0, "user_capabilities": set(),
               "is_superuser": False, "cap": permissions,
               "hub_version": HUB_VERSION,
               "shell_mode": _shell_mode(),
               "latest_agent_version": get_latest_agent_version()}
    context.update(i18n.template_context(current_language(), chosen_language()))
    if not session.get("user"):
        return context
    try:
        current = access.current()
        context["user_capabilities"] = current["capabilities"]
        context["is_superuser"] = current["superuser"]
        context["open_alert_count"] = _scoped_open_alert_count()
    except Exception:
        pass
    return context

@app.route("/")
@login_required
@access.require(permissions.VIEW)
def index():
    # The Dashboard no longer classifies high temperatures (that is the Alerts tab now, from a
    # server-side average), so the threshold values it used to embed are gone.
    return render_template("index.html", hub_version=HUB_VERSION,
                           latest_agent_version=get_latest_agent_version())

@app.route("/inventory")
@login_required
@access.require(permissions.VIEW)
def inventory_page():
    return render_template("inventory.html", hub_version=HUB_VERSION,
                           latest_agent_version=get_latest_agent_version())

@app.route("/alerts")
@login_required
@access.require(permissions.VIEW)
def alerts_page():
    return render_template("alerts.html", hub_version=HUB_VERSION,
                           latest_agent_version=get_latest_agent_version())

@app.route("/settings")
@login_required
@access.require(permissions.MANAGE_SETTINGS)
def settings_page():
    return render_template("settings.html", hub_version=HUB_VERSION,
                           latest_agent_version=get_latest_agent_version())

@app.route("/permissions")
@login_required
@access.require(permissions.MANAGE_PERMISSION_GROUPS)
def permissions_page():
    return render_template("permissions.html", hub_version=HUB_VERSION,
                           latest_agent_version=get_latest_agent_version())

@app.route("/remote")
@login_required
@access.require(permissions.REMOTE_CONTROL)
def remote_page():
    """The multi-machine remote workspace: several PCs open at once, one tab each.

    Gated on remote_control rather than view, like the machine page's Remote tab -- the page
    is nothing but viewers. It names no machine of its own: which PCs are opened is chosen in
    the browser, and every session it starts goes through /api/remote/<machine>/start, which
    checks the capability AND the machine's scope again for each one.
    """
    return render_template("remote.html", hub_version=HUB_VERSION,
                           latest_agent_version=get_latest_agent_version())

@app.route("/machine/<machine>")
@login_required
@access.require_machine(permissions.VIEW)
def machine_page(machine):
    return render_template(
        "machine.html", machine=machine,
        high_temp_threshold=settings.get_int(DB_PATH, "hub.high_temp_threshold"),
        low_load_threshold=settings.get_int(DB_PATH, "hub.low_load_threshold"),
        live_window_seconds=settings.get_int(DB_PATH, "hub.live_default_window_seconds"),
        # How often the page renews its "somebody is watching this" ping. Served rather than
        # hardcoded in machine.js so the ping rate and the watch TTL it has to beat stay one
        # decision (see live.py).
        live_poll_seconds=live.POLL_INTERVAL_SECONDS,
        enabled_metrics=enabled_history_metrics(),
        hub_version=HUB_VERSION,
        latest_agent_version=get_latest_agent_version()
    )

# ================================
# START
# ================================
application = app

if __name__ == "__main__":
    # Local self-reporting is intentionally disabled: the fleet agent runs on the
    # hub machine too and reports this host with full sensor data, so starting
    # local_logger here would double-report the hostname and make the dashboard's
    # Load/Clock flicker. See wsgi.py for how to re-enable on an agent-less box.
    # start_local_logger()

    # Use socketio.run instead of app.run
    print(f"Starting hub on {LOCAL_MACHINE}...")
    socketio.run(app, host="0.0.0.0", port=3001, debug=False, allow_unsafe_werkzeug=True)
