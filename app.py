import ctypes
import json
import os
import re
import time
import threading
import csv
import socket
import sqlite3
import queue
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps
import wmi
import pythoncom
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from flask_socketio import SocketIO
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
import requests

import fleet
from fleet_web import create_fleet_blueprint

load_dotenv()

# ================================
# CONFIG
# ================================
# Bump on every push to main and restart the hub service -- shown in the
# dashboard header so a stale/un-restarted deployment is obvious at a glance.
HUB_VERSION = "1.11.0"
CHECK_INTERVAL = 5
OVERHEAT_THRESHOLD = 85
# Below this CPU load %, a high temp reading is flagged "investigate" rather than
# "expected" -- sent to the frontend the same way OVERHEAT_THRESHOLD is.
LOW_LOAD_THRESHOLD = 40
SPIKE_THRESHOLD = 10
LHM_URL = "http://localhost:8085/data.json"
HUB_URL = os.environ.get("HUB_URL", "http://localhost:5000")

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

DB_PATH = os.path.join(LOG_DIR, "temp_v2.db")
# Daily CSV archives are retired -- the DB is the single source of truth now.
# Existing CSV files on disk are left untouched; we just stop writing new ones.
WRITE_CSV_ARCHIVE = False
SQLITE_TIMEOUT_SECONDS = 30
DB_WRITE_BATCH_SIZE = 200
DB_WRITE_FLUSH_SECONDS = 0.5

# Readings retention. A background pruner deletes readings older than this, so the
# DB stays bounded instead of growing forever (see start_retention_pruner()).
RETENTION_DAYS = 30
RETENTION_PRUNE_INTERVAL_SECONDS = 24 * 60 * 60
RETENTION_PRUNE_BATCH = 50000
LIVE_DEFAULT_WINDOW_HOURS = 3
DEFAULT_HISTORY_LIMIT = 1200
MAX_HISTORY_POINTS_PER_MACHINE = 2000
MAX_HISTORY_MACHINE_MULTIPLIER = 16
VALID_RESOLUTIONS = {"raw": None, "10s": 10, "1m": 60, "5m": 300}

LOCAL_MACHINE = socket.gethostname()

# Latest known uptime/temp per machine -- kept in memory for speed, but also
# mirrored to machine_info (see persist_live_status) so a hub restart doesn't
# instantly blank them out. The DB fallback only counts for LIVE_STATUS_CACHE_SECONDS;
# past that a machine that's actually gone quiet should read as unknown again,
# not show an arbitrarily stale reading forever.
LIVE_STATUS_CACHE_SECONDS = 10 * 60

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

def extract_diagnostics(sensors):
    """Pulls the specific fields the UI shows out of a raw flattened LHM sensor
    list (see companion.py's flatten_sensors). Every field is None when not
    found -- e.g. no discrete GPU, or an older companion that sent no sensors."""
    if not sensors:
        return {
            "cpu_load_pct": None, "cpu_clock_mhz": None,
            "gpu_temp": None, "gpu_load_pct": None, "gpu_clock_mhz": None,
            "memory_load_pct": None,
        }
    return {
        "cpu_load_pct": _find_sensor_value(sensors, "cpu", "Load", ["cpu total", "total cpu"]),
        "cpu_clock_mhz": _find_sensor_value(sensors, "cpu", "Clock", ["core average", "cpu core #1", "bus speed"]),
        "gpu_temp": _find_sensor_value(sensors, "gpu", "Temperature", ["gpu core", "gpu hot spot", "gpu package"]),
        "gpu_load_pct": _find_sensor_value(sensors, "gpu", "Load", ["gpu core", "d3d 3d"]),
        "gpu_clock_mhz": _find_sensor_value(sensors, "gpu", "Clock", ["gpu core", "gpu shader"]),
        "memory_load_pct": _find_sensor_value(sensors, "ram", "Load", ["memory"]),
    }

def load_cached_live_status(machine_name):
    """DB-backed fallback for get_latest_temp/get_latest_uptime right after a hub
    restart, when the in-memory dicts above are empty. Only trusts a row up to
    LIVE_STATUS_CACHE_SECONDS old -- see the comment above."""
    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT last_temp, last_uptime_seconds, updated_at FROM machine_info WHERE machine = ?",
            (machine_name,),
        ).fetchone()
    if not row or not row["updated_at"]:
        return {}
    updated_at = parse_request_datetime(row["updated_at"])
    if updated_at is None or (datetime.now() - updated_at).total_seconds() > LIVE_STATUS_CACHE_SECONDS:
        return {}
    return {"temp": row["last_temp"], "uptime_seconds": row["last_uptime_seconds"]}

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
# AUTH CONFIG (Google sign-in)
# ================================
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
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
        "ALLOWED_EMAILS must list at least one allowed Google account email (comma-separated)."
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
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
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


