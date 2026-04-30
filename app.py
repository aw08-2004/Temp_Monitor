import os
import time
import threading
import csv
import socket
from datetime import datetime
import pandas as pd
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

# Identify the host machine
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

def save_and_emit_temp(machine, temp):
    now = datetime.now()
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    log_file = get_log_path()

    # Create CSV with headers if it doesn't exist
    if not os.path.exists(log_file):
        with open(log_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "machine", "temperature"])

    # Append data
    with open(log_file, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([timestamp_str, machine, temp])

    # Emit via WebSocket
    socketio.emit('new_temp', {
        'machine': machine,
        'timestamp': timestamp_str,
        'temp': temp,
        'threshold': OVERHEAT_THRESHOLD
    })

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
                    print(f"⚠️ SPIKE: {last_temp} → {temp}")
                if temp >= OVERHEAT_THRESHOLD:
                    print(f"🔥 OVERHEATING: {temp}°C")
                
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
    
    save_and_emit_temp(data['machine'], float(data['temp']))
    return jsonify({"status": "success"}), 200

@app.route('/api/history')
def get_history():
    """Provide historical data for the frontend chart initialization"""
    date = request.args.get("date") or today_str()
    limit_raw = request.args.get("limit")
    limit = None
    if limit_raw is not None:
        try:
            limit = int(limit_raw)
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400
        if limit <= 0:
            return jsonify({"error": "limit must be greater than 0"}), 400

    log_file = get_log_path(date)
    
    if not os.path.exists(log_file):
        return jsonify({})

    df = pd.read_csv(log_file)
    if df.empty:
        return jsonify({})

    df["temperature"] = pd.to_numeric(df["temperature"], errors="coerce")
    df = df.dropna(subset=["temperature"])
    if df.empty:
        return jsonify({})

    df = df.sort_values("timestamp")

    # Structure data by machine: { "Machine1": [{"x": "time", "y": temp}], ... }
    history = {}
    for machine, group in df.groupby("machine", sort=False):
        if limit is not None and len(group) > limit:
            group = group.tail(limit)

        history[machine] = [
            {"x": timestamp, "y": round(float(temperature), 1)}
            for timestamp, temperature in zip(group["timestamp"], group["temperature"])
        ]
    
    return jsonify(history)

@app.route('/api/daily_summary')
def get_daily_summary():
    """Provide daily averages and reading counts for selected date."""
    date = request.args.get("date") or today_str()
    log_file = get_log_path(date)

    if not os.path.exists(log_file):
        return jsonify({
            "date": date,
            "overall_avg": None,
            "machine_averages": {},
            "machine_count": 0,
            "reading_count": 0
        })

    df = pd.read_csv(log_file)
    if df.empty:
        return jsonify({
            "date": date,
            "overall_avg": None,
            "machine_averages": {},
            "machine_count": 0,
            "reading_count": 0
        })

    df["temperature"] = pd.to_numeric(df["temperature"], errors="coerce")
    df = df.dropna(subset=["temperature"])
    if df.empty:
        return jsonify({
            "date": date,
            "overall_avg": None,
            "machine_averages": {},
            "machine_count": 0,
            "reading_count": 0
        })

    machine_averages = (
        df.groupby("machine")["temperature"]
        .mean()
        .round(1)
        .to_dict()
    )

    return jsonify({
        "date": date,
        "overall_avg": round(float(df["temperature"].mean()), 1),
        "machine_averages": machine_averages,
        "machine_count": len(machine_averages),
        "reading_count": int(len(df))
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
.chart-tools { display: flex; gap: 8px; margin-bottom: 10px; }
.chart-tools button {
    background: #0b1220; color: var(--text); border: 1px solid #334155;
    border-radius: 10px; padding: 6px 10px; cursor: pointer;
}
.chart-tools button:hover { border-color: #64748b; }
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
    let chart;
    const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899'];
    const LIVE_MAX_POINTS = 600;
    let colorIndex = 0;

    // Initialize Chart.js
    const ctx = document.getElementById('tempChart').getContext('2d');
    chart = new Chart(ctx, {
        type: 'line',
        data: { datasets: [] },
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: { duration: 0 }, // Disable animation for performance on live updates
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                x: { type: 'time', time: { tooltipFormat: 'HH:mm:ss' }, title: { display: true, text: 'Time' }, grid: {color: '#334155'} },
                y: { title: { display: true, text: 'Temperature (°C)' }, grid: {color: '#334155'} }
            },
            plugins: {
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
                    }
                }
            }
        }
    });

    document.getElementById('live-zoom-in').addEventListener('click', () => {
        if (typeof chart.zoom === 'function') chart.zoom(1.2);
    });
    document.getElementById('live-zoom-out').addEventListener('click', () => {
        if (typeof chart.zoom === 'function') chart.zoom(0.8);
    });
    document.getElementById('live-reset-zoom').addEventListener('click', () => {
        if (typeof chart.resetZoom === 'function') chart.resetZoom();
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

    // Load initial history
    fetch(`/api/history?limit=${LIVE_MAX_POINTS}`)
        .then(res => res.json())
        .then(data => {
            for (const [machine, points] of Object.entries(data)) {
                const dataset = getOrCreateDataset(machine);
                dataset.data = points.slice(-LIVE_MAX_POINTS); // points format: {x: timestamp, y: temp}
                
                // Update card with latest temp from history
                if (points.length > 0) {
                    updateMachineCard(machine, points[points.length - 1].y, 85); // fallback threshold
                }
            }
            chart.update('none');
        });

    // Handle Live Socket Updates
    socket.on('connect', () => { document.getElementById('socket-status').innerText = 'Live 🟢'; document.getElementById('socket-status').style.color = '#22c55e'; });
    socket.on('disconnect', () => { document.getElementById('socket-status').innerText = 'Offline 🔴'; document.getElementById('socket-status').style.color = '#ef4444'; });

    socket.on('new_temp', (msg) => {
        updateMachineCard(msg.machine, msg.temp, msg.threshold);
        
        const dataset = getOrCreateDataset(msg.machine);
        dataset.data.push({ x: msg.timestamp, y: msg.temp });
        
        // Keep only the latest points per machine for smooth live interaction
        if (dataset.data.length > LIVE_MAX_POINTS) {
            dataset.data.splice(0, dataset.data.length - LIVE_MAX_POINTS);
        }
        
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
.toolbar input, .toolbar button {
    background: #0b1220; color: var(--text); border: 1px solid #334155;
    border-radius: 10px; padding: 8px 12px;
}
.toolbar button { cursor: pointer; }
.toolbar button:hover { border-color: #64748b; }
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
    const overallAvgEl = document.getElementById('overall-avg');
    const dailyMetaEl = document.getElementById('daily-meta');
    const machineAvgBody = document.getElementById('machine-avg-body');
    const noDataEl = document.getElementById('no-data');

    function getLocalDateString() {
        const now = new Date();
        const local = new Date(now.getTime() - (now.getTimezoneOffset() * 60000));
        return local.toISOString().split('T')[0];
    }

    const dailyChart = new Chart(document.getElementById('dailyChart').getContext('2d'), {
        type: 'line',
        data: { datasets: [] },
        options: {
            parsing: false,
            normalized: true,
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                x: { type: 'time', time: { tooltipFormat: 'HH:mm:ss' }, title: { display: true, text: 'Time' }, grid: { color: '#334155' } },
                y: { title: { display: true, text: 'Temperature (°C)' }, grid: { color: '#334155' } }
            },
            plugins: {
                decimation: {
                    enabled: true,
                    algorithm: 'lttb',
                    samples: 1200,
                    threshold: 2000
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
                    }
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

    async function loadSelectedDay() {
        const date = dayPicker.value;
        if (!date) return;

        const [historyRes, summaryRes] = await Promise.all([
            fetch(`/api/history?date=${encodeURIComponent(date)}`),
            fetch(`/api/daily_summary?date=${encodeURIComponent(date)}`)
        ]);

        const history = await historyRes.json();
        const summary = await summaryRes.json();

        dailyChart.data.datasets = Object.entries(history).map(([machine, points], index) => ({
            label: machine,
            data: points.map((point) => ({ x: Date.parse(point.x), y: Number(point.y) })),
            borderColor: colors[index % colors.length],
            backgroundColor: 'transparent',
            borderWidth: 1.8,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHitRadius: 12,
            tension: 0
        }));
        dailyChart.update('none');

        if (typeof dailyChart.resetZoom === 'function') {
            dailyChart.resetZoom();
        }

        if (summary.overall_avg === null) {
            overallAvgEl.textContent = '-- °C';
            dailyMetaEl.textContent = `${date} • 0 machines • 0 readings`;
        } else {
            overallAvgEl.textContent = `${Number(summary.overall_avg).toFixed(1)} °C`;
            dailyMetaEl.textContent = `${date} • ${summary.machine_count} machines • ${summary.reading_count} readings`;
        }

        updateMachineAverages(summary.machine_averages);
        noDataEl.style.display = dailyChart.data.datasets.length ? 'none' : 'block';
    }

    document.getElementById('load-day').addEventListener('click', loadSelectedDay);
    document.getElementById('zoom-in').addEventListener('click', () => {
        if (typeof dailyChart.zoom === 'function') dailyChart.zoom(1.2);
    });
    document.getElementById('zoom-out').addEventListener('click', () => {
        if (typeof dailyChart.zoom === 'function') dailyChart.zoom(0.8);
    });
    document.getElementById('reset-zoom').addEventListener('click', () => {
        if (typeof dailyChart.resetZoom === 'function') dailyChart.resetZoom();
    });

    dayPicker.value = getLocalDateString();
    loadSelectedDay();
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
