import ast
import ctypes
import json
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
# VERSION  --  bump on every push to main, or nothing will update
# ================================
VERSION = "2.4.0"

# Third-party packages the companion needs. Update this alongside any new
# import so a self-update installs them automatically -- see install_requirements().
REQUIREMENTS = ["requests"]

# Spawn helper subprocesses (pip, the restarted self) with no console window,
# even when running under python.exe instead of pythonw.exe.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ================================
# CONFIG
# ================================
HUB_URL = "https://temp.arkeanos.net/api/report"
LHM_URL = "http://localhost:8085/data.json"   # LibreHardwareMonitor's built-in web server
INTERVAL = 5                                   # seconds between temp reports
UPTIME_INTERVAL = 10 * 60                      # seconds between uptime reports

# Machine identity: the PC's own name. Override only if you really need to.
MACHINE_NAME = os.environ.get("TEMP_MONITOR_MACHINE") or socket.gethostname()

# Self-update settings
UPDATE_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/companion.py"
UPDATE_INTERVAL = 7 * 24 * 60 * 60  # 7 days
UPDATE_ENABLED = True
MAX_CHAIN_RESTARTS = 3

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)

# Sensor names we like, best first
PREFERRED_SENSORS = [
    "cpu package",
    "core (tctl/tdie)",
    "core average",
    "core max",
    "cpu cores",
]


# ================================
# TEMP READ  --  via LibreHardwareMonitor's JSON endpoint
# ================================
def _parse_value(node):
    """LHM gives RawValue (float) and Value ('52.3 °C'). Prefer raw, fall back to text."""
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


def get_cpu_temp():
    """Reads CPU temperature from the LibreHardwareMonitor web server."""
    try:
        resp = requests.get(LHM_URL, timeout=3)
        resp.raise_for_status()
        sensors = _walk(resp.json())

        if not sensors:
            print("No CPU temperature sensors found. Is LibreHardwareMonitor running as admin?")
            return None

        for wanted in PREFERRED_SENSORS:
            for name, value in sensors:
                if wanted in name:
                    return value

        return sensors[0][1]  # any CPU temp beats no CPU temp

    except requests.exceptions.RequestException:
        print(f"Cannot reach LibreHardwareMonitor at {LHM_URL}. Is it running with the web server enabled?")
    except ValueError:
        print("LibreHardwareMonitor returned something that isn't JSON.")
    except Exception as e:
        print(f"Error reading local temp: {e}")

    return None


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
        print(f"[system-info] Could not read BIOS/system info: {e}")

    return info


# ================================
# UPTIME  --  ms since boot via kernel32, no extra dependency needed
# ================================
def get_uptime_seconds():
    try:
        return round(ctypes.windll.kernel32.GetTickCount64() / 1000)
    except Exception as e:
        print(f"[uptime] Could not read system uptime: {e}")
        return None


# ================================
# SELF-UPDATER
# ================================
def parse_version(text):
    match = re.search(r'^VERSION\s*=\s*["\']([\d.]+)["\']', text, re.MULTILINE)
    return match.group(1) if match else None


def version_tuple(v):
    return tuple(int(part) for part in v.split("."))


def looks_like_valid_companion(text):
    """Cheap sanity checks so a GitHub error page can never overwrite the script."""
    return (
        len(text) > 500
        and "def get_cpu_temp" in text
        and parse_version(text) is not None
    )


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

    print(f"[update] Ensuring dependencies: {', '.join(requirements)}")
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
        print(f"[update] Failed to install dependencies {requirements}: {e}")
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


def restart_self(chain_count):
    env = os.environ.copy()
    env["TEMP_MONITOR_RESTARTS"] = str(chain_count + 1)

    print("[update] Restarting into the new version...\n")
    sys.stdout.flush()

    subprocess.Popen(
        [sys.executable, SCRIPT_PATH] + sys.argv[1:],
        env=env,
        cwd=SCRIPT_DIR,
        creationflags=_NO_WINDOW,
    )
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
        is_newer = version_tuple(hub_version) > version_tuple(VERSION)
    except (ValueError, AttributeError):
        return last_update_check

    if is_newer:
        print(f"[update] Hub reports v{hub_version} is available, checking now...")
        check_for_update()
        return time.time()

    return last_update_check


