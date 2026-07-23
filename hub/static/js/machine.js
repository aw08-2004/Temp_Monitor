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
const uptimeLabelEl = document.getElementById('stat-uptime-label');
const uptimeValueEl = document.getElementById('stat-uptime');

// Concise "3h ago" from a server-local "YYYY-MM-DD HH:MM:SS" string, for the offline
// "Last seen" readout (the hub and operators run in the same timezone).
function formatRelativeTime(updatedAt) {
    if (!updatedAt) return '--';
    const then = new Date(String(updatedAt).replace(' ', 'T'));
    if (Number.isNaN(then.getTime())) return updatedAt;
    const secs = Math.max(0, Math.floor((Date.now() - then.getTime()) / 1000));
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
}

// The Uptime card doubles as a "Last seen" card when the machine is offline -- a stale
// uptime is meaningless, and last-seen is what you actually want for a machine that's gone quiet.
function showUptime(uptimeSeconds) {
    uptimeLabelEl.textContent = 'Uptime';
    uptimeValueEl.textContent = formatUptime(uptimeSeconds);
    uptimeValueEl.removeAttribute('title');
}

function showLastSeen(updatedAt) {
    uptimeLabelEl.textContent = 'Last seen';
    uptimeValueEl.textContent = formatRelativeTime(updatedAt);
    if (updatedAt) uptimeValueEl.title = updatedAt; else uptimeValueEl.removeAttribute('title');
}
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
    params.set('date', date);
    params.set('from', formatDateForApi(new Date(minMs)));
    params.set('to', formatDateForApi(new Date(maxMs)));
    params.set('resolution', resolution);
    params.set('limit', 'all');
    // One request returns every panel's series: { metrics: { key: [{x, y}], ... } }.
    return `/api/machines/${encodeURIComponent(MACHINE)}/history?${params.toString()}`;
}

function scheduleViewportReload() {
    if (!dynamicResolutionEl.checked || !selectedDayRange) return;
    if (viewportReloadTimer !== null) clearTimeout(viewportReloadTimer);
    viewportReloadTimer = setTimeout(() => {
        loadVisibleViewport();
        viewportReloadTimer = null;
    }, VIEWPORT_RELOAD_DEBOUNCE_MS);
}

// ---- Historical multi-panel dashboard ----------------------------------------
// One Chart.js line panel per metric, Komodo-style. METRICS is the single source of truth
// for which panels exist and how each looks; a panel renders only when its collection
// toggle (data-enabled-metrics, from settings.py's metrics.* knobs) is on. `diag` maps a
// metric to its key in the live `diagnostics` payload, for real-time appends over the socket.
const ENABLED_METRICS = (() => {
    try { return JSON.parse(config.dataset.enabledMetrics || '{}'); }
    catch (e) { return {}; }
})();

// `rate: true` marks a metric stored in bytes per second. Those panels label their axis
// and tooltip through formatRate() instead of pinning a fixed "B/s", so a 400 MB/s NVMe
// and a 2 KB/s idle NIC are both readable -- at a fixed B/s the former is an unreadable
// nine-digit tick and the latter is a flat line at the bottom of the axis.
const METRICS = [
    { key: 'cpu_load',   label: 'CPU Load',    unit: '%',   color: '#10b981', max: 100, diag: 'cpu_load_pct' },
    { key: 'memory',     label: 'Memory',      unit: '%',   color: '#f59e0b', max: 100, diag: 'memory_load_pct' },
    { key: 'disk',       label: 'Disk',        unit: '%',   color: '#3b82f6', max: 100, diag: 'disk_load_pct' },
    { key: 'net_rx',     label: 'Network In',  unit: 'B/s', color: '#22d3ee', rate: true, diag: 'net_rx_bps' },
    { key: 'net_tx',     label: 'Network Out', unit: 'B/s', color: '#ec4899', rate: true, diag: 'net_tx_bps' },
    { key: 'disk_read',  label: 'Disk Read',   unit: 'B/s', color: '#14b8a6', rate: true, diag: 'disk_read_bps' },
    { key: 'disk_write', label: 'Disk Write',  unit: 'B/s', color: '#f43f5e', rate: true, diag: 'disk_write_bps' },
    { key: 'gpu_temp',   label: 'GPU Temp',    unit: '°C',  color: '#8b5cf6', diag: 'gpu_temp' },
    { key: 'gpu_load',   label: 'GPU Load',    unit: '%',   color: '#a855f7', max: 100, diag: 'gpu_load_pct' },
    { key: 'temp',       label: 'Temperature', unit: '°C',  color: '#f97316' },
];

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

