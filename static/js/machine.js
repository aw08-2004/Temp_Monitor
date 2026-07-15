const config = document.getElementById('machine-config');
const MACHINE = config.dataset.machine;
const OVERHEAT_THRESHOLD = Number(config.dataset.overheatThreshold);
const LOW_LOAD_THRESHOLD = Number(config.dataset.lowLoadThreshold);

const zoomPlugin = window['chartjs-plugin-zoom'];
if (zoomPlugin) Chart.register(zoomPlugin.default || zoomPlugin);

const rootStyles = getComputedStyle(document.documentElement);
const chartGridColor = rootStyles.getPropertyValue('--card-border').trim();

requestNotificationPermission();

const socket = connectSocketWithStatus();
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
            x: { type: 'time', time: { tooltipFormat: 'HH:mm:ss' }, title: { display: true, text: 'Time' }, grid: { color: chartGridColor } },
            y: { title: { display: true, text: 'Temperature (°C)' }, grid: { color: chartGridColor } }
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

let lastCpuLoadPct = null;

function applyTemp(temp) {
    if (temp === undefined || temp === null) return;
    document.getElementById('stat-temp').textContent = Number(temp).toFixed(1) + ' °C';
    const status = classifyOverheatStatus(temp, OVERHEAT_THRESHOLD, lastCpuLoadPct, LOW_LOAD_THRESHOLD);
    if (status === 'normal') {
        tempCard.classList.remove('stat-card--overheat');
        setStatusPill(statusEl, 'ok', 'Normal');
    } else if (status === 'overheat-expected') {
        tempCard.classList.add('stat-card--overheat');
        setStatusPill(statusEl, 'warn', '🔥 Overheating (high load)');
    } else {
        tempCard.classList.add('stat-card--overheat');
        setStatusPill(statusEl, 'danger', '🔥 Overheating (low load — investigate)');
    }
}

function formatMetric(value, suffix) {
    return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)} ${suffix}` : '--';
}

function applyDiagnostics(diagnostics) {
    const d = diagnostics || {};
    lastCpuLoadPct = typeof d.cpu_load_pct === 'number' ? d.cpu_load_pct : null;
    document.getElementById('stat-cpu-load').textContent = formatMetric(d.cpu_load_pct, '%');
    document.getElementById('stat-cpu-clock').textContent = formatMetric(d.cpu_clock_mhz, 'MHz');
    document.getElementById('stat-gpu-temp').textContent = 'Temp: ' + formatMetric(d.gpu_temp, '°C');
    document.getElementById('stat-gpu-load').textContent = 'Load: ' + formatMetric(d.gpu_load_pct, '%');
    document.getElementById('stat-gpu-clock').textContent = 'Clock: ' + formatMetric(d.gpu_clock_mhz, 'MHz');
}

async function loadMachineInfo() {
    try {
        const resp = await fetch('/api/machines/' + encodeURIComponent(MACHINE));
        if (!resp.ok) return;
        const info = await resp.json();
        applyDiagnostics(info.diagnostics);
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

socket.on('new_temp', (msg) => {
    if (msg.machine !== MACHINE) return;
    applyDiagnostics(msg.diagnostics);
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