def check_for_update():
    """Pull main, compare versions, swap the file, restart. Never fatal."""
    if not UPDATE_ENABLED:
        return

    chain_count = int(os.environ.get("TEMP_MONITOR_RESTARTS", "0"))
    if chain_count >= MAX_CHAIN_RESTARTS:
        print("[update] Restart limit reached this session, skipping check.")
        return

    try:
        print(f"[update] Checking for updates (current v{VERSION})...")
        resp = requests.get(UPDATE_URL, timeout=10)
        resp.raise_for_status()
        remote_src = resp.text

        if not looks_like_valid_companion(remote_src):
            print("[update] Remote file failed sanity check. Ignoring.")
            return

        remote_version = parse_version(remote_src)
        if version_tuple(remote_version) <= version_tuple(VERSION):
            print(f"[update] Already up to date (remote v{remote_version}).")
            return

        print(f"[update] New version found: v{VERSION} -> v{remote_version}")

        fd, tmp_path = tempfile.mkstemp(suffix=".py", dir=SCRIPT_DIR)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(remote_src)

        try:
            compile(remote_src, tmp_path, "exec")
        except SyntaxError as e:
            os.remove(tmp_path)
            print(f"[update] Downloaded file has a syntax error, aborting: {e}")
            return

        if not install_requirements(parse_requirements(remote_src)):
            os.remove(tmp_path)
            print("[update] Aborting update: dependency installation failed.")
            return

        backup_path = SCRIPT_PATH + ".bak"
        try:
            with open(SCRIPT_PATH, "r", encoding="utf-8") as src, \
                 open(backup_path, "w", encoding="utf-8", newline="\n") as dst:
                dst.write(src.read())
        except Exception as e:
            print(f"[update] Could not write backup, aborting: {e}")
            os.remove(tmp_path)
            return

        replace_with_retry(tmp_path, SCRIPT_PATH)
        print(f"[update] Updated to v{remote_version} (backup at {backup_path})")

        restart_self(chain_count)

    except requests.exceptions.RequestException:
        print("[update] Could not reach GitHub. Will try again later.")
    except SystemExit:
        raise
    except Exception:
        print("[update] Unexpected error during update check:")
        traceback.print_exc()


# ================================
# MAIN LOOP
# ================================
if __name__ == "__main__":
    print(f"Companion monitor v{VERSION} - machine: {MACHINE_NAME}")
    print(f"Reading sensors from {LHM_URL}")
    print(f"Sending data to {HUB_URL} every {INTERVAL} seconds.")

    system_info = get_system_info()
    print(f"System info: {system_info}")

    check_for_update()               # check #1: every startup
    last_update_check = time.time()
    last_uptime_check = 0            # force an uptime report on the first cycle

    while True:
        # check #2: once a week if the process just keeps running
        if time.time() - last_update_check >= UPDATE_INTERVAL:
            last_update_check = time.time()
            check_for_update()

        current_temp = get_cpu_temp()

        if current_temp is not None:
            try:
                payload = {"machine": MACHINE_NAME, "temp": current_temp}
                payload.update(system_info)
                if time.time() - last_uptime_check >= UPTIME_INTERVAL:
                    uptime_seconds = get_uptime_seconds()
                    if uptime_seconds is not None:
                        payload["uptime_seconds"] = uptime_seconds
                    last_uptime_check = time.time()
                response = requests.post(
                    HUB_URL,
                    json=payload,
                    timeout=3,
                    allow_redirects=False
                )
                print(f"Sent: {current_temp}°C - Hub responded: {response.status_code}")
                last_update_check = handle_hub_response(response, last_update_check)
            except requests.exceptions.RequestException:
                print("Failed to connect to Hub. Retrying next cycle...")

        time.sleep(INTERVAL)
