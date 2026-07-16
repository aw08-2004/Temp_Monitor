import ast
import collections
import ctypes
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import traceback

import requests

# ================================
# VERSION  --  bump on every push to main, or nothing will update.
# From 2.8.0 onward every pushed companion.py must also be re-signed (see
# sign_release.py) or clients will refuse the update. See UPDATE_PUBLIC_KEY_HEX.
# ================================
VERSION = "2.10.0"

# Third-party packages the companion needs. Update this alongside any new
# import so a self-update installs them automatically -- see install_requirements().
# `cryptography` is required to verify signed updates (see verify_signature()).
REQUIREMENTS = ["requests", "cryptography"]

# Spawn helper subprocesses (pip, the restarted self) with no console window,
# even when running under python.exe instead of pythonw.exe.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ================================
# CONFIG
# ================================
HUB_URL = "https://temp.arkeanos.net/api/report"
LHM_URL = "http://localhost:8085/data.json"   # LibreHardwareMonitor's built-in web server
INTERVAL = 5                                   # seconds between temp reports
SENSOR_INTERVAL = 10                           # seconds between full sensor-block reports
UPTIME_INTERVAL = 10 * 60                      # seconds between uptime reports
LHM_BACKOFF_MAX = 30                           # cap on the back-off sleep when LHM is down
OFFLINE_BUFFER_MAX = 1000                      # max readings held while the hub is unreachable

# Machine identity: the PC's own name. Override only if you really need to.
MACHINE_NAME = os.environ.get("TEMP_MONITOR_MACHINE") or socket.gethostname()

# Self-update settings
UPDATE_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/companion.py"
SIG_URL = UPDATE_URL + ".sig"                  # detached Ed25519 signature over companion.py bytes
UPDATE_INTERVAL = 7 * 24 * 60 * 60             # 7 days
UPDATE_ENABLED = True
MAX_CHAIN_RESTARTS = 3

# Ed25519 public key (64 hex chars) used to verify downloaded updates. Set this at
# release time: run `python sign_release.py --genkey`, keep the private key OFF the
# repo, and paste the printed public key here. An empty key makes verify_signature()
# fail closed -- updates stall (never apply) rather than run unverified code.
UPDATE_PUBLIC_KEY_HEX = "9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c"

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)

# Machine-wide state dir (log + restart guard). Survives reinstalls and lives
# outside the install dir so an update swap never disturbs it.
PROGRAM_DATA_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "TempMonitor")
RESTART_STATE_PATH = os.path.join(PROGRAM_DATA_DIR, ".restart_state.json")

# --- Migration to the C#/.NET fleet agent (Windows Service) -------------------
# From 2.10.0, companion.py retires itself in favor of TempMonitorAgent.exe, which
# reaches telemetry parity AND adds the fleet command channel (restart/rename/
# scripts/etc. -- see agent/). This is fully automatic and fleet-wide: every
# machine that self-updates to this version attempts the swap on its own. It is
# designed to fail safe -- any error at any step just leaves companion.py running
# and retries (capped) later; nothing here ever removes the fallback until the new
# agent is CONFIRMED running.
MIGRATION_ENABLED = True                          # kill-switch for a hotfix push
AGENT_MANIFEST_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/agent.manifest.json"
AGENT_INSTALLER_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/install/agent-install.ps1"
AGENT_SERVICE_NAME = "TempMonitorAgent"
MIGRATION_STATE_PATH = os.path.join(PROGRAM_DATA_DIR, ".migration_state.json")
MIGRATION_CHECK_INTERVAL = 24 * 60 * 60           # once a day
MIGRATION_MAX_ATTEMPTS = 5                        # then give up and stay on companion.py

# Sensor names we like, best first
PREFERRED_SENSORS = [
    "cpu package",
    "core (tctl/tdie)",
    "core average",
    "core max",
    "cpu cores",
]


