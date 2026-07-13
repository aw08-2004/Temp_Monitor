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
VERSION = "2.0.0"

# ================================
# CONFIG
# ================================
HUB_URL = "https://temp.arkeanos.net/api/report"
LHM_URL = "http://localhost:8085/data.json"   # LibreHardwareMonitor's built-in web server
INTERVAL = 5                                   # seconds between temp reports

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


def restart_self(chain_count):
    env = os.environ.copy()
    env["TEMP_MONITOR_RESTARTS"] = str(chain_count + 1)

    print("[update] Restarting into the new version...\n")
    sys.stdout.flush()

    subprocess.Popen([sys.executable, SCRIPT_PATH] + sys.argv[1:], env=env, cwd=SCRIPT_DIR)
    sys.exit(0)


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

        backup_path = SCRIPT_PATH + ".bak"
        try:
            with open(SCRIPT_PATH, "r", encoding="utf-8") as src, \
                 open(backup_path, "w", encoding="utf-8", newline="\n") as dst:
                dst.write(src.read())
        except Exception as e:
            print(f"[update] Could not write backup, aborting: {e}")
            os.remove(tmp_path)
            return

        os.replace(tmp_path, SCRIPT_PATH)
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

    check_for_update()               # check #1: every startup
    last_update_check = time.time()

    while True:
        # check #2: once a week if the process just keeps running
        if time.time() - last_update_check >= UPDATE_INTERVAL:
            last_update_check = time.time()
            check_for_update()

        current_temp = get_cpu_temp()

        if current_temp is not None:
            try:
                response = requests.post(
                    HUB_URL,
                    json={"machine": MACHINE_NAME, "temp": current_temp},
                    timeout=3,
                    allow_redirects=False
                )
                print(f"Sent: {current_temp}°C - Hub responded: {response.status_code}")
            except requests.exceptions.RequestException:
                print("Failed to connect to Hub. Retrying next cycle...")

        time.sleep(INTERVAL)