# Fleet command-channel endpoints (agent-facing token auth + console-facing
# login_required). Registered here, once login_required exists to hand in.
app.register_blueprint(create_fleet_blueprint(
    DB_PATH, AGENT_ENROLLMENT_SECRET, login_required
))


@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/login/google")
def login_google():
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo") or oauth.google.userinfo(token=token)
    email = (user_info.get("email") or "").strip().lower()

    if not user_info.get("email_verified", True):
        return "Google account email is not verified.", 403
    if email not in ALLOWED_EMAILS:
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


@socketio.on("connect")
def handle_socket_connect():
    if not session.get("user"):
        return False  # reject the connection; browser falls back to no live updates

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

def write_readings_batch(records):
    if not records:
        return
    with get_db_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO readings(ts_text, ts_epoch, machine, temp, sensors_json) VALUES (?, ?, ?, ?, ?)",
            records,
        )

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
            records.append(
                (to_timestamp_str(parsed_ts), to_epoch_seconds(parsed_ts), machine, temp, None)
            )

    write_readings_batch(records)
    with get_db_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO imported_days(day) VALUES (?)", (date,))

def enqueue_reading(timestamp_str, timestamp_epoch, machine, temp, sensors_json=None):
    ensure_db_writer_running()
    record = (timestamp_str, timestamp_epoch, machine, float(temp), sensors_json)
    try:
        db_write_queue.put_nowait(record)
    except queue.Full:
        print("WARNING: SQLite queue is full; writing synchronously.")
        write_readings_batch([record])

# How often persist_live_status actually hits SQLite per machine. Reports come in
# every few seconds, but the cache only needs to be fresh to within
# LIVE_STATUS_CACHE_SECONDS, so there's no need to write anywhere near that often.
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

def save_and_emit_temp(machine, temp, uptime_seconds=None, sensors=None, timestamp_epoch=None):
    machine_name = str(machine).strip()
    if not machine_name:
        raise ValueError("Machine name cannot be empty.")

    temp_value = float(temp)
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
    enqueue_reading(timestamp_str, timestamp_epoch, machine_name, temp_value, sensors_json)

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
    socketio.emit('new_temp', {
        'machine': machine_name,
        'timestamp': timestamp_str,
        'timestamp_epoch': timestamp_epoch,
        'temp': temp_value,
        'threshold': OVERHEAT_THRESHOLD,
        'low_load_threshold': LOW_LOAD_THRESHOLD,
        'uptime_seconds': get_latest_uptime(machine_name),
        'diagnostics': extract_diagnostics(get_latest_sensors(machine_name)),
    })

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

def query_raw_history(start_epoch, end_epoch, machine, limit):
    sql = """
        SELECT machine, ts_text, temp
        FROM readings
        WHERE ts_epoch >= ? AND ts_epoch <= ?
    """
    params = [start_epoch, end_epoch]
    if machine:
        sql += " AND machine = ?"
        params.append(machine)

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
            "y": round(float(row["temp"]), 1),
        })
    return {machine_name: list(points) for machine_name, points in history.items()}

def query_bucketed_history(start_epoch, end_epoch, machine, limit, bucket_seconds):
    sql = """
        SELECT
            machine,
            CAST((ts_epoch / ?) AS INTEGER) * ? AS bucket_epoch,
            AVG(temp) AS avg_temp,
            MIN(temp) AS min_temp,
            MAX(temp) AS max_temp,
            COUNT(*) AS sample_count
        FROM readings
        WHERE ts_epoch >= ? AND ts_epoch <= ?
    """
    params = [bucket_seconds, bucket_seconds, start_epoch, end_epoch]
    if machine:
        sql += " AND machine = ?"
        params.append(machine)
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
            "y": round(float(row["avg_temp"]), 1),
            "min": round(float(row["min_temp"]), 1),
            "max": round(float(row["max_temp"]), 1),
            "count": int(row["sample_count"]),
        })
    return {machine_name: list(points) for machine_name, points in history.items()}

# ================================
# RETENTION  --  keep the readings table bounded to RETENTION_DAYS
# ================================
def prune_old_readings_once():
    """Delete readings older than RETENTION_DAYS, in batches so the first big prune
    (potentially millions of rows) never holds a single long write lock that would
    stall the reading writer. Returns the number of rows removed."""
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
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
        print(f"[retention] Pruned {total} reading(s) older than {RETENTION_DAYS} days.")
    return total