function scaleBytes(bytes, base) {
    let value = Math.abs(Number(bytes));
    let step = 0;
    while (value >= base && step < BYTE_UNITS.length - 1) { value /= base; step += 1; }
    return { value: Number(bytes) < 0 ? -value : value, unit: BYTE_UNITS[step] };
}

// Throughput scales in 1000s, capacity in 1024s -- deliberately different, because each
// matches what the operator is comparing against. Chart.js picks round tick values in raw
// bytes (400000, 800000...), so a binary axis would label them 390.6 KB/s and 781.3 KB/s;
// decimal makes those gridlines land on 400 KB/s and 800 KB/s, and it is what network gear
// reports anyway. Disk capacity stays binary so "476 GB" matches what Explorer shows for
// the same drive.
function formatRate(bytesPerSecond) {
    if (!Number.isFinite(Number(bytesPerSecond))) return '--';
    const { value, unit } = scaleBytes(bytesPerSecond, 1000);
    // Whole numbers below 1 KB/s: a "0.0 B/s" axis tick reads as broken. One decimal above
    // -- enough to tell 1.4 from 1.9 MB/s without noise. Round values keep their integer
    // form, so an 800 KB/s gridline is labelled "800 KB/s", not "800.0 KB/s".
    if (unit === 'B' || Number.isInteger(value)) return `${Math.round(value)} ${unit}/s`;
    return `${value.toFixed(1)} ${unit}/s`;
}

// Absolute size (GB in, human units out) for the Storage cards. A decimal below 100 only
// -- "412.0 GB" is false precision next to a number an operator reads as "about 400".
function formatGb(gb) {
    if (!Number.isFinite(Number(gb))) return '--';
    const { value, unit } = scaleBytes(Number(gb) * 1024 * 1024 * 1024, 1024);
    return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

const gridEl = document.getElementById('metric-grid');
const panels = [];              // { metric, chart, emptyEl, titleEl }
let selectedDayRange = null;
let syncingXRange = false;      // guards the cross-panel zoom/pan mirroring below
// Total physical RAM (GB) for this machine -- a constant we learn from the latest
// diagnostics. Lets the Memory panel say what 100% is and convert a % point to GB on hover.
let memTotalGb = null;

function formatMemTooltip(pct) {
    if (!Number.isFinite(memTotalGb)) return `${pct.toFixed(1)} %`;
    const usedGb = (pct / 100) * memTotalGb;
    return `${usedGb.toFixed(1)} / ${memTotalGb.toFixed(0)} GB  (${pct.toFixed(0)}%)`;
}

// Reflect the machine's total RAM into the Memory panel: title becomes "Memory (16 GB)"
// so 100% is unambiguous, and the tooltip (via memTotalGb) starts reporting GB.
function updateMemoryTotal(totalGb) {
    if (!Number.isFinite(totalGb)) return;
    memTotalGb = totalGb;
    const panel = panels.find((p) => p.metric.key === 'memory');
    if (panel && panel.titleEl) panel.titleEl.textContent = `Memory (${totalGb.toFixed(0)} GB)`;
}

function metricEnabled(metric) {
    return ENABLED_METRICS[metric.key] !== false;   // default on for unknown keys
}

function panelConfig(metric) {
    const yScale = { title: { display: true, text: metric.unit }, grid: { color: chartGridColor } };
    if (metric.max !== undefined) { yScale.min = 0; yScale.max = metric.max; }
    if (metric.rate) {
        // Each tick scales on its own value, so the axis stays readable whatever range the
        // zoom lands on. The axis title drops the unit -- it now lives on every tick.
        yScale.min = 0;
        yScale.title.text = 'per second';
        yScale.ticks = { callback: (value) => formatRate(value) };
    }
    return {
        type: 'line',
        data: {
            datasets: [{
                label: metric.label, data: [], parsing: false,
                borderColor: metric.color, backgroundColor: 'transparent',
                borderWidth: 2, tension: 0.25, pointRadius: 0,
                pointHoverRadius: 6, pointHitRadius: 20,
            }],
        },
        options: {
            responsive: true, maintainAspectRatio: false, normalized: true,
            animation: { duration: 0 },
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                x: { type: 'time', time: { tooltipFormat: 'HH:mm:ss' }, grid: { color: chartGridColor } },
                y: yScale,
            },
            plugins: {
                decimation: { enabled: true, algorithm: 'lttb', samples: 400 },
                legend: { display: false },
                tooltip: {
                    mode: 'index', intersect: false,
                    callbacks: {
                        label: (ctx) => {
                            if (metric.key === 'memory') return formatMemTooltip(ctx.parsed.y);
                            if (metric.rate) return formatRate(ctx.parsed.y);
                            return `${ctx.parsed.y.toFixed(1)} ${metric.unit}`;
                        },
                    },
                },
                zoom: {
                    pan: { enabled: true, mode: 'x' },
                    zoom: {
                        wheel: { enabled: true }, pinch: { enabled: true },
                        drag: { enabled: true, backgroundColor: 'rgba(34, 197, 94, 0.15)' },
                        mode: 'x',
                    },
                    onZoomComplete: ({ chart }) => onPanelRangeChanged(chart),
                    onPanComplete: ({ chart }) => onPanelRangeChanged(chart),
                },
            },
        },
    };
}