# ================================
# LOGGING  --  rotating file log so field issues on client machines are
# diagnosable. Under the scheduled task we run windowless (pythonw), so plain
# print() goes nowhere; this persists to %ProgramData%\TempMonitor\companion.log.
# ================================
def setup_logging():
    logger = logging.getLogger("companion")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        os.makedirs(PROGRAM_DATA_DIR, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(PROGRAM_DATA_DIR, "companion.log"),
            maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass  # never let logging setup crash the agent
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = setup_logging()


# ================================
# TEMP READ  --  via LibreHardwareMonitor's JSON endpoint
# ================================
def _parse_value(node):
    """Strict parse for CPU *temperature* selection: 0 or negative means "no valid
    reading". Do NOT use for arbitrary sensors -- see _parse_sensor_value()."""
    raw = node.get("RawValue")
    if isinstance(raw, (int, float)) and raw == raw and raw > 0:  # raw != raw catches NaN
        return round(float(raw), 1)

    text = str(node.get("Value", ""))
    match = re.search(r"(-?\d+[.,]?\d*)", text)
    if match:
        try:
            value = float(match.group(1).replace(",", "."))
            if value > 0:
                return round(value, 1)
        except ValueError:
            pass
    return None


def _parse_sensor_value(node):
    """Lenient parse for the general flattened sensor list. Unlike _parse_value,
    this keeps legitimate 0 and negative readings -- a 0% CPU load, a parked-core
    clock, a 0 RPM fan, or a negative voltage/temperature offset are all real
    values, not "missing". Only genuinely absent values and NaN return None."""
    raw = node.get("RawValue")
    if isinstance(raw, (int, float)) and raw == raw:  # reject NaN only
        return round(float(raw), 1)

    text = str(node.get("Value", ""))
    match = re.search(r"(-?\d+[.,]?\d*)", text)
    if match:
        try:
            return round(float(match.group(1).replace(",", ".")), 1)
        except ValueError:
            pass
    return None


def _walk(node, in_cpu=False, found=None):
    """Collect (sensor_name, temp) pairs from CPU hardware nodes in the LHM tree."""
    if found is None:
        found = []

    hardware_id = str(node.get("HardwareId", "")).lower()
    if "cpu" in hardware_id:
        in_cpu = True

    if in_cpu and node.get("Type") == "Temperature":
        value = _parse_value(node)
        if value is not None:
            found.append((str(node.get("Text", "")).lower(), value))

    for child in node.get("Children", []):
        _walk(child, in_cpu, found)

    return found


def fetch_lhm_data():
    """Fetches and parses the full LibreHardwareMonitor sensor tree, once per cycle.
    Returns None on any failure; the caller handles back-off and messaging so this
    doesn't spam the log every few seconds when LHM is simply down."""
    try:
        resp = requests.get(LHM_URL, timeout=3)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException:
        return None  # "LHM unreachable" -- reported by the main loop's back-off logic
    except ValueError:
        log.warning("LibreHardwareMonitor returned something that isn't JSON.")
    except Exception as e:
        log.warning(f"Error reading local temp: {e}")

    return None


def pick_cpu_temp(data):
    """Picks the best single CPU temperature out of an already-fetched LHM tree."""
    sensors = _walk(data)

    if not sensors:
        log.warning("No CPU temperature sensors found. Is LibreHardwareMonitor running as admin?")
        return None

    for wanted in PREFERRED_SENSORS:
        for name, value in sensors:
            if wanted in name:
                return value

    return sensors[0][1]  # any CPU temp beats no CPU temp


def get_cpu_temp():
    """Backwards-compatible one-call helper: fetch the LHM tree and return the best
    CPU temp. The main loop now calls fetch_lhm_data()/pick_cpu_temp() separately so
    it can reuse the tree for flatten_sensors(), but this wrapper is kept ON PURPOSE:
    companions deployed at v2.5.0 and earlier gate their self-update on the literal
    string 'def get_cpu_temp' being present in the downloaded source. Dropping it
    strands every one of them at their current version -- which is the bug this
    version fixes. Do not remove until the whole fleet is past 2.6.0."""
    data = fetch_lhm_data()
    return pick_cpu_temp(data) if data else None


def flatten_sensors(node, hardware=None, hardware_id=None, group=None, found=None):
    """Collects every leaf sensor in the whole LHM tree (not just CPU) into a flat
    list of dicts, so the hub can store/diagnose off of everything LHM reports.

    hardware_id (e.g. "/amdcpu/0", "/gpu-nvidia/0", "/ram") is kept alongside the
    human-readable hardware name (e.g. "AMD Ryzen 7 5800X") because the display
    name never contains the literal words "cpu"/"gpu"/etc -- hardware_id is what
    reliably identifies the hardware category, same as _walk's "cpu" in
    HardwareId check above.
    """
    if found is None:
        found = []

    text = node.get("Text")
    children = node.get("Children") or []
    node_hardware_id = node.get("HardwareId")

    if node_hardware_id:
        hardware = text
        hardware_id = str(node_hardware_id).lower()
    elif "Type" not in node and text:
        # Sensor-type group node (e.g. "Temperatures", "Clocks", "Load") -- its
        # children are the actual leaf sensors.
        group = text

    if not children and ("Value" in node or "RawValue" in node):
        found.append({
            "hardware": hardware,
            "hardware_id": hardware_id,
            "group": group,
            "name": text,
            "type": node.get("Type"),
            "value": _parse_sensor_value(node),
            "text": node.get("Value"),
        })

    for child in children:
        flatten_sensors(child, hardware, hardware_id, group, found)

    return found


# ================================
# SYSTEM IDENTITY  --  BIOS asset tag / serial number / model, read once at startup
# ================================
_PLACEHOLDER_ASSET_TAGS = ("default string", "no asset", "to be filled", "invalid")


def get_system_info():
    """Reads BIOS/chassis identity via CIM. Values don't change at runtime, so call once."""
    ps_command = (
        "$bios = Get-CimInstance Win32_BIOS; "
        "$cs = Get-CimInstance Win32_ComputerSystem; "
        "$encl = Get-CimInstance Win32_SystemEnclosure; "
        "[PSCustomObject]@{ "
        "SerialNumber = $bios.SerialNumber; "
        "Model = $cs.Model; "
        "AssetTag = $encl.SMBIOSAssetTag "
        "} | ConvertTo-Json -Compress"
    )
    info = {"serial_number": None, "model": None, "asset_tag": None}
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_command],
            timeout=15,
            stderr=subprocess.DEVNULL,
        )
        parsed = json.loads(output)

        serial = str(parsed.get("SerialNumber") or "").strip()
        model = str(parsed.get("Model") or "").strip()
        asset_tag = str(parsed.get("AssetTag") or "").strip()

        info["serial_number"] = serial or None
        info["model"] = model or None
        # Many boards ship with a placeholder like "Default string" when no asset tag is set
        if asset_tag and not any(p in asset_tag.lower() for p in _PLACEHOLDER_ASSET_TAGS):
            info["asset_tag"] = asset_tag
    except Exception as e:
        log.warning(f"[system-info] Could not read BIOS/system info: {e}")

    return info


