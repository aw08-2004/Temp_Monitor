import os
import time
import threading
import csv
import socket
import sqlite3
import queue
from collections import defaultdict, deque
from datetime import datetime, timedelta
import wmi
import pythoncom
from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO
import requests

# ================================
# CONFIG
# ================================
CHECK_INTERVAL = 5
OVERHEAT_THRESHOLD = 85
SPIKE_THRESHOLD = 10
LHM_URL = "http://localhost:8085/data.json"

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

# ================================
# WEB & WEBSOCKET SETUP
# ================================
app = Flask(__name__)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    transports=["polling"],
    allow_upgrades=False
)

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

def save_and_emit_temp(machine, temp):
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

    # Emit via WebSocket
    socketio.emit('new_temp', {
        'machine': machine_name,
        'timestamp': timestamp_str,
        'temp': temp_value,
        'threshold': OVERHEAT_THRESHOLD
    })

def save_machine_info(machine, asset_tag, serial_number, model):
    machine_name = str(machine).strip()
    asset_tag = (str(asset_tag).strip() or None) if asset_tag else None
    serial_number = (str(serial_number).strip() or None) if serial_number else None
    model = (str(model).strip() or None) if model else None
    if not machine_name or not any([asset_tag, serial_number, model]):
        return

    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO machine_info(machine, asset_tag, serial_number, model, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(machine) DO UPDATE SET
                asset_tag = excluded.asset_tag,
                serial_number = excluded.serial_number,
                model = excluded.model,
                updated_at = excluded.updated_at
            """,
            (machine_name, asset_tag, serial_number, model, to_timestamp_str(datetime.now())),
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
                
                save_and_emit_temp(LOCAL_MACHINE, temp)
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
    save_and_emit_temp(machine, float(data['temp']))
    save_machine_info(machine, data.get('asset_tag'), data.get('serial_number'), data.get('model'))
    return jsonify({"status": "success"}), 200

@app.route('/api/machines')
def get_machines():
    """Machine identity info (asset tag / serial number / model) reported by companions."""
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT machine, asset_tag, serial_number, model, updated_at FROM machine_info ORDER BY machine ASC"
        ).fetchall()
    return jsonify([dict(row) for row in rows])

@app.route('/api/history')
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
<!-- Chart.js and date adapter for time scales -->
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom"></script>
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
.card { background: var(--card); padding: 20px; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.4); transition: 0.3s; }
.card h2 { font-size: 16px; margin-bottom: 5px; color: var(--muted); font-weight: 400; }
.stat { font-size: 32px; font-weight: 600; margin-bottom: 8px; transition: color 0.3s; }
.label { font-size: 13px; color: var(--muted); }

/* Overheat Animations */
.overheat { background: #450a0a; border: 1px solid var(--danger); animation: pulse 1s infinite; }
.overheat .stat { color: var(--danger); }
@keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); }
    70% { box-shadow: 0 0 0 15px rgba(239, 68, 68, 0); }
    100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
}
.chart-tools { display: flex; gap: 8px; margin-bottom: 10px; align-items: center; flex-wrap: wrap; }
.chart-tools button, .chart-tools select {
    background: #0b1220; color: var(--text); border: 1px solid #334155;
    border-radius: 10px; padding: 6px 10px; cursor: pointer;
}
.chart-tools button:hover { border-color: #64748b; }
.chart-tools label { color: var(--muted); font-size: 13px; }
.chart-tools input[type="checkbox"] { width: 16px; height: 16px; accent-color: #22c55e; cursor: pointer; }
.chart-container { position: relative; height: 400px; width: 100%; }
</style>
</head>
<body>

<div class="container">
    <header>
        <h1>Live CPU Monitor</h1>
        <div class="header-right">
            <a class="nav-link" href="/history">Daily Summary</a>
            <span id="socket-status" style="color: #eab308;">Connecting...</span>
        </div>
    </header>

    <!-- Dynamic Machine Cards go here -->
    <div class="grid" id="machine-cards"></div>

    <div class="card">
        <h2>Live Temperature Graph</h2>
        <div class="chart-tools">
            <label for="live-resolution">Resolution:</label>
            <select id="live-resolution">
                <option value="raw">Raw</option>
                <option value="10s">10s</option>
                <option value="1m">1m</option>
                <option value="5m" selected>5m</option>
            </select>
            <span id="live-resolution-in-use" class="label">In use: --</span>
            <input id="live-dynamic-resolution" type="checkbox" checked>
            <label for="live-dynamic-resolution">Dynamic on zoom</label>
            <button id="live-zoom-in" type="button">Zoom In</button>
            <button id="live-zoom-out" type="button">Zoom Out</button>
            <button id="live-reset-zoom" type="button">Reset Zoom</button>
        </div>
        <div class="chart-container">
            <canvas id="tempChart"></canvas>
        </div>
    </div>
</div>

<script>
    const zoomPlugin = window['chartjs-plugin-zoom'];
    if (zoomPlugin) {
        Chart.register(zoomPlugin.default || zoomPlugin);
    }

    // Request Desktop Notifications
    if (Notification.permission !== "granted" && Notification.permission !== "denied") {
        Notification.requestPermission();
    }

    const socket = io({ transports: ['polling'], upgrade: false });
    const machineCards = document.getElementById('machine-cards');
    const liveResolutionEl = document.getElementById('live-resolution');
    const liveResolutionInUseEl = document.getElementById('live-resolution-in-use');
    const liveDynamicResolutionEl = document.getElementById('live-dynamic-resolution');
    let chart;
    const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899'];
    let colorIndex = 0;
    const LIVE_UPDATE_INTERVAL_MS = 250;
    const LIVE_VIEWPORT_RELOAD_DEBOUNCE_MS = 250;
    let liveChartUpdateTimer = null;
    let liveViewportReloadTimer = null;
    let liveHistoryLoadInFlight = false;
    let lastLiveHistoryRequest = null;

    function formatDateForApi(date) {
        const pad = (value) => String(value).padStart(2, '0');
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    }

    function scheduleLiveChartUpdate() {
        if (liveChartUpdateTimer !== null) return;
        liveChartUpdateTimer = setTimeout(() => {
            pruneLiveDatasetsToToday();
            chart.update('none');
            liveChartUpdateTimer = null;
        }, LIVE_UPDATE_INTERVAL_MS);
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

    function chooseLiveResolutionForSpan(spanMs) {
        if (spanMs <= 45 * 60 * 1000) return 'raw';
        if (spanMs <= 6 * 60 * 60 * 1000) return '10s';
        if (spanMs <= 18 * 60 * 60 * 1000) return '1m';
        return '5m';
    }

    function syncLiveResolutionControl() {
        if (!liveResolutionEl || !liveDynamicResolutionEl) return;
        liveResolutionEl.disabled = liveDynamicResolutionEl.checked;
    }

    function getSelectedLiveResolution() {
        const selected = (liveResolutionEl?.value || '5m').trim().toLowerCase();
        if (selected === 'raw' || selected === '10s' || selected === '1m' || selected === '5m') {
            return selected;
        }
        return '5m';
    }

    function formatLiveResolutionLabel(resolution) {
        if (resolution === 'raw') return 'Raw';
        if (resolution === '10s') return '10s';
        if (resolution === '1m') return '1m';
        if (resolution === '5m') return '5m';
        return '--';
    }

    function setLiveResolutionInUse(resolution) {
        if (!liveResolutionInUseEl) return;
        liveResolutionInUseEl.textContent = `In use: ${formatLiveResolutionLabel(resolution)}`;
    }

    function getRequestedLiveResolution(spanMs) {
        if (liveDynamicResolutionEl?.checked) {
            const resolved = chooseLiveResolutionForSpan(spanMs);
            if (liveResolutionEl) liveResolutionEl.value = resolved;
            return resolved;
        }
        return getSelectedLiveResolution();
    }

    function getStartOfTodayMs() {
        const start = new Date();
        start.setHours(0, 0, 0, 0);
        return start.getTime();
    }

    function pruneLiveDatasetsToToday() {
        const startOfTodayMs = getStartOfTodayMs();
        for (const dataset of chart.data.datasets) {
            dataset.data = dataset.data.filter((point) => {
                const x = Number(point?.x);
                return Number.isFinite(x) && x >= startOfTodayMs;
            });
        }
    }

    function getLiveDefaultRange() {
        return { minMs: getStartOfTodayMs(), maxMs: Date.now() };
    }

    function getLiveViewportRange() {
        const defaultRange = getLiveDefaultRange();
        const xScale = chart?.scales?.x;
        if (!xScale) return defaultRange;
        const scaleMin = Number(xScale.min);
        const scaleMax = Number(xScale.max);
        if (!Number.isFinite(scaleMin) || !Number.isFinite(scaleMax)) return defaultRange;
        const minMs = Math.max(defaultRange.minMs, Math.floor(scaleMin));
        const maxMs = Math.min(defaultRange.maxMs, Math.ceil(scaleMax));
        if (maxMs <= minMs) return defaultRange;
        return { minMs, maxMs };
    }

    function buildLiveHistoryUrl(minMs, maxMs, resolution) {
        const from = new Date(minMs);
        const to = new Date(maxMs);
        return `/api/history?from=${encodeURIComponent(formatDateForApi(from))}&to=${encodeURIComponent(formatDateForApi(to))}&resolution=${encodeURIComponent(resolution)}&limit=all`;
    }

    function scheduleLiveViewportReload() {
        if (!liveDynamicResolutionEl?.checked) return;
        if (liveViewportReloadTimer !== null) clearTimeout(liveViewportReloadTimer);
        liveViewportReloadTimer = setTimeout(() => {
            loadLiveViewportRange();
            liveViewportReloadTimer = null;
        }, LIVE_VIEWPORT_RELOAD_DEBOUNCE_MS);
    }

    // Initialize Chart.js
    const ctx = document.getElementById('tempChart').getContext('2d');
    chart = new Chart(ctx, {
        type: 'line',
        data: { datasets: [] },
        options: {
            responsive: true, maintainAspectRatio: false,
            normalized: true,
            animation: { duration: 0 }, // Disable animation for performance on live updates
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                x: { type: 'time', time: { tooltipFormat: 'HH:mm:ss' }, title: { display: true, text: 'Time' }, grid: {color: '#334155'} },
                y: { title: { display: true, text: 'Temperature (°C)' }, grid: {color: '#334155'} }
            },
            plugins: {
                decimation: {
                    enabled: true,
                    algorithm: 'lttb',
                    samples: 300
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
                    onZoom: () => scheduleLiveViewportReload(),
                    onPan: () => scheduleLiveViewportReload(),
                    onZoomComplete: () => scheduleLiveViewportReload(),
                    onPanComplete: () => scheduleLiveViewportReload()
                }
            }
        }
    });

    document.getElementById('live-zoom-in').addEventListener('click', () => {
        if (typeof chart.zoom === 'function') chart.zoom(1.2);
        scheduleLiveViewportReload();
    });
    document.getElementById('live-zoom-out').addEventListener('click', () => {
        if (typeof chart.zoom === 'function') chart.zoom(0.8);
        scheduleLiveViewportReload();
    });
    document.getElementById('live-reset-zoom').addEventListener('click', () => {
        if (typeof chart.resetZoom === 'function') chart.resetZoom();
        lastLiveHistoryRequest = null;
        loadLiveHistory();
    });

    // Helper: Create or update UI Card for a machine
    function updateMachineCard(machine, temp, threshold) {
        let card = document.getElementById('card-' + machine);
        if (!card) {
            card = document.createElement('div');
            card.id = 'card-' + machine;
            card.className = 'card';
            card.innerHTML = `
                <h2>${machine}</h2>
                <div class="stat" id="temp-${machine}">-- °C</div>
                <div class="label" id="status-${machine}">Online</div>
            `;
            machineCards.appendChild(card);
        }

        const tempEl = document.getElementById('temp-' + machine);
        const statusEl = document.getElementById('status-' + machine);
        tempEl.innerText = temp.toFixed(1) + ' °C';

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

    // Helper: Add dataset to chart
    function getOrCreateDataset(machine) {
        let dataset = chart.data.datasets.find(ds => ds.label === machine);
        if (!dataset) {
            dataset = {
                label: machine,
                data: [],
                parsing: false,
                borderColor: colors[colorIndex % colors.length],
                backgroundColor: 'transparent',
                borderWidth: 2,
                tension: 0.3,
                pointRadius: 0, // Hide points for cleaner line
                pointHoverRadius: 6,
                pointHitRadius: 20
            };
            chart.data.datasets.push(dataset);
            colorIndex++;
        }
        return dataset;
    }

    async function loadLiveHistoryRange(minMs, maxMs, resolution, resetZoom) {
        if (liveHistoryLoadInFlight) return;
        liveHistoryLoadInFlight = true;
        try {
            const historyRes = await fetch(buildLiveHistoryUrl(minMs, maxMs, resolution));
            const data = await historyRes.json();
            chart.data.datasets = [];
            colorIndex = 0;
            for (const [machine, points] of Object.entries(data)) {
                const dataset = getOrCreateDataset(machine);
                dataset.data = points
                    .map((point) => {
                        const x = toChartTimestamp(point.x ?? point.timestamp ?? point.ts_text);
                        const y = Number(point.y);
                        if (x === null || !Number.isFinite(y)) return null;
                        return { x, y };
                    })
                    .filter(Boolean);

                // Update card with latest temp from history
                if (points.length > 0) {
                    updateMachineCard(machine, points[points.length - 1].y, 85); // fallback threshold
                }
            }
            pruneLiveDatasetsToToday();
            setLiveResolutionInUse(resolution);
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
            lastLiveHistoryRequest = { minMs, maxMs, resolution };
        } finally {
            liveHistoryLoadInFlight = false;
        }
    }

    async function loadLiveHistory() {
        const { minMs, maxMs } = getLiveDefaultRange();
        const resolution = getRequestedLiveResolution(maxMs - minMs);
        await loadLiveHistoryRange(minMs, maxMs, resolution, true);
    }

    async function loadLiveViewportRange() {
        if (!liveDynamicResolutionEl?.checked) return;
        const { minMs, maxMs } = getLiveViewportRange();
        if (maxMs <= minMs) return;
        const resolution = getRequestedLiveResolution(maxMs - minMs);
        if (
            lastLiveHistoryRequest &&
            lastLiveHistoryRequest.resolution === resolution &&
            Math.abs(lastLiveHistoryRequest.minMs - minMs) < 10000 &&
            Math.abs(lastLiveHistoryRequest.maxMs - maxMs) < 10000
        ) {
            return;
        }
        await loadLiveHistoryRange(minMs, maxMs, resolution, false);
    }

    liveDynamicResolutionEl.addEventListener('change', () => {
        syncLiveResolutionControl();
        lastLiveHistoryRequest = null;
        if (liveDynamicResolutionEl.checked) {
            loadLiveViewportRange();
        } else {
            loadLiveHistory();
        }
    });
    liveResolutionEl.addEventListener('change', () => {
        if (liveDynamicResolutionEl?.checked) return;
        lastLiveHistoryRequest = null;
        loadLiveHistory();
    });
    syncLiveResolutionControl();
    loadLiveHistory();
    document.getElementById('tempChart').addEventListener('wheel', () => {
        scheduleLiveViewportReload();
    }, { passive: true });

    // Handle Live Socket Updates
    socket.on('connect', () => { document.getElementById('socket-status').innerText = 'Live 🟢'; document.getElementById('socket-status').style.color = '#22c55e'; });
    socket.on('disconnect', () => { document.getElementById('socket-status').innerText = 'Offline 🔴'; document.getElementById('socket-status').style.color = '#ef4444'; });

    socket.on('new_temp', (msg) => {
        updateMachineCard(msg.machine, msg.temp, msg.threshold);
        
        const x = toChartTimestamp(msg.timestamp_ms ?? msg.timestamp_epoch ?? msg.timestamp);
        if (x === null) return;

        const dataset = getOrCreateDataset(msg.machine);
        dataset.data.push({ x, y: Number(msg.temp) });
        scheduleLiveChartUpdate();
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
        <a href="/" class="back-link">Live Dashboard</a>
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
def index():
    return render_template_string(HTML)

@app.route("/history")
def history_page():
    return render_template_string(HISTORY_HTML)

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
