import ctypes
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
from flask import Flask, render_template_string, request, jsonify, redirect, session, url_for
from flask_socketio import SocketIO
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
import requests

load_dotenv()

# ================================
# CONFIG
# ================================
# Bump on every push to main and restart the hub service -- shown in the
# dashboard header so a stale/un-restarted deployment is obvious at a glance.
HUB_VERSION = "1.0.0"
CHECK_INTERVAL = 5
OVERHEAT_THRESHOLD = 85
SPIKE_THRESHOLD = 10
LHM_URL = "http://localhost:8085/data.json"
HUB_URL = os.environ.get("HUB_URL", "http://localhost:5000")

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

DB_PATH = os.path.join(LOG_DIR, "temp_v2.db")
WRITE_CSV_ARCHIVE = True
SQLITE_TIMEOUT_SECONDS = 30
DB_WRITE_BATCH_SIZE = 200
DB_WRITE_FLUSH_SECONDS = 0.5
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
# COMPANION VERSION WATCHER  --  lets companions self-update promptly instead of
# waiting for their own weekly GitHub poll. We periodically check the same
# source companion.py updates from, and echo the newest known version back in
# /api/report's response; companion.py checks for an update as soon as it sees
# a newer number there.
# ================================
COMPANION_SOURCE_URL = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/companion.py"
COMPANION_VERSION_CHECK_INTERVAL = 15 * 60  # 15 minutes

latest_companion_version = None
latest_companion_version_lock = threading.Lock()

def get_latest_companion_version():
    with latest_companion_version_lock:
        return latest_companion_version

def refresh_latest_companion_version():
    global latest_companion_version
    try:
        resp = requests.get(COMPANION_SOURCE_URL, timeout=10)
        resp.raise_for_status()
        match = re.search(r'^VERSION\s*=\s*["\']([\d.]+)["\']', resp.text, re.MULTILINE)
        if match:
            with latest_companion_version_lock:
                latest_companion_version = match.group(1)
    except Exception as e:
        print(f"[companion-version] Could not refresh latest version: {e}")

def companion_version_watcher():
    while True:
        refresh_latest_companion_version()
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
# WEB & WEBSOCKET SETUP
# ================================
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
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


@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML)


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
            "INSERT OR IGNORE INTO readings(ts_text, ts_epoch, machine, temp) VALUES (?, ?, ?, ?)",
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
                (to_timestamp_str(parsed_ts), to_epoch_seconds(parsed_ts), machine, temp)
            )

    write_readings_batch(records)
    with get_db_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO imported_days(day) VALUES (?)", (date,))

def enqueue_reading(timestamp_str, timestamp_epoch, machine, temp):
    ensure_db_writer_running()
    record = (timestamp_str, timestamp_epoch, machine, float(temp))
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