function buildPanels() {
    gridEl.replaceChildren();
    panels.length = 0;
    for (const metric of METRICS) {
        if (!metricEnabled(metric)) continue;

        const container = document.createElement('div');
        container.className = 'metric-panel';

        const head = document.createElement('div');
        head.className = 'metric-panel__head';
        const title = document.createElement('span');
        title.className = 'metric-panel__title';
        title.textContent = metric.label;
        head.appendChild(title);
        container.appendChild(head);

        const chartBox = document.createElement('div');
        chartBox.className = 'metric-panel__chart';
        const canvas = document.createElement('canvas');
        chartBox.appendChild(canvas);
        container.appendChild(chartBox);

        const emptyEl = document.createElement('div');
        emptyEl.className = 'stat-card__meta metric-panel__empty';
        emptyEl.textContent = 'No data for this range.';
        emptyEl.style.display = 'none';
        container.appendChild(emptyEl);

        gridEl.appendChild(container);
        const chart = new Chart(canvas.getContext('2d'), panelConfig(metric));
        panels.push({ metric, chart, emptyEl, titleEl: title });
    }
    // If diagnostics already told us the RAM size before the panels existed, apply it now.
    if (Number.isFinite(memTotalGb)) updateMemoryTotal(memTotalGb);
}

// Mirror one panel's zoom/pan onto every other panel so the whole grid shares a time axis,
// then (in dynamic mode) reload the visible window at an appropriate resolution.
function onPanelRangeChanged(sourceChart) {
    if (syncingXRange) return;
    const xs = sourceChart.scales?.x;
    if (!xs || !Number.isFinite(Number(xs.min)) || !Number.isFinite(Number(xs.max))) return;
    applyXRangeToAll(Number(xs.min), Number(xs.max), sourceChart);
    scheduleViewportReload();
}

function applyXRangeToAll(minMs, maxMs, exceptChart) {
    syncingXRange = true;
    try {
        for (const p of panels) {
            if (p.chart === exceptChart) continue;
            p.chart.options.scales.x.min = minMs;
            p.chart.options.scales.x.max = maxMs;
            p.chart.update('none');
        }
    } finally {
        syncingXRange = false;
    }
}

function applyRange(chart, minMs, maxMs, resetZoom) {
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
}

async function loadHistoryRange(minMs, maxMs, resolution, resetZoom) {
    if (historyLoadInFlight || !dayPicker.value || panels.length === 0) return;
    historyLoadInFlight = true;
    try {
        const historyRes = await fetch(buildHistoryUrl(dayPicker.value, minMs, maxMs, resolution));
        const body = await historyRes.json();
        const series = (body && body.metrics) || {};
        let anyData = false;
        for (const p of panels) {
            const points = (series[p.metric.key] || [])
                .map((point) => {
                    const x = toChartTimestamp(point.x ?? point.timestamp ?? point.ts_text);
                    const y = Number(point.y);
                    if (x === null || !Number.isFinite(y)) return null;
                    return { x, y };
                })
                .filter(Boolean);
            p.chart.data.datasets[0].data = points;
            p.emptyEl.style.display = points.length ? 'none' : 'block';
            if (points.length) anyData = true;
            applyRange(p.chart, minMs, maxMs, resetZoom);
        }
        noDataEl.style.display = anyData ? 'none' : 'block';
        setResolutionInUse(resolution);
        lastHistoryRequest = { minMs, maxMs, resolution };
    } finally {
        historyLoadInFlight = false;
    }
}