def prune_command_output_once():
    """Drop live-terminal scrollback for commands that finished long ago. The durable
    record (command_results.output) is untouched -- these rows only exist so an operator
    can watch a command stream, and 256KB per command adds up otherwise."""
    cutoff = int(time.time()) - fleet.OUTPUT_RETENTION_SECONDS
    removed = fleet.prune_command_output(DB_PATH, cutoff)
    if removed:
        print(f"[retention] Pruned {removed} command output chunk(s).")
    return removed


def retention_pruner():
    while True:
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
        time.sleep(RETENTION_PRUNE_INTERVAL_SECONDS)


def start_retention_pruner():
    threading.Thread(target=retention_pruner, daemon=True, name="retention_pruner").start()


init_db()
fleet.init_fleet_db(DB_PATH)
start_companion_version_watcher()
start_retention_pruner()

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
                if temp >= OVERHEAT_THRESHOLD:
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
        if client_ts > now_epoch + 300 or client_ts < now_epoch - RETENTION_DAYS * 86400:
            client_ts = None
    save_and_emit_temp(machine, float(data['temp']), uptime_seconds, sensors, timestamp_epoch=client_ts)
    # Keep an enrolled agent's online/offline status fresh off its ordinary temp
    # reports too, so it doesn't read offline between dedicated heartbeats.
    fleet.touch_last_seen(DB_PATH, machine)
    reported_version = data.get('companion_version')
    save_machine_info(
        machine,
        data.get('asset_tag'),
        data.get('serial_number'),
        data.get('model'),
        reported_version,
    )

    response_payload = {"status": "success"}
    latest_version = get_advertised_version(reported_version)
    if latest_version:
        response_payload["latest_version"] = latest_version
    return jsonify(response_payload), 200

@app.route('/api/machines')
@login_required
def get_machines():
    """Machine identity info (asset tag / serial number / model / companion version)
    reported by companions, plus their latest known live temp and uptime."""
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
    for row in result:
        row['uptime_seconds'] = get_latest_uptime(row['machine'])
        row['temp'] = get_latest_temp(row['machine'])
        row['diagnostics'] = extract_diagnostics(get_latest_sensors(row['machine']))
    result.sort(key=lambda row: row['machine'])
    return jsonify(result)


@app.route('/api/machines/<machine>')
@login_required
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
    return jsonify(result)

@app.route('/api/history')
@login_required
def get_history():
    """Provide history data with optional range/machine/resolution controls."""
    date = request.args.get("date")
    machine = (request.args.get("machine") or "").strip() or None
    from_raw = request.args.get("from")
    to_raw = request.args.get("to")
    limit = parse_history_limit(request.args.get("limit"))
    requested_resolution = (request.args.get("resolution") or "auto").strip().lower()

    if date:
        day_start = parse_request_datetime(date)
        if day_start is None:
            return jsonify({"error": "Invalid date format; use YYYY-MM-DD."}), 400
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
            start_dt = get_oldest_reading_datetime() or (end_dt - timedelta(hours=LIVE_DEFAULT_WINDOW_HOURS))

    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    start_epoch = to_epoch_seconds(start_dt)
    end_epoch = to_epoch_seconds(end_dt)
    span_seconds = max(1, end_epoch - start_epoch)
    resolution = pick_resolution(requested_resolution, span_seconds)

    if resolution == "raw":
        history = query_raw_history(start_epoch, end_epoch, machine, limit)
    else:
        history = query_bucketed_history(
            start_epoch,
            end_epoch,
            machine,
            limit,
            VALID_RESOLUTIONS[resolution],
        )
    return jsonify(history)

@app.route('/api/daily_summary')
@login_required
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

    with get_db_conn() as conn:
        summary = conn.execute(
            """
            SELECT AVG(temp) AS overall_avg, COUNT(*) AS reading_count
            FROM readings
            WHERE ts_epoch >= ? AND ts_epoch < ?
            """,
            (start_epoch, end_epoch),
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
            """
            SELECT machine, AVG(temp) AS avg_temp
            FROM readings
            WHERE ts_epoch >= ? AND ts_epoch < ?
            GROUP BY machine
            ORDER BY machine ASC
            """,
            (start_epoch, end_epoch),
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

@app.route("/")
@login_required
def index():
    return render_template("index.html", hub_version=HUB_VERSION,
                           latest_companion_version=get_latest_companion_version(),
                           latest_agent_version=get_latest_agent_version())

@app.route("/history")
@login_required
def history_page():
    return render_template("history.html", hub_version=HUB_VERSION,
                           latest_companion_version=get_latest_companion_version(),
                           latest_agent_version=get_latest_agent_version())

@app.route("/machine/<machine>")
@login_required
def machine_page(machine):
    return render_template(
        "machine.html", machine=machine, overheat_threshold=OVERHEAT_THRESHOLD,
        low_load_threshold=LOW_LOAD_THRESHOLD, hub_version=HUB_VERSION,
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