def save_and_emit_temp(machine, temp, uptime_seconds=None):
    machine_name = str(machine).strip()
    if not machine_name:
        raise ValueError("Machine name cannot be empty.")

    temp_value = float(temp)
    now = datetime.now()
    timestamp_str = to_timestamp_str(now)
    timestamp_epoch = to_epoch_seconds(now)

    if WRITE_CSV_ARCHIVE:
        append_csv_archive(timestamp_str, machine_name, temp_value)

    enqueue_reading(timestamp_str, timestamp_epoch, machine_name, temp_value)

    set_latest_uptime(machine_name, uptime_seconds)
    set_latest_temp(machine_name, temp_value)
    persist_live_status(machine_name, temp_value, uptime_seconds)

    # Emit via WebSocket
    socketio.emit('new_temp', {
        'machine': machine_name,
        'timestamp': timestamp_str,
        'temp': temp_value,
        'threshold': OVERHEAT_THRESHOLD,
        'uptime_seconds': get_latest_uptime(machine_name)
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

init_db()
start_companion_version_watcher()

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
    save_and_emit_temp(machine, float(data['temp']), uptime_seconds)
    save_machine_info(
        machine,
        data.get('asset_tag'),
        data.get('serial_number'),
        data.get('model'),
        data.get('companion_version'),
    )

    response_payload = {"status": "success"}
    latest_version = get_latest_companion_version()
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

# ================================
# LOGIN PAGE
# ================================
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign in - Temp Monitor</title>
<link rel="icon" href="{{ url_for('static', filename='thermometer.png') }}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
<style>
:root {
    --bg: #0f172a; --card: #1e293b; --accent: #22c55e;
    --text: #e2e8f0; --muted: #94a3b8;
}
* { box-sizing: border-box; font-family: 'Inter', sans-serif; }
body {
    background: linear-gradient(135deg, #0f172a, #020617); color: var(--text);
    height: 100vh; margin: 0; display: flex; align-items: center; justify-content: center;
}
.card {
    background: var(--card); padding: 40px; border-radius: 16px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.4); text-align: center; max-width: 340px;
}
h1 { font-size: 20px; font-weight: 600; margin-bottom: 8px; }
p { color: var(--muted); font-size: 14px; margin-bottom: 24px; }
.google-btn {
    display: inline-flex; align-items: center; gap: 10px; background: #fff; color: #1f1f1f;
    padding: 10px 20px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 14px;
}
.google-btn:hover { background: #f0f0f0; }
</style>
</head>
<body>
    <div class="card">
        <h1>Temp Monitor</h1>
        <p>Sign in with an authorized Google account to view the dashboard.</p>
        <a class="google-btn" href="{{ url_for('login_google') }}">
            <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.81.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.95v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.97 10.72A5.4 5.4 0 0 1 3.68 9c0-.6.1-1.18.29-1.72V4.95H.95A9 9 0 0 0 0 9c0 1.45.35 2.83.95 4.05l3.02-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.51.45 3.44 1.35l2.59-2.59C13.46.89 11.43 0 9 0A9 9 0 0 0 .95 4.95l3.02 2.33C4.68 5.16 6.66 3.58 9 3.58z"/></svg>
            Sign in with Google
        </a>
    </div>
</body>
</html>
"""

# ================================
# WEB DASHBOARD
# ================================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Multi-Node CPU Monitor</title>
<link rel="icon" href="{{ url_for('static', filename='thermometer.png') }}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
<!-- Socket.IO -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.js"></script>

<style>
:root {
    --bg: #0f172a; --card: #1e293b; --accent: #22c55e;
    --text: #e2e8f0; --muted: #94a3b8; --danger: #ef4444;
}
* { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }
body { background: linear-gradient(135deg, #0f172a, #020617); color: var(--text); padding: 30px; }
.container { max-width: 1200px; margin: auto; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; }
h1 { font-size: 28px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 12px; font-size: 14px; color: var(--muted); }
.nav-link { color: #93c5fd; text-decoration: none; border: 1px solid #334155; padding: 6px 10px; border-radius: 10px; }
.nav-link:hover { color: #bfdbfe; border-color: #475569; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 20px;}
.card {
    background: var(--card); padding: 20px; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.4);
    transition: 0.15s; cursor: pointer; border: 1px solid transparent;
}
.card:hover { border-color: #475569; transform: translateY(-2px); }
.card h2 { font-size: 16px; margin-bottom: 5px; color: var(--muted); font-weight: 400; }
.stat { font-size: 32px; font-weight: 600; margin-bottom: 8px; transition: color 0.3s; }
.label { font-size: 13px; color: var(--muted); }
.empty-state { color: var(--muted); font-size: 14px; }

/* Overheat Animations */
.overheat { background: #450a0a; border-color: var(--danger); animation: pulse 1s infinite; }
.overheat .stat { color: var(--danger); }
@keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); }
    70% { box-shadow: 0 0 0 15px rgba(239, 68, 68, 0); }
    100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
}
</style>
</head>
<body>

<div class="container">
    <header>
        <h1>Live CPU Monitor</h1>
        <div class="header-right">
            <a class="nav-link" href="/history">Daily Summary</a>
            <span id="socket-status" style="color: #eab308;">Connecting...</span>
            <span>Hub v{{ hub_version }}</span>
            <span>{{ session.user.email }}</span>
            <a class="nav-link" href="{{ url_for('logout') }}">Sign out</a>
        </div>
    </header>

    <!-- Dynamic Machine Cards go here -->
    <div class="grid" id="machine-cards"></div>
    <div class="empty-state" id="empty-state" style="display:none;">No machines have reported in yet.</div>
</div>

<script>
    // Request Desktop Notifications
    if (Notification.permission !== "granted" && Notification.permission !== "denied") {
        Notification.requestPermission();
    }

    const socket = io({ transports: ['polling'], upgrade: false });
    const machineCards = document.getElementById('machine-cards');
    const emptyStateEl = document.getElementById('empty-state');

    function formatMachineInfo(info) {
        if (!info) return '';
        const parts = [];
        if (info.model) parts.push(info.model);
        if (info.serial_number) parts.push(`SN: ${info.serial_number}`);
        if (info.asset_tag) parts.push(`Asset: ${info.asset_tag}`);
        return parts.join(' • ');
    }

    function formatUptime(seconds) {
        const value = Number(seconds);
        if (!Number.isFinite(value)) return '--';
        const total = Math.max(0, Math.floor(value));
        const days = Math.floor(total / 86400);
        const hours = Math.floor((total % 86400) / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const parts = [];
        if (days) parts.push(`${days}d`);
        if (days || hours) parts.push(`${hours}h`);
        parts.push(`${minutes}m`);
        return parts.join(' ');
    }

    function goToMachine(machine) {
        window.location.href = '/machine/' + encodeURIComponent(machine);
    }

    // Helper: Create or update UI Card for a machine
    function updateMachineCard(machine, temp, threshold, uptimeSeconds, info) {
        let card = document.getElementById('card-' + machine);
        if (!card) {
            card = document.createElement('div');
            card.id = 'card-' + machine;
            card.className = 'card';
            card.innerHTML = `
                <h2>${machine}</h2>
                <div class="label" id="info-${machine}" style="display:none;"></div>
                <div class="stat" id="temp-${machine}">-- °C</div>
                <div class="label" id="uptime-${machine}">Uptime: --</div>
                <div class="label" id="status-${machine}">--</div>
            `;
            card.addEventListener('click', () => goToMachine(machine));
            machineCards.appendChild(card);
            emptyStateEl.style.display = 'none';
        }

        if (info) {
            const infoEl = document.getElementById('info-' + machine);
            const text = formatMachineInfo(info);
            infoEl.textContent = text;
            infoEl.style.display = text ? '' : 'none';
        }

        if (uptimeSeconds !== undefined && uptimeSeconds !== null) {
            document.getElementById('uptime-' + machine).textContent = `Uptime: ${formatUptime(uptimeSeconds)}`;
        }

        const statusEl = document.getElementById('status-' + machine);
        if (temp === undefined || temp === null) return;

        document.getElementById('temp-' + machine).innerText = Number(temp).toFixed(1) + ' °C';

        // Overheat Logic
        if (temp >= threshold) {
            if (!card.classList.contains('overheat')) {
                card.classList.add('overheat');
                statusEl.innerText = '🔥 OVERHEATING';
                statusEl.style.color = '#ef4444';
                if (Notification.permission === "granted") {
                    new Notification("CPU Overheat Alert!", {
                        body: `${machine} is overheating at ${temp}°C!`,
                        icon: "https://cdn-icons-png.flaticon.com/512/3248/3248139.png"
                    });
                }
            }
        } else {
            card.classList.remove('overheat');
            statusEl.innerText = 'Normal';
            statusEl.style.color = 'var(--muted)';
        }
    }

    async function refreshMachineInfo() {
        try {
            const resp = await fetch('/api/machines');
            if (!resp.ok) return;
            const rows = await resp.json();
            emptyStateEl.style.display = rows.length ? 'none' : 'block';
            for (const row of rows) {
                updateMachineCard(row.machine, row.temp, 85, row.uptime_seconds, row);
            }
        } catch (e) { /* non-critical, dashboard still works without it */ }
    }

    refreshMachineInfo();

    // Handle Live Socket Updates
    socket.on('connect', () => { document.getElementById('socket-status').innerText = 'Live 🟢'; document.getElementById('socket-status').style.color = '#22c55e'; });
    socket.on('disconnect', () => { document.getElementById('socket-status').innerText = 'Offline 🔴'; document.getElementById('socket-status').style.color = '#ef4444'; });

    socket.on('new_temp', (msg) => {
        updateMachineCard(msg.machine, msg.temp, msg.threshold, msg.uptime_seconds);
    });
</script>
</body>
</html>
"""

MACHINE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ machine }} - Temp Monitor</title>
<link rel="icon" href="{{ url_for('static', filename='thermometer.png') }}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.js"></script>
<style>
:root {
    --bg: #0f172a; --card: #1e293b; --accent: #22c55e;
    --text: #e2e8f0; --muted: #94a3b8; --danger: #ef4444;
}
* { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }
body { background: linear-gradient(135deg, #0f172a, #020617); color: var(--text); padding: 30px; }
.container { max-width: 1200px; margin: auto; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; flex-wrap: wrap; gap: 10px; }
h1 { font-size: 28px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 12px; font-size: 14px; color: var(--muted); }
.nav-link { color: #93c5fd; text-decoration: none; border: 1px solid #334155; padding: 6px 10px; border-radius: 10px; }
.nav-link:hover { color: #bfdbfe; border-color: #475569; }
.card { background: var(--card); padding: 20px; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.4); margin-bottom: 20px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 20px; }
.stat-card h2 { font-size: 14px; margin-bottom: 6px; color: var(--muted); font-weight: 400; }
.stat { font-size: 32px; font-weight: 600; }
.label { font-size: 13px; color: var(--muted); }
.overheat .stat { color: var(--danger); }
.overheat { background: #450a0a; border: 1px solid var(--danger); }
.identity-line { font-size: 14px; color: var(--muted); margin-top: -4px; margin-bottom: 20px; }
.chart-tools { display: flex; gap: 8px; margin-bottom: 10px; align-items: center; flex-wrap: wrap; }
.chart-tools button, .chart-tools select, .chart-tools input[type="date"] {
    background: #0b1220; color: var(--text); border: 1px solid #334155;
    border-radius: 10px; padding: 6px 10px; cursor: pointer;
}
.chart-tools button:hover { border-color: #64748b; }
.chart-tools label { color: var(--muted); font-size: 13px; }
.chart-tools input[type="checkbox"] { width: 16px; height: 16px; accent-color: #22c55e; cursor: pointer; }
.chart-container { position: relative; height: 420px; width: 100%; }
</style>
</head>
<body>

<div class="container">
    <header>
        <h1>{{ machine }}</h1>
        <div class="header-right">
            <a class="nav-link" href="/">All machines</a>
            <a class="nav-link" href="/history">Daily Summary</a>
            <span id="socket-status" style="color: #eab308;">Connecting...</span>
            <span>Hub v{{ hub_version }}</span>
            <a class="nav-link" href="{{ url_for('logout') }}">Sign out</a>
        </div>
    </header>

    <div class="grid">
        <div class="card stat-card" id="temp-card">
            <h2>Live Temperature</h2>
            <div class="stat" id="stat-temp">-- °C</div>
            <div class="label" id="stat-status">--</div>
        </div>
        <div class="card stat-card">
            <h2>Uptime</h2>
            <div class="stat" id="stat-uptime">--</div>
        </div>
        <div class="card stat-card">
            <h2>Companion Version</h2>
            <div class="stat" id="stat-version">--</div>
        </div>
        <div class="card stat-card">
            <h2>Identity</h2>
            <div class="label" id="stat-model">Model: --</div>
            <div class="label" id="stat-serial">Serial: --</div>
            <div class="label" id="stat-asset">Asset tag: --</div>
        </div>
    </div>

    <div class="card">
        <h2 style="margin-bottom: 10px;">Temperature History</h2>
        <div class="chart-tools">
            <label for="day-picker">Day:</label>
            <input id="day-picker" type="date">
            <label for="resolution">Resolution:</label>
            <select id="resolution">
                <option value="raw">Raw</option>
                <option value="10s">10s</option>
                <option value="1m">1m</option>
                <option value="5m">5m</option>
            </select>
            <span id="resolution-in-use" class="label">In use: --</span>
            <input id="dynamic-resolution" type="checkbox" checked>
            <label for="dynamic-resolution">Dynamic on zoom</label>
            <button id="zoom-in" type="button">Zoom In</button>
            <button id="zoom-out" type="button">Zoom Out</button>
            <button id="reset-zoom" type="button">Reset Zoom</button>
        </div>
        <div class="chart-container">
            <canvas id="tempChart"></canvas>
        </div>
        <div id="no-data" class="label" style="margin-top: 12px; display: none;">No readings found for this day.</div>
    </div>
</div>

<script>
    const MACHINE = decodeURIComponent(window.location.pathname.split('/').pop());
    const OVERHEAT_THRESHOLD = {{ overheat_threshold }};

    const zoomPlugin = window['chartjs-plugin-zoom'];
    if (zoomPlugin) Chart.register(zoomPlugin.default || zoomPlugin);

    if (Notification.permission !== "granted" && Notification.permission !== "denied") {
        Notification.requestPermission();
    }

    const socket = io({ transports: ['polling'], upgrade: false });
    const dayPicker = document.getElementById('day-picker');
    const resolutionEl = document.getElementById('resolution');
    const resolutionInUseEl = document.getElementById('resolution-in-use');
    const dynamicResolutionEl = document.getElementById('dynamic-resolution');
    const noDataEl = document.getElementById('no-data');
    const tempCard = document.getElementById('temp-card');
    const statusEl = document.getElementById('stat-status');
    const VIEWPORT_RELOAD_DEBOUNCE_MS = 250;
    let viewportReloadTimer = null;
    let lastHistoryRequest = null;
    let historyLoadInFlight = false;
    let viewingToday = true;

    function getLocalDateString() {
        const now = new Date();
        const local = new Date(now.getTime() - (now.getTimezoneOffset() * 60000));
        return local.toISOString().split('T')[0];
    }

    function formatDateForApi(date) {
        const pad = (value) => String(value).padStart(2, '0');
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    }

    function getDayRange(dateString) {
        const start = new Date(`${dateString}T00:00:00`);
        if (Number.isNaN(start.getTime())) return null;
        const end = new Date(start.getTime() + 24 * 60 * 60 * 1000);
        return { startMs: start.getTime(), endMs: Math.min(end.getTime(), Date.now()) };
    }

    function chooseResolutionForSpan(spanMs) {
        if (spanMs <= 45 * 60 * 1000) return 'raw';
        if (spanMs <= 6 * 60 * 60 * 1000) return '10s';
        if (spanMs <= 18 * 60 * 60 * 1000) return '1m';
        return '5m';
    }

    function syncResolutionControl() {
        resolutionEl.disabled = dynamicResolutionEl.checked;
    }

    function getSelectedResolution(spanMs) {
        if (dynamicResolutionEl.checked) {
            const resolved = chooseResolutionForSpan(spanMs);
            resolutionEl.value = resolved;
            return resolved;
        }
        return (resolutionEl.value || '5m').trim().toLowerCase();
    }

    function setResolutionInUse(resolution) {
        resolutionInUseEl.textContent = `In use: ${resolution || '--'}`;
    }

    function toChartTimestamp(value) {
        if (typeof value === 'number' && Number.isFinite(value)) {
            return value > 1e12 ? value : value * 1000;
        }
        if (typeof value === 'string' && value.trim()) {
            const normalized = value.includes('T') ? value : value.replace(' ', 'T');
            const parsed = Date.parse(normalized);
            return Number.isNaN(parsed) ? null : parsed;
        }
        return null;
    }

    function buildHistoryUrl(date, minMs, maxMs, resolution) {
        const params = new URLSearchParams();
        params.set('machine', MACHINE);
        params.set('date', date);
        params.set('from', formatDateForApi(new Date(minMs)));
        params.set('to', formatDateForApi(new Date(maxMs)));
        params.set('resolution', resolution);
        params.set('limit', 'all');
        return `/api/history?${params.toString()}`;
    }

    function scheduleViewportReload() {
        if (!dynamicResolutionEl.checked || !selectedDayRange) return;
        if (viewportReloadTimer !== null) clearTimeout(viewportReloadTimer);
        viewportReloadTimer = setTimeout(() => {
            loadVisibleViewport();
            viewportReloadTimer = null;
        }, VIEWPORT_RELOAD_DEBOUNCE_MS);
    }

    const chart = new Chart(document.getElementById('tempChart').getContext('2d'), {
        type: 'line',
        data: {
            datasets: [{
                label: MACHINE,
                data: [],
                parsing: false,
                borderColor: '#3b82f6',
                backgroundColor: 'transparent',
                borderWidth: 2,
                tension: 0.25,
                pointRadius: 0,
                pointHoverRadius: 6,
                pointHitRadius: 20
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            normalized: true,
            animation: { duration: 0 },
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                x: { type: 'time', time: { tooltipFormat: 'HH:mm:ss' }, title: { display: true, text: 'Time' }, grid: { color: '#334155' } },
                y: { title: { display: true, text: 'Temperature (°C)' }, grid: { color: '#334155' } }
            },
            plugins: {
                decimation: { enabled: true, algorithm: 'lttb', samples: 400 },
                legend: { display: false },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: { label: (ctx) => `${ctx.parsed.y.toFixed(1)} °C` }
                },
                zoom: {
                    pan: { enabled: true, mode: 'x' },
                    zoom: {
                        wheel: { enabled: true },
                        pinch: { enabled: true },
                        drag: { enabled: true, backgroundColor: 'rgba(34, 197, 94, 0.15)' },
                        mode: 'x'
                    },
                    onZoom: () => scheduleViewportReload(),
                    onPan: () => scheduleViewportReload(),
                    onZoomComplete: () => scheduleViewportReload(),
                    onPanComplete: () => scheduleViewportReload()
                }
            }
        }
    });

    let selectedDayRange = null;

    async function loadHistoryRange(minMs, maxMs, resolution, resetZoom) {
        if (historyLoadInFlight || !dayPicker.value) return;
        historyLoadInFlight = true;
        try {
            const historyRes = await fetch(buildHistoryUrl(dayPicker.value, minMs, maxMs, resolution));
            const data = await historyRes.json();
            const points = data[MACHINE] || [];
            chart.data.datasets[0].data = points
                .map((point) => {
                    const x = toChartTimestamp(point.x ?? point.timestamp ?? point.ts_text);
                    const y = Number(point.y);
                    if (x === null || !Number.isFinite(y)) return null;
                    return { x, y };
                })
                .filter(Boolean);
            noDataEl.style.display = chart.data.datasets[0].data.length ? 'none' : 'block';
            setResolutionInUse(resolution);

            if (resetZoom) {
                chart.options.scales.x.min = undefined;
                chart.options.scales.x.max = undefined;
                chart.update('none');
                if (typeof chart.resetZoom === 'function') chart.resetZoom();
            } else {
                chart.options.scales.x.min = minMs;
                chart.options.scales.x.max = maxMs;
                chart.update('none');
            }
            lastHistoryRequest = { minMs, maxMs, resolution };
        } finally {
            historyLoadInFlight = false;
        }
    }

    async function loadVisibleViewport() {
        if (!selectedDayRange || historyLoadInFlight) return;
        const xScale = chart.scales?.x;
        if (!xScale) return;
        const scaleMin = Number(xScale.min);
        const scaleMax = Number(xScale.max);
        if (!Number.isFinite(scaleMin) || !Number.isFinite(scaleMax)) return;
        const minMs = Math.max(selectedDayRange.startMs, Math.floor(scaleMin));
        const maxMs = Math.min(selectedDayRange.endMs, Math.ceil(scaleMax));
        if (maxMs <= minMs) return;
        const resolution = getSelectedResolution(maxMs - minMs);
        if (
            lastHistoryRequest &&
            lastHistoryRequest.resolution === resolution &&
            Math.abs(lastHistoryRequest.minMs - minMs) < 10000 &&
            Math.abs(lastHistoryRequest.maxMs - maxMs) < 10000
        ) {
            return;
        }
        await loadHistoryRange(minMs, maxMs, resolution, false);
    }

    async function loadSelectedDay() {
        const date = dayPicker.value;
        if (!date) return;
        const range = getDayRange(date);
        if (!range) return;
        viewingToday = date === getLocalDateString();
        selectedDayRange = range;
        lastHistoryRequest = null;
        await loadHistoryRange(
            range.startMs,
            range.endMs,
            getSelectedResolution(range.endMs - range.startMs),
            true
        );
    }

    document.getElementById('zoom-in').addEventListener('click', () => {
        if (typeof chart.zoom === 'function') chart.zoom(1.2);
        scheduleViewportReload();
    });
    document.getElementById('zoom-out').addEventListener('click', () => {
        if (typeof chart.zoom === 'function') chart.zoom(0.8);
        scheduleViewportReload();
    });
    document.getElementById('reset-zoom').addEventListener('click', () => {
        if (!selectedDayRange) return;
        const resolution = getSelectedResolution(selectedDayRange.endMs - selectedDayRange.startMs);
        loadHistoryRange(selectedDayRange.startMs, selectedDayRange.endMs, resolution, true);
    });
    dayPicker.addEventListener('change', loadSelectedDay);
    resolutionEl.addEventListener('change', () => {
        if (dynamicResolutionEl.checked || !selectedDayRange) return;
        const resolution = getSelectedResolution(selectedDayRange.endMs - selectedDayRange.startMs);
        lastHistoryRequest = null;
        loadHistoryRange(selectedDayRange.startMs, selectedDayRange.endMs, resolution, true);
    });
    dynamicResolutionEl.addEventListener('change', () => {
        syncResolutionControl();
        if (!selectedDayRange) return;
        const resolution = getSelectedResolution(selectedDayRange.endMs - selectedDayRange.startMs);
        lastHistoryRequest = null;
        loadHistoryRange(selectedDayRange.startMs, selectedDayRange.endMs, resolution, true);
    });
    document.getElementById('tempChart').addEventListener('wheel', () => {
        scheduleViewportReload();
    }, { passive: true });

    function formatUptime(seconds) {
        const value = Number(seconds);
        if (!Number.isFinite(value)) return '--';
        const total = Math.max(0, Math.floor(value));
        const days = Math.floor(total / 86400);
        const hours = Math.floor((total % 86400) / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const parts = [];
        if (days) parts.push(`${days}d`);
        if (days || hours) parts.push(`${hours}h`);
        parts.push(`${minutes}m`);
        return parts.join(' ');
    }

    function applyTemp(temp) {
        if (temp === undefined || temp === null) return;
        document.getElementById('stat-temp').textContent = Number(temp).toFixed(1) + ' °C';
        if (temp >= OVERHEAT_THRESHOLD) {
            tempCard.classList.add('overheat');
            statusEl.textContent = '🔥 OVERHEATING';
            statusEl.style.color = '#ef4444';
        } else {
            tempCard.classList.remove('overheat');
            statusEl.textContent = 'Normal';
            statusEl.style.color = 'var(--muted)';
        }
    }

    async function loadMachineInfo() {
        try {
            const resp = await fetch('/api/machines/' + encodeURIComponent(MACHINE));
            if (!resp.ok) return;
            const info = await resp.json();
            applyTemp(info.temp);
            document.getElementById('stat-uptime').textContent = formatUptime(info.uptime_seconds);
            document.getElementById('stat-version').textContent = info.companion_version || '--';
            document.getElementById('stat-model').textContent = 'Model: ' + (info.model || '--');
            document.getElementById('stat-serial').textContent = 'Serial: ' + (info.serial_number || '--');
            document.getElementById('stat-asset').textContent = 'Asset tag: ' + (info.asset_tag || '--');
        } catch (e) { /* non-critical */ }
    }

    dayPicker.value = getLocalDateString();
    syncResolutionControl();
    loadSelectedDay();
    loadMachineInfo();

    socket.on('connect', () => { document.getElementById('socket-status').innerText = 'Live 🟢'; document.getElementById('socket-status').style.color = '#22c55e'; });
    socket.on('disconnect', () => { document.getElementById('socket-status').innerText = 'Offline 🔴'; document.getElementById('socket-status').style.color = '#ef4444'; });

    socket.on('new_temp', (msg) => {
        if (msg.machine !== MACHINE) return;
        applyTemp(msg.temp);
        if (msg.uptime_seconds !== undefined && msg.uptime_seconds !== null) {
            document.getElementById('stat-uptime').textContent = formatUptime(msg.uptime_seconds);
        }
        if (!viewingToday) return;

        const x = toChartTimestamp(msg.timestamp_ms ?? msg.timestamp_epoch ?? msg.timestamp);
        if (x === null) return;
        chart.data.datasets[0].data.push({ x, y: Number(msg.temp) });
        if (selectedDayRange) selectedDayRange.endMs = Math.max(selectedDayRange.endMs, x);
        noDataEl.style.display = 'none';
        chart.update('none');
    });
</script>
</body>
</html>
"""

HISTORY_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily CPU Summary</title>
<link rel="icon" href="{{ url_for('static', filename='thermometer.png') }}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom"></script>
<style>
:root {
    --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8;
}
* { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }
body { background: linear-gradient(135deg, #0f172a, #020617); color: var(--text); padding: 30px; }
.container { max-width: 1200px; margin: auto; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h1 { font-size: 28px; font-weight: 600; }
.back-link { color: #93c5fd; text-decoration: none; border: 1px solid #334155; padding: 8px 12px; border-radius: 10px; }
.back-link:hover { color: #bfdbfe; border-color: #475569; }
.card { background: var(--card); padding: 20px; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.4); }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 20px; }
.toolbar label { color: var(--muted); font-size: 14px; }
.toolbar input, .toolbar select, .toolbar button {
    background: #0b1220; color: var(--text); border: 1px solid #334155;
    border-radius: 10px; padding: 8px 12px;
}
.toolbar button { cursor: pointer; }
.toolbar button:hover { border-color: #64748b; }
.toolbar input[type="checkbox"] { width: 16px; height: 16px; accent-color: #22c55e; cursor: pointer; padding: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin-bottom: 20px; }
.stat { font-size: 34px; font-weight: 600; margin-bottom: 8px; }
.label { color: var(--muted); font-size: 13px; }
.chart-container { position: relative; height: 430px; width: 100%; margin-top: 10px; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid #334155; font-size: 14px; }
th { color: var(--muted); font-weight: 500; }
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>Daily Temperature Summary</h1>
        <div style="display:flex; align-items:center; gap:12px;">
            <span class="label">Hub v{{ hub_version }}</span>
            <a href="/" class="back-link">Live Dashboard</a>
        </div>
    </header>

    <div class="card toolbar">
        <label for="day-picker">Select day:</label>
        <input id="day-picker" type="date">
        <label for="daily-resolution">Resolution:</label>
        <select id="daily-resolution">
            <option value="raw">Raw</option>
            <option value="10s" selected>10s</option>
            <option value="1m">1m</option>
            <option value="5m">5m</option>
        </select>
        <span id="daily-resolution-in-use" class="label">In use: --</span>
        <input id="daily-dynamic-resolution" type="checkbox" checked>
        <label for="daily-dynamic-resolution">Dynamic on zoom</label>
        <button id="load-day">Load Day</button>
        <button id="zoom-in">Zoom In</button>
        <button id="zoom-out">Zoom Out</button>
        <button id="reset-zoom">Reset Zoom</button>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Overall Daily Average</h2>
            <div id="overall-avg" class="stat">-- °C</div>
            <div id="daily-meta" class="label">Select a day to load data.</div>
        </div>
        <div class="card">
            <h2>Average by Machine</h2>
            <table>
                <thead>
                    <tr><th>Machine</th><th>Average (°C)</th></tr>
                </thead>
                <tbody id="machine-avg-body">
                    <tr><td colspan="2" class="label">No data loaded.</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <div class="card">
        <h2>Daily Temperature Graph</h2>
        <div class="label">Use mouse wheel or drag to zoom. Hover points to see exact temperatures.</div>
        <div class="chart-container">
            <canvas id="dailyChart"></canvas>
        </div>
        <div id="no-data" class="label" style="margin-top: 12px; display: none;">No readings found for this day.</div>
    </div>
</div>

<script>
    const zoomPlugin = window['chartjs-plugin-zoom'];
    if (zoomPlugin) {
        Chart.register(zoomPlugin.default || zoomPlugin);
    }

    const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899', '#22d3ee', '#f97316'];
    const dayPicker = document.getElementById('day-picker');
    const dailyResolutionEl = document.getElementById('daily-resolution');
    const dailyResolutionInUseEl = document.getElementById('daily-resolution-in-use');
    const dailyDynamicResolutionEl = document.getElementById('daily-dynamic-resolution');
    const overallAvgEl = document.getElementById('overall-avg');
    const dailyMetaEl = document.getElementById('daily-meta');
    const machineAvgBody = document.getElementById('machine-avg-body');
    const noDataEl = document.getElementById('no-data');
    const VIEWPORT_RELOAD_DEBOUNCE_MS = 250;
    let selectedDayRange = null;
    let viewportReloadTimer = null;
    let lastHistoryRequest = null;
    let historyLoadInFlight = false;

    function getLocalDateString() {
        const now = new Date();
        const local = new Date(now.getTime() - (now.getTimezoneOffset() * 60000));
        return local.toISOString().split('T')[0];
    }

    function formatDateForApi(date) {
        const pad = (value) => String(value).padStart(2, '0');
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    }

    function getDayRange(dateString) {
        const start = new Date(`${dateString}T00:00:00`);
        if (Number.isNaN(start.getTime())) return null;
        const end = new Date(start.getTime() + 24 * 60 * 60 * 1000);
        return { startMs: start.getTime(), endMs: end.getTime() };
    }

    function chooseResolutionForSpan(spanMs) {
        if (spanMs <= 45 * 60 * 1000) return 'raw';
        if (spanMs <= 6 * 60 * 60 * 1000) return '10s';
        if (spanMs <= 18 * 60 * 60 * 1000) return '1m';
        return '5m';
    }

    function syncDailyResolutionControl() {
        if (!dailyResolutionEl || !dailyDynamicResolutionEl) return;
        dailyResolutionEl.disabled = dailyDynamicResolutionEl.checked;
    }

    function getSelectedDailyResolution(spanMs) {
        if (dailyDynamicResolutionEl?.checked) {
            const resolved = chooseResolutionForSpan(spanMs);
            if (dailyResolutionEl) dailyResolutionEl.value = resolved;
            return resolved;
        }
        const selected = (dailyResolutionEl?.value || '10s').trim().toLowerCase();
        if (selected === 'raw' || selected === '10s' || selected === '1m' || selected === '5m') {
            return selected;
        }
        return '10s';
    }

    function formatDailyResolutionLabel(resolution) {
        if (resolution === 'raw') return 'Raw';
        if (resolution === '10s') return '10s';
        if (resolution === '1m') return '1m';
        if (resolution === '5m') return '5m';
        return '--';
    }

    function setDailyResolutionInUse(resolution) {
        if (!dailyResolutionInUseEl) return;
        dailyResolutionInUseEl.textContent = `In use: ${formatDailyResolutionLabel(resolution)}`;
    }

    function buildHistoryUrl(date, minMs, maxMs, resolution) {
        const params = new URLSearchParams();
        params.set('date', date);
        params.set('from', formatDateForApi(new Date(minMs)));
        params.set('to', formatDateForApi(new Date(maxMs)));
        params.set('resolution', resolution);
        params.set('limit', 'all');
        return `/api/history?${params.toString()}`;
    }

    function scheduleViewportReload() {
        if (!dailyDynamicResolutionEl?.checked) return;
        if (!selectedDayRange) return;
        if (viewportReloadTimer !== null) clearTimeout(viewportReloadTimer);
        viewportReloadTimer = setTimeout(() => {
            loadVisibleViewport();
            viewportReloadTimer = null;
        }, VIEWPORT_RELOAD_DEBOUNCE_MS);
    }

    const dailyChart = new Chart(document.getElementById('dailyChart').getContext('2d'), {
        type: 'line',
        data: { datasets: [] },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            normalized: true,
            animation: { duration: 0 },
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                x: { type: 'time', time: { tooltipFormat: 'HH:mm:ss' }, title: { display: true, text: 'Time' }, grid: { color: '#334155' } },
                y: { title: { display: true, text: 'Temperature (°C)' }, grid: { color: '#334155' } }
            },
            plugins: {
                decimation: {
                    enabled: true,
                    algorithm: 'lttb',
                    samples: 400
                },
                legend: { labels: { color: '#e2e8f0' } },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} °C`
                    }
                },
                zoom: {
                    pan: { enabled: true, mode: 'x' },
                    zoom: {
                        wheel: { enabled: true },
                        pinch: { enabled: true },
                        drag: { enabled: true, backgroundColor: 'rgba(34, 197, 94, 0.15)' },
                        mode: 'x'
                    },
                    onZoom: () => scheduleViewportReload(),
                    onPan: () => scheduleViewportReload(),
                    onZoomComplete: () => scheduleViewportReload(),
                    onPanComplete: () => scheduleViewportReload()
                }
            }
        }
    });

    function updateMachineAverages(machineAverages) {
        machineAvgBody.innerHTML = '';
        const entries = Object.entries(machineAverages || {});
        if (entries.length === 0) {
            const row = document.createElement('tr');
            const cell = document.createElement('td');
            cell.colSpan = 2;
            cell.className = 'label';
            cell.textContent = 'No data for selected day.';
            row.appendChild(cell);
            machineAvgBody.appendChild(row);
            return;
        }

        entries.sort((a, b) => a[0].localeCompare(b[0]));
        for (const [machine, avg] of entries) {
            const row = document.createElement('tr');
            const machineCell = document.createElement('td');
            machineCell.textContent = machine;
            const avgCell = document.createElement('td');
            avgCell.textContent = Number(avg).toFixed(1);
            row.appendChild(machineCell);
            row.appendChild(avgCell);
            machineAvgBody.appendChild(row);
        }
    }

    function setDailyDatasets(history) {
        const toChartTimestamp = (value) => {
            if (typeof value === 'number' && Number.isFinite(value)) {
                return value > 1e12 ? value : value * 1000;
            }
            if (typeof value === 'string' && value.trim()) {
                const normalized = value.includes('T') ? value : value.replace(' ', 'T');
                const parsed = Date.parse(normalized);
                return Number.isNaN(parsed) ? null : parsed;
            }
            return null;
        };

        dailyChart.data.datasets = Object.entries(history).map(([machine, points], index) => ({
            label: machine,
            parsing: false,
            data: points
                .map((point) => {
                    const x = toChartTimestamp(point.x ?? point.timestamp ?? point.ts_text);
                    const y = Number(point.y);
                    if (x === null || !Number.isFinite(y)) return null;
                    return { x, y };
                })
                .filter(Boolean),
            borderColor: colors[index % colors.length],
            backgroundColor: 'transparent',
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 7,
            pointHitRadius: 20,
            tension: 0.25
        }));
        noDataEl.style.display = dailyChart.data.datasets.length ? 'none' : 'block';
    }

    async function loadDailySummary(date) {
        const summaryRes = await fetch(`/api/daily_summary?date=${encodeURIComponent(date)}`);
        const summary = await summaryRes.json();

        if (summary.overall_avg === null) {
            overallAvgEl.textContent = '-- °C';
            dailyMetaEl.textContent = `${date} • 0 machines • 0 readings`;
        } else {
            overallAvgEl.textContent = `${Number(summary.overall_avg).toFixed(1)} °C`;
            dailyMetaEl.textContent = `${date} • ${summary.machine_count} machines • ${summary.reading_count} readings`;
        }
        updateMachineAverages(summary.machine_averages);
    }

    async function loadHistoryRange(minMs, maxMs, resolution, resetZoom) {
        if (historyLoadInFlight || !dayPicker.value) return;
        historyLoadInFlight = true;
        try {
            const historyRes = await fetch(buildHistoryUrl(dayPicker.value, minMs, maxMs, resolution));
            const history = await historyRes.json();
            setDailyDatasets(history);
            setDailyResolutionInUse(resolution);

            if (resetZoom) {
                dailyChart.options.scales.x.min = undefined;
                dailyChart.options.scales.x.max = undefined;
                dailyChart.update('none');
                if (typeof dailyChart.resetZoom === 'function') dailyChart.resetZoom();
            } else {
                dailyChart.options.scales.x.min = minMs;
                dailyChart.options.scales.x.max = maxMs;
                dailyChart.update('none');
            }

            lastHistoryRequest = { minMs, maxMs, resolution };
        } finally {
            historyLoadInFlight = false;
        }
    }

    async function loadVisibleViewport() {
        if (!selectedDayRange || historyLoadInFlight) return;
        const xScale = dailyChart.scales?.x;
        if (!xScale) return;

        const scaleMin = Number(xScale.min);
        const scaleMax = Number(xScale.max);
        if (!Number.isFinite(scaleMin) || !Number.isFinite(scaleMax)) return;

        const minMs = Math.max(selectedDayRange.startMs, Math.floor(scaleMin));
        const maxMs = Math.min(selectedDayRange.endMs, Math.ceil(scaleMax));
        if (maxMs <= minMs) return;

        const resolution = getSelectedDailyResolution(maxMs - minMs);
        if (
            lastHistoryRequest &&
            lastHistoryRequest.resolution === resolution &&
            Math.abs(lastHistoryRequest.minMs - minMs) < 10000 &&
            Math.abs(lastHistoryRequest.maxMs - maxMs) < 10000
        ) {
            return;
        }

        await loadHistoryRange(minMs, maxMs, resolution, false);
    }

    async function loadSelectedDay() {
        const date = dayPicker.value;
        if (!date) return;

        const range = getDayRange(date);
        if (!range) return;

        selectedDayRange = range;
        lastHistoryRequest = null;

        await Promise.all([
            loadDailySummary(date),
            loadHistoryRange(
                range.startMs,
                range.endMs,
                getSelectedDailyResolution(range.endMs - range.startMs),
                true
            )
        ]);
    }

    document.getElementById('load-day').addEventListener('click', loadSelectedDay);
    dailyResolutionEl.addEventListener('change', () => {
        if (dailyDynamicResolutionEl?.checked) return;
        if (!selectedDayRange) return;
        const resolution = getSelectedDailyResolution(selectedDayRange.endMs - selectedDayRange.startMs);
        lastHistoryRequest = null;
        loadHistoryRange(selectedDayRange.startMs, selectedDayRange.endMs, resolution, true);
    });
    dailyDynamicResolutionEl.addEventListener('change', () => {
        syncDailyResolutionControl();
        if (!selectedDayRange) return;
        const resolution = getSelectedDailyResolution(selectedDayRange.endMs - selectedDayRange.startMs);
        lastHistoryRequest = null;
        loadHistoryRange(selectedDayRange.startMs, selectedDayRange.endMs, resolution, true);
    });
    document.getElementById('zoom-in').addEventListener('click', () => {
        if (typeof dailyChart.zoom === 'function') dailyChart.zoom(1.2);
        scheduleViewportReload();
    });
    document.getElementById('zoom-out').addEventListener('click', () => {
        if (typeof dailyChart.zoom === 'function') dailyChart.zoom(0.8);
        scheduleViewportReload();
    });
    document.getElementById('reset-zoom').addEventListener('click', () => {
        if (!selectedDayRange) return;
        const resolution = getSelectedDailyResolution(selectedDayRange.endMs - selectedDayRange.startMs);
        loadHistoryRange(selectedDayRange.startMs, selectedDayRange.endMs, resolution, true);
    });

    dayPicker.value = getLocalDateString();
    syncDailyResolutionControl();
    loadSelectedDay();
    document.getElementById('dailyChart').addEventListener('wheel', () => {
        scheduleViewportReload();
    }, { passive: true });
</script>
</body>
</html>
"""

@app.route("/")
@login_required
def index():
    return render_template_string(HTML, hub_version=HUB_VERSION)

@app.route("/history")
@login_required
def history_page():
    return render_template_string(HISTORY_HTML, hub_version=HUB_VERSION)

@app.route("/machine/<machine>")
@login_required
def machine_page(machine):
    return render_template_string(
        MACHINE_HTML, machine=machine, overheat_threshold=OVERHEAT_THRESHOLD, hub_version=HUB_VERSION
    )

# ================================
# START
# ================================
application = app

if __name__ == "__main__":
    # Start local logger in background
    start_local_logger()
    
    # Use socketio.run instead of app.run
    print(f"Starting hub on {LOCAL_MACHINE}...")
    socketio.run(app, host="0.0.0.0", port=3001, debug=False, allow_unsafe_werkzeug=True)