# ================================
# UPTIME  --  ms since boot via kernel32, no extra dependency needed
# ================================
def get_uptime_seconds():
    try:
        return round(ctypes.windll.kernel32.GetTickCount64() / 1000)
    except Exception as e:
        log.warning(f"[uptime] Could not read system uptime: {e}")
        return None


# ================================
# SELF-UPDATER
# ================================
def parse_version(text):
    match = re.search(r'^VERSION\s*=\s*["\']([\d.]+)["\']', text, re.MULTILINE)
    return match.group(1) if match else None


def version_tuple(v):
    """Tolerant version parse: reads the leading dotted-numeric prefix and ignores
    any suffix (e.g. '2.8.0-rc1' -> (2, 8, 0)). Never raises."""
    match = re.match(r"\s*(\d+(?:\.\d+)*)", str(v))
    if not match:
        return (0,)
    return tuple(int(p) for p in match.group(1).split("."))


def _cmp_versions(a, b):
    """Return 1 if a > b, -1 if a < b, 0 if equal. Pads to equal length so that
    '2.8' and '2.8.0' compare as equal rather than '2.8' < '2.8.0'."""
    ta, tb = version_tuple(a), version_tuple(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def looks_like_valid_companion(text):
    """Cheap sanity checks so a GitHub error page can never overwrite the script.

    Keyed on structural markers (a core config constant + the updater itself)
    rather than an individual sensor-helper name. The previous check looked for
    'def get_cpu_temp', which was renamed to pick_cpu_temp in the 2.6.0 commit --
    that silently stranded the whole fleet, because every deployed companion
    rejected the new source as invalid and refused to self-update."""
    return (
        len(text) > 500
        and "HUB_URL" in text
        and "def check_for_update" in text
        and parse_version(text) is not None
    )


def verify_signature(source_bytes, signature_hex):
    """Verify a detached Ed25519 signature over the exact downloaded bytes using the
    embedded public key. Fails closed: an unset key, a malformed signature, or a
    mismatch all return False, so the update is refused rather than run unverified."""
    if not UPDATE_PUBLIC_KEY_HEX:
        log.error("[update] No update public key configured; refusing unverified update.")
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except Exception as e:
        log.error(f"[update] cryptography unavailable, cannot verify update: {e}")
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(UPDATE_PUBLIC_KEY_HEX))
        pub.verify(bytes.fromhex(signature_hex.strip()), source_bytes)
        return True
    except InvalidSignature:
        log.error("[update] Signature verification FAILED -- update rejected.")
        return False
    except Exception as e:
        log.error(f"[update] Could not verify signature: {e}")
        return False