async function loadVisibleViewport() {
    if (!selectedDayRange || historyLoadInFlight || panels.length === 0) return;
    const xScale = panels[0].chart.scales?.x;
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
    for (const p of panels) if (typeof p.chart.zoom === 'function') p.chart.zoom(1.2);
    scheduleViewportReload();
});
document.getElementById('zoom-out').addEventListener('click', () => {
    for (const p of panels) if (typeof p.chart.zoom === 'function') p.chart.zoom(0.8);
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
gridEl.addEventListener('wheel', () => {
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

// ---- Storage cards ------------------------------------------------------------
// One tile per volume: fill bar, % occupied, and used/total. Rebuilt on every reading
// rather than patched in place -- a USB-attached fixed disk or a newly mounted volume
// changes the LIST, and diffing three text nodes is not worth the state it would need.
const diskGridEl = document.getElementById('disk-grid');
const diskEmptyEl = document.getElementById('disk-empty');

// Thresholds an operator acts on: amber at 80% (worth planning for), red at 90% (Windows
// itself starts complaining, and updates begin failing).
function diskFillClass(pct) {
    if (pct >= 90) return 'disk-tile__fill--danger';
    if (pct >= 80) return 'disk-tile__fill--warn';
    return '';
}

function renderDisks(disks) {
    if (!diskGridEl) return;
    const list = Array.isArray(disks) ? disks : [];
    // An empty list from a machine that IS reporting sensors means "no disks seen", but an
    // absent key means an older hub -- either way the previous tiles are stale, so clear.
    diskGridEl.replaceChildren();
    diskEmptyEl.style.display = list.length ? 'none' : 'block';

    for (const disk of list) {
        const pct = Number(disk.used_pct);
        const hasPct = Number.isFinite(pct);

        const tile = document.createElement('div');
        tile.className = 'disk-tile';

        const head = document.createElement('div');
        head.className = 'disk-tile__head';
        const name = document.createElement('span');
        name.className = 'disk-tile__name';
        // textContent: volume labels come from the agent, and /api/report is unauthenticated.
        name.textContent = disk.name || 'Disk';
        name.title = name.textContent;
        const value = document.createElement('span');
        value.className = 'disk-tile__pct';
        value.textContent = hasPct ? `${pct.toFixed(0)}%` : '--';
        head.append(name, value);

        const bar = document.createElement('div');
        bar.className = 'disk-tile__bar';
        const fill = document.createElement('div');
        fill.className = `disk-tile__fill ${diskFillClass(pct)}`.trim();
        fill.style.width = `${hasPct ? Math.min(100, Math.max(0, pct)) : 0}%`;
        bar.appendChild(fill);

        const meta = document.createElement('div');
        meta.className = 'stat-card__meta';
        // GB needs the agent's volume sensors (3.10.0+). Without them we still know the
        // percentage, so show the bar and say what's missing instead of an empty tile.
        if (Number.isFinite(Number(disk.used_gb)) && Number.isFinite(Number(disk.total_gb))) {
            const free = Number(disk.total_gb) - Number(disk.used_gb);
            meta.textContent =
                `${formatGb(disk.used_gb)} of ${formatGb(disk.total_gb)} used · ${formatGb(free)} free`;
        } else {
            meta.textContent = 'Used space only — size needs agent 3.10.0+';
        }

        tile.append(head, bar, meta);
        diskGridEl.appendChild(tile);
    }
}

function applyDiagnostics(diagnostics) {
    const d = diagnostics || {};
    renderDisks(d.disks);
    if (typeof d.mem_total_gb === 'number') updateMemoryTotal(d.mem_total_gb);
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
        if (info.status === 'offline') {
            showLastSeen(info.updated_at);
        } else {
            showUptime(info.uptime_seconds);
        }
        document.getElementById('stat-version').textContent = info.companion_version || '--';
        document.getElementById('stat-model').textContent = 'Model: ' + (info.model || '--');
        document.getElementById('stat-serial').textContent = 'Serial: ' + (info.serial_number || '--');
        document.getElementById('stat-service').textContent = 'Service tag: ' + (info.service_tag || '--');
        document.getElementById('stat-asset').textContent = 'Asset tag: ' + (info.asset_tag || '--');
    } catch (e) { /* non-critical */ }
}

// ---- Primary sensor pin -------------------------------------------------------
// Populated from what this machine is actually reporting, so the operator picks a real
// name by recognition rather than typing one that has to match exactly.
const primarySensorSelect = document.getElementById('primary-sensor');
const primarySensorSave = document.getElementById('primary-sensor-save');
const primarySensorStatus = document.getElementById('primary-sensor-status');
const primarySensorOrder = document.getElementById('primary-sensor-order');
let savedPrimarySensor = '';

async function loadPrimarySensor() {
    const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/sensors`);
    if (!resp.ok) return;
    const body = await resp.json();

    savedPrimarySensor = body.primary_sensor_name || '';
    // Rebuild, keeping the "follow the fleet order" option at the top. Its label stays
    // short on purpose -- the preference chain can be five names long, and putting it in
    // the option text stretches the select across the whole card. It goes in the help
    // line below instead, where it costs no layout.
    primarySensorSelect.replaceChildren();
    const followOpt = document.createElement('option');
    followOpt.value = '';
    followOpt.textContent = 'Follow the fleet preference order';
    primarySensorSelect.appendChild(followOpt);

    primarySensorOrder.textContent = (body.preference && body.preference.length)
        ? ` (currently ${body.preference.join(' → ')})`
        : '';

    for (const s of body.sensors || []) {
        const opt = document.createElement('option');
        opt.value = s.name;
        // textContent, never innerHTML: these names come from the agent, and /api/report
        // is unauthenticated.
        opt.textContent = s.value === null || s.value === undefined
            ? s.name
            : `${s.name} — ${s.value} °C`;
        primarySensorSelect.appendChild(opt);
    }

    // A pinned sensor the machine isn't currently reporting would otherwise vanish from
    // the list and look unset. Show it, flagged, so the operator can see why the pin
    // isn't taking effect.
    if (savedPrimarySensor && !(body.sensors || []).some((s) => s.name === savedPrimarySensor)) {
        const missing = document.createElement('option');
        missing.value = savedPrimarySensor;
        missing.textContent = `${savedPrimarySensor} — not currently reported`;
        primarySensorSelect.appendChild(missing);
    }

    primarySensorSelect.value = savedPrimarySensor;
    primarySensorSave.hidden = true;
    primarySensorStatus.textContent = (!body.sensors || !body.sensors.length)
        ? 'No CPU temperature sensors reported yet.'
        : '';
}

// Picking a sensor saves it immediately -- no Save button (it stays hidden). A seq guard
// drops a slow response that a newer pick has already superseded.
let primarySensorSaveSeq = 0;

primarySensorSelect.addEventListener('change', savePrimarySensor);

async function savePrimarySensor() {
    if (primarySensorSelect.value === savedPrimarySensor) return;   // nothing changed
    const seq = ++primarySensorSaveSeq;
    primarySensorStatus.textContent = 'Saving…';
    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/primary_sensor`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ primary_sensor_name: primarySensorSelect.value || null }),
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${resp.status}`);
        }
        if (seq !== primarySensorSaveSeq) return;
        await loadPrimarySensor();
        primarySensorStatus.textContent = 'Saved. Applies from the next reading.';
    } catch (e) {
        if (seq !== primarySensorSaveSeq) return;
        primarySensorStatus.textContent = `Could not save: ${e.message}`;
    }
}

dayPicker.value = getLocalDateString();
syncResolutionControl();
buildPanels();
loadSelectedDay();
loadMachineInfo();
loadPrimarySensor();

socket.on('new_temp', (msg) => {
    if (msg.machine !== MACHINE) return;
    applyDiagnostics(msg.diagnostics);
    applyTemp(msg.temp);
    // A live report means the machine is online now, so restore the uptime readout
    // (it may have been showing "Last seen" from an earlier offline load).
    if (msg.uptime_seconds !== undefined && msg.uptime_seconds !== null) {
        showUptime(msg.uptime_seconds);
    }
    // Follow a companion self-update without a refresh. Only present when the client
    // reported one, so an older client's silence can't blank the version we already show.
    if (msg.companion_version) {
        document.getElementById('stat-version').textContent = msg.companion_version;
    }
    if (!viewingToday) return;

    const x = toChartTimestamp(msg.timestamp_ms ?? msg.timestamp_epoch ?? msg.timestamp);
    if (x === null) return;
    // Append this report to every panel: temperature from msg.temp, the rest from the live
    // diagnostics block (which now carries disk & network alongside cpu/gpu/memory). A metric
    // the machine doesn't report is simply skipped for that tick.
    const diagnostics = msg.diagnostics || {};
    for (const p of panels) {
        const y = p.metric.key === 'temp' ? Number(msg.temp) : Number(diagnostics[p.metric.diag]);
        if (!Number.isFinite(y)) continue;
        p.chart.data.datasets[0].data.push({ x, y });
        p.emptyEl.style.display = 'none';
        p.chart.update('none');
    }
    if (selectedDayRange) selectedDayRange.endMs = Math.max(selectedDayRange.endMs, x);
    noDataEl.style.display = 'none';
});