def parse_requirements(text):
    """Pull the REQUIREMENTS list literal out of a companion.py source string."""
    match = re.search(r'^REQUIREMENTS\s*=\s*(\[[^\]]*\])', text, re.MULTILINE)
    if not match:
        return []
    try:
        parsed = ast.literal_eval(match.group(1))
        return [str(p) for p in parsed if str(p).strip()]
    except (ValueError, SyntaxError):
        return []


def install_requirements(requirements):
    """pip install the new version's deps before swapping it in. Idempotent -- pip
    no-ops on packages that are already satisfied, so this is cheap on every update."""
    if not requirements:
        return True

    log.info(f"[update] Ensuring dependencies: {', '.join(requirements)}")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", *requirements],
            check=True,
            timeout=180,
            creationflags=_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        log.error(f"[update] Failed to install dependencies {requirements}: {e}")
        return False


def replace_with_retry(tmp_path, dest_path, attempts=5, delay_seconds=1.0):
    """os.replace, retrying past transient locks (AV scan, indexer) so an update
    doesn't get abandoned just because the file was briefly in use."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(tmp_path, dest_path)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay_seconds)


# --- restart-loop guard, persisted so it survives a Task-Scheduler relaunch ---
def _read_restart_state():
    """Returns (target_version, count) for the in-flight update, ('' , 0) if none."""
    try:
        with open(RESTART_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return str(data.get("target") or ""), int(data.get("count") or 0)
    except Exception:
        pass
    return "", 0


def _write_restart_state(target, count):
    try:
        os.makedirs(PROGRAM_DATA_DIR, exist_ok=True)
        with open(RESTART_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"target": target, "count": count}, f)
    except Exception as e:
        log.warning(f"[update] Could not write restart state: {e}")


def _clear_restart_state():
    try:
        os.remove(RESTART_STATE_PATH)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"[update] Could not clear restart state: {e}")


def restart_self():
    """Relaunch into the freshly-written companion.py.

    In production we run as a Windows Scheduled Task, and Task Scheduler puts each
    task inside a job object. When our process exits, the job is closed and every
    child in it is killed -- so simply Popen-ing a replacement and exiting races:
    the new process is torn down with us, nothing is left running, and because we
    exited 0 the task's "restart on failure" never fires. The whole fleet then sits
    on the new file but the old, still-loaded code until the next logon.

    Strategy: try to launch the replacement so it *breaks away* from the job. If the
    job forbids breakaway (the Task Scheduler case), CreateProcess fails and we
    instead exit non-zero, letting the task's RestartCount/RestartInterval (set in
    install.ps1) relaunch us cleanly from the new file on disk. Outside a job (e.g.
    run by hand from a console) the breakaway simply succeeds and we restart in
    place. The restart-loop guard lives in a file (see _write_restart_state), so it
    holds across both paths even though the task-host restart gets a fresh env."""
    log.info("[update] Restarting into the new version...")

    argv = [sys.executable, SCRIPT_PATH] + sys.argv[1:]

    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        try:
            subprocess.Popen(
                argv,
                cwd=SCRIPT_DIR,
                close_fds=True,
                creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                               | CREATE_BREAKAWAY_FROM_JOB),
            )
        except OSError:
            # Inside a no-breakaway job (Task Scheduler). Any child we spawn dies
            # with us, so don't leave a doomed process behind -- exit non-zero and
            # let the task host restart us from the new file. os._exit avoids
            # atexit/buffered-IO cleanup so the exit code reaches Task Scheduler
            # unambiguously.
            log.info("[update] No-breakaway job detected; exiting for task-host restart.")
            os._exit(17)
        else:
            sys.exit(0)
    else:
        subprocess.Popen(argv, cwd=SCRIPT_DIR, close_fds=True, start_new_session=True)
        sys.exit(0)


def handle_hub_response(response, last_update_check):
    """Hub echoes back the newest companion version it knows about (see app.py's
    /api/report). If it's ahead of us, check now instead of waiting for the
    weekly poll -- this is what makes updates roll out promptly."""
    try:
        data = response.json()
    except ValueError:
        return last_update_check

    hub_version = data.get("latest_version") if isinstance(data, dict) else None
    if not hub_version:
        return last_update_check

    try:
        is_newer = _cmp_versions(hub_version, VERSION) > 0
    except Exception:
        return last_update_check

    if is_newer:
        log.info(f"[update] Hub reports v{hub_version} is available, checking now...")
        check_for_update()
        return time.time()

    return last_update_check


def check_for_update():
    """Pull main, verify signature, swap the file, restart. Never fatal."""
    if not UPDATE_ENABLED:
        return

    try:
        log.info(f"[update] Checking for updates (current v{VERSION})...")
        resp = requests.get(UPDATE_URL, timeout=10)
        resp.raise_for_status()
        remote_bytes = resp.content
        try:
            remote_src = remote_bytes.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("[update] Remote file isn't valid UTF-8. Ignoring.")
            return

        if not looks_like_valid_companion(remote_src):
            log.warning("[update] Remote file failed sanity check. Ignoring.")
            return

        remote_version = parse_version(remote_src)
        if _cmp_versions(remote_version, VERSION) <= 0:
            log.info(f"[update] Already up to date (remote v{remote_version}).")
            return

        # Restart-loop guard: bound repeated failed attempts to reach the SAME
        # target version, across both restart paths (file-persisted).
        state_target, state_count = _read_restart_state()
        attempts = state_count if state_target == remote_version else 0
        if attempts >= MAX_CHAIN_RESTARTS:
            log.error(f"[update] Giving up on v{remote_version} after {attempts} restart attempts.")
            return

        log.info(f"[update] New version found: v{VERSION} -> v{remote_version}")

        # Verify a detached signature over the EXACT downloaded bytes. Fail closed.
        try:
            sig_resp = requests.get(SIG_URL, timeout=10)
            sig_resp.raise_for_status()
            sig_hex = sig_resp.text.strip()
        except requests.exceptions.RequestException:
            log.error("[update] Could not fetch signature; refusing update (fail closed).")
            return
        if not verify_signature(remote_bytes, sig_hex):
            log.error("[update] Aborting update: signature could not be verified.")
            return

        # Only compile/run once we trust the bytes.
        try:
            compile(remote_src, "<downloaded companion.py>", "exec")
        except SyntaxError as e:
            log.error(f"[update] Downloaded file has a syntax error, aborting: {e}")
            return

        if not install_requirements(parse_requirements(remote_src)):
            log.error("[update] Aborting update: dependency installation failed.")
            return

        # Write the verified bytes verbatim so the on-disk file matches what was
        # signed (no newline/encoding rewriting).
        fd, tmp_path = tempfile.mkstemp(suffix=".py", dir=SCRIPT_DIR)
        with os.fdopen(fd, "wb") as f:
            f.write(remote_bytes)

        backup_path = SCRIPT_PATH + ".bak"
        try:
            with open(SCRIPT_PATH, "rb") as src, open(backup_path, "wb") as dst:
                dst.write(src.read())
        except Exception as e:
            log.error(f"[update] Could not write backup, aborting: {e}")
            os.remove(tmp_path)
            return

        replace_with_retry(tmp_path, SCRIPT_PATH)
        log.info(f"[update] Updated to v{remote_version} (backup at {backup_path})")

        _write_restart_state(remote_version, attempts + 1)
        restart_self()

    except requests.exceptions.RequestException:
        log.warning("[update] Could not reach GitHub. Will try again later.")
    except SystemExit:
        raise
    except Exception:
        log.error("[update] Unexpected error during update check:\n" + traceback.format_exc())


# ================================
# HUB REPORTING
# ================================
def post_reading(payload):
    """POST a single reading to the hub. Returns True if the hub accepted it (or
    rejected it as bad input -- 4xx -- which won't succeed on a retry either), and
    the response object for the caller. Raises requests exceptions on connectivity
    failure so the caller can buffer."""
    resp = requests.post(HUB_URL, json=payload, timeout=3, allow_redirects=False)
    return resp


def flush_offline_buffer(buffer):
    """Send readings buffered while the hub was unreachable, oldest first. Stops on
    the first connectivity failure or hub-side (5xx) error, keeping the rest for
    later. Drops 4xx entries (malformed -- a retry won't help)."""
    sent = 0
    while buffer:
        item = buffer[0]
        try:
            resp = post_reading(item)
        except requests.exceptions.RequestException:
            break
        if resp.status_code >= 500:
            break
        buffer.popleft()
        sent += 1
    if sent:
        log.info(f"Flushed {sent} buffered reading(s) to the hub ({len(buffer)} remaining).")


# ================================
# MIGRATION  --  install the C# agent, then decommission this companion
# ================================
def _agent_service_state():
    """Returns the C# agent's SCM state string (e.g. 'RUNNING', 'STOPPED'), or None
    if the service doesn't exist or sc.exe couldn't be reached."""
    try:
        out = subprocess.check_output(
            ["sc.exe", "query", AGENT_SERVICE_NAME],
            timeout=10, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW,
        ).decode("utf-8", "ignore")
    except Exception:
        return None
    match = re.search(r"STATE\s*:\s*\d+\s+(\w+)", out)
    return match.group(1) if match else None


def _read_migration_state():
    try:
        with open(MIGRATION_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {"attempts": int(data.get("attempts") or 0)}
    except Exception:
        pass
    return {"attempts": 0}


def _write_migration_state(state):
    try:
        os.makedirs(PROGRAM_DATA_DIR, exist_ok=True)
        with open(MIGRATION_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"[migration] Could not write migration state: {e}")


def _decommission_self():
    """The C# agent is confirmed running -- unregister companion's scheduled tasks
    and stop LibreHardwareMonitor (the agent reads sensors in-process, it doesn't
    need the standalone LHM web server), then exit for good. Both companion and the
    new agent report the same machine name, so leaving both running would double
    every reading -- this must be a clean handoff, not a dual-run period."""
    log.info("[migration] C# agent confirmed running -- decommissioning companion.")
    for task in ("TempMonitor - Companion", "TempMonitor - LibreHardwareMonitor"):
        try:
            subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"],
                            timeout=15, capture_output=True, creationflags=_NO_WINDOW)
        except Exception as e:
            log.warning(f"[migration] Could not remove scheduled task {task!r}: {e}")
    try:
        subprocess.run(["taskkill", "/IM", "LibreHardwareMonitor.exe", "/F"],
                        timeout=15, capture_output=True, creationflags=_NO_WINDOW)
    except Exception as e:
        log.warning(f"[migration] Could not stop LibreHardwareMonitor: {e}")
    log.info("[migration] Decommissioned. Telemetry and fleet commands now come from "
              "TempMonitorAgent. Exiting.")
    os._exit(0)


def check_and_migrate_to_agent():
    """Fully automatic, fleet-wide: if the C# Windows Service agent isn't installed
    yet, fetch its signed release manifest (same Ed25519 trust root as companion's
    own self-update), install it, confirm it's actually running, then decommission
    this companion. Never fatal -- any failure just leaves companion.py running and
    retries (capped) on the next check."""
    if not MIGRATION_ENABLED:
        return

    state = _read_migration_state()
    if state["attempts"] >= MIGRATION_MAX_ATTEMPTS:
        return  # gave up -- keep reporting via companion.py indefinitely

    existing_state = _agent_service_state()
    if existing_state is not None:
        if existing_state == "RUNNING":
            _decommission_self()
        return  # installed but not (yet) running -- leave it be, check again later

    log.info("[migration] TempMonitorAgent service not found -- attempting install.")
    state["attempts"] += 1
    _write_migration_state(state)

    try:
        manifest_resp = requests.get(AGENT_MANIFEST_URL, timeout=10)
        manifest_resp.raise_for_status()
        manifest_bytes = manifest_resp.content

        sig_resp = requests.get(AGENT_MANIFEST_URL + ".sig", timeout=10)
        sig_resp.raise_for_status()
        sig_hex = sig_resp.text.strip()

        if not verify_signature(manifest_bytes, sig_hex):
            log.error("[migration] Agent manifest signature invalid -- aborting (fail closed).")
            return

        manifest = json.loads(manifest_bytes)
        agent_url = manifest["url"]

        installer_resp = requests.get(AGENT_INSTALLER_URL, timeout=15)
        installer_resp.raise_for_status()
        installer_path = os.path.join(tempfile.gettempdir(), "agent-install.ps1")
        with open(installer_path, "w", encoding="utf-8") as f:
            f.write(installer_resp.text)

        # Companion already runs elevated (RunLevel Highest), so this needs no
        # further UAC prompt. No -EnrollmentSecret: the agent runs telemetry-only
        # until an operator enrolls it separately, same as a fresh manual install.
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", installer_path,
             "-AgentUrl", agent_url],
            timeout=300, capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        log.info(f"[migration] Installer exit code: {result.returncode}")
        if result.returncode != 0:
            log.warning(f"[migration] Installer failed:\n{result.stdout[-1000:]}\n{result.stderr[-1000:]}")
            return

        for _ in range(10):
            time.sleep(2)
            if _agent_service_state() == "RUNNING":
                _decommission_self()
                return
        log.warning("[migration] Service did not reach RUNNING state after install; will retry later.")

    except requests.exceptions.RequestException as e:
        log.warning(f"[migration] Could not reach GitHub: {e}. Will try again later.")
    except SystemExit:
        raise
    except Exception:
        log.error("[migration] Unexpected error during migration attempt:\n" + traceback.format_exc())


# ================================
# MAIN LOOP
# ================================
if __name__ == "__main__":
    log.info(f"Companion monitor v{VERSION} - machine: {MACHINE_NAME}")
    log.info(f"Reading sensors from {LHM_URL}")
    log.info(f"Sending data to {HUB_URL} every {INTERVAL} seconds.")

    system_info = get_system_info()
    log.info(f"System info: {system_info}")

    # If a restart was in flight to reach this version and we're now running it,
    # the update succeeded -- clear the guard so future updates start fresh.
    _st, _ = _read_restart_state()
    if _st and _cmp_versions(_st, VERSION) <= 0:
        _clear_restart_state()

    check_for_update()                # check #1: every startup
    check_and_migrate_to_agent()      # attempt #1: every startup
    last_update_check = time.time()
    last_migration_check = time.time()
    last_uptime_check = 0            # force an uptime report on the first cycle
    last_sensor_report = 0           # force a sensor report on the first cycle
    lhm_fail_streak = 0
    offline_buffer = collections.deque(maxlen=OFFLINE_BUFFER_MAX)

    while True:
        # check #2: once a week if the process just keeps running
        if time.time() - last_update_check >= UPDATE_INTERVAL:
            last_update_check = time.time()
            check_for_update()

        if time.time() - last_migration_check >= MIGRATION_CHECK_INTERVAL:
            last_migration_check = time.time()
            check_and_migrate_to_agent()

        lhm_data = fetch_lhm_data()
        if lhm_data is None:
            # LHM unreachable: back off so we don't hammer/log every 5s.
            if lhm_fail_streak == 0:
                log.warning(f"LibreHardwareMonitor unreachable at {LHM_URL}; backing off.")
            lhm_fail_streak += 1
            time.sleep(min(15 * lhm_fail_streak, LHM_BACKOFF_MAX))
            continue
        if lhm_fail_streak:
            log.info("LibreHardwareMonitor reachable again.")
            lhm_fail_streak = 0

        current_temp = pick_cpu_temp(lhm_data)

        if current_temp is not None:
            payload = {
                "machine": MACHINE_NAME,
                "temp": current_temp,
                "companion_version": VERSION,
                "client_ts": int(time.time()),   # hub honors this so backfilled readings keep their time
            }
            payload.update(system_info)

            # Full sensor block only every SENSOR_INTERVAL -- temp still every cycle.
            # The hub renders cached diagnostics between sensor reports, so Load/Clock
            # don't blank out; this cuts ~36KB/report bandwidth and DB growth.
            if time.time() - last_sensor_report >= SENSOR_INTERVAL:
                sensors = flatten_sensors(lhm_data)
                if sensors:
                    payload["sensors"] = sensors
                last_sensor_report = time.time()

            if time.time() - last_uptime_check >= UPTIME_INTERVAL:
                uptime_seconds = get_uptime_seconds()
                if uptime_seconds is not None:
                    payload["uptime_seconds"] = uptime_seconds
                last_uptime_check = time.time()

            try:
                response = post_reading(payload)
                log.info(f"Sent: {current_temp}°C - Hub responded: {response.status_code}")
                flush_offline_buffer(offline_buffer)   # drain backlog now that we're connected
                last_update_check = handle_hub_response(response, last_update_check)
            except requests.exceptions.RequestException:
                # Buffer a lightweight (sensorless) copy so we don't hoard 36KB blobs;
                # temp history is what matters for backfill.
                buffered = {k: v for k, v in payload.items() if k != "sensors"}
                offline_buffer.append(buffered)
                log.warning(f"Failed to connect to Hub. Buffered reading ({len(offline_buffer)} queued).")

        time.sleep(INTERVAL)
