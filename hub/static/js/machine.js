const config = document.getElementById('machine-config');
const MACHINE = config.dataset.machine;
// How much history the panels OPEN on, from hub.live_default_window_seconds. Small by
// default (a minute), because this page is watched live far more often than it is read as an
// archive -- a whole day fitted into 400 px is a flat line you have to zoom into before it
// says anything. The fallback matches the registry default, for a page served by an older
// hub that doesn't emit the attribute.
const LIVE_WINDOW_MS = (Number(config.dataset.liveWindowSeconds) || 60) * 1000;

const zoomPlugin = window['chartjs-plugin-zoom'];
if (zoomPlugin) Chart.register(zoomPlugin.default || zoomPlugin);

const rootStyles = getComputedStyle(document.documentElement);
const chartGridColor = rootStyles.getPropertyValue('--card-border').trim();

const socket = connectSocketWithStatus();
const dayPicker = document.getElementById('day-picker');
const resolutionEl = document.getElementById('resolution');
const resolutionInUseEl = document.getElementById('resolution-in-use');
const dynamicResolutionEl = document.getElementById('dynamic-resolution');
const noDataEl = document.getElementById('no-data');
const tempCard = document.getElementById('temp-card');
const uptimeLabelEl = document.getElementById('stat-uptime-label');
const uptimeValueEl = document.getElementById('stat-uptime');

// Concise "3h ago" from a server-local "YYYY-MM-DD HH:MM:SS" string, for the offline
// "Last seen" readout (the hub and operators run in the same timezone).
function formatRelativeTime(updatedAt) {
    if (!updatedAt) return '--';
    const then = new Date(String(updatedAt).replace(' ', 'T'));
    if (Number.isNaN(then.getTime())) return updatedAt;
    const secs = Math.max(0, Math.floor((Date.now() - then.getTime()) / 1000));
    if (secs < 60) return t('machine.ago.seconds', { value: secs });
    const mins = Math.floor(secs / 60);
    if (mins < 60) return t('machine.ago.minutes', { value: mins });
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return t('machine.ago.hours', { value: hrs });
    return t('machine.ago.days', { value: Math.floor(hrs / 24) });
}

// The Uptime card doubles as a "Last seen" card when the machine is offline -- a stale
// uptime is meaningless, and last-seen is what you actually want for a machine that's gone quiet.
function showUptime(uptimeSeconds) {
    uptimeLabelEl.textContent = t('machine.uptime');
    uptimeValueEl.textContent = formatUptime(uptimeSeconds);
    uptimeValueEl.removeAttribute('title');
}

function showLastSeen(updatedAt) {
    uptimeLabelEl.textContent = t('machine.last_seen');
    uptimeValueEl.textContent = formatRelativeTime(updatedAt);
    if (updatedAt) uptimeValueEl.title = updatedAt; else uptimeValueEl.removeAttribute('title');
}
const VIEWPORT_RELOAD_DEBOUNCE_MS = 250;
let viewportReloadTimer = null;
let lastHistoryRequest = null;
let historyLoadInFlight = false;
let viewingToday = true;
// Follow mode: every live reading re-anchors the visible window so the newest point sits at
// the right edge. On for today, off the moment the operator pans or zooms (they've asked to
// look somewhere specific, and yanking the axis out from under them each tick is unusable),
// and back on via Reset. Deliberately separate from `viewingToday`: a past day has no
// "latest" to follow, but you can also be on today and simply not want it.
let followLive = true;

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

// What the panels SHOW when the page opens (and what Reset returns to). Distinct from
// getDayRange, which stays the bound on where panning and zooming may reach -- the whole day
// is one pan away, it just isn't what you land on. A past day has no live edge to sit at, so
// it opens on the whole day exactly as before.
function getInitialRange(dateString) {
    const day = getDayRange(dateString);
    if (!day) return null;
    if (dateString !== getLocalDateString()) return day;
    return { startMs: Math.max(day.startMs, day.endMs - LIVE_WINDOW_MS), endMs: day.endMs };
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
    resolutionInUseEl.textContent = t('history.in_use',
        { value: resolution || t('history.resolution_label.unknown') });
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
// `bits: true` additionally renders that rate in bits, which is the unit network hardware
// is specified and sold in. Network only -- see BIT_UNITS.
// A rate metric's `unit` never reaches the screen: panelConfig() replaces the axis title
// with t('machine.per_second') and the tooltip returns formatRate(), both of which carry
// the unit per-tick instead. It stays in the table as the row's own documentation of what
// it charts -- which is exactly why the network rows say 'b/s' and the disk rows 'B/s'.
// `label` is a function, not a string: this table is built at module load, and a panel
// title has to be the operator's language rather than whatever the file was written in.
// One literal key per metric, so the key scan in tests/test_i18n.py can see them all.
const METRICS = [
    { key: 'cpu_load',   label: () => t('machine.metric.cpu_load'),   unit: '%',   color: '#10b981', max: 100, diag: 'cpu_load_pct' },
    { key: 'memory',     label: () => t('machine.metric.memory'),     unit: '%',   color: '#f59e0b', max: 100, diag: 'memory_load_pct' },
    { key: 'disk',       label: () => t('machine.metric.disk'),       unit: '%',   color: '#3b82f6', max: 100, diag: 'disk_load_pct' },
    { key: 'net_rx',     label: () => t('machine.metric.net_rx'),     unit: 'b/s', color: '#22d3ee', rate: true, bits: true, diag: 'net_rx_bps' },
    { key: 'net_tx',     label: () => t('machine.metric.net_tx'),     unit: 'b/s', color: '#ec4899', rate: true, bits: true, diag: 'net_tx_bps' },
    { key: 'disk_read',  label: () => t('machine.metric.disk_read'),  unit: 'B/s', color: '#14b8a6', rate: true, diag: 'disk_read_bps' },
    { key: 'disk_write', label: () => t('machine.metric.disk_write'), unit: 'B/s', color: '#f43f5e', rate: true, diag: 'disk_write_bps' },
    { key: 'gpu_temp',   label: () => t('machine.metric.gpu_temp'),   unit: '°C',  color: '#8b5cf6', diag: 'gpu_temp' },
    { key: 'gpu_load',   label: () => t('machine.metric.gpu_load'),   unit: '%',   color: '#a855f7', max: 100, diag: 'gpu_load_pct' },
    // `decimals: 0` -- a fan reports whole revolutions per minute, and "1200.0 RPM" reads as
    // precision the sensor doesn't have. Everything else keeps the default single decimal.
    { key: 'fan_rpm',    label: () => t('machine.metric.fan_rpm'),    unit: 'RPM', color: '#38bdf8', decimals: 0, diag: 'fan_rpm' },
    { key: 'cpu_power',  label: () => t('machine.metric.cpu_power'),  unit: 'W',   color: '#eab308', diag: 'cpu_power_w' },
    { key: 'gpu_power',  label: () => t('machine.metric.gpu_power'),  unit: 'W',   color: '#c084fc', diag: 'gpu_power_w' },
    { key: 'temp',       label: () => t('machine.metric.temp'),       unit: '°C',  color: '#f97316' },
];

function metricLabel(metric) {
    return typeof metric.label === 'function' ? metric.label() : String(metric.label);
}

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];
// Network throughput is quoted in BITS everywhere an operator would check it against
// something: the NIC is a 1 Gb/s card, the switch port is 100 Mb/s, the ISP sells 500 Mb/s.
// A chart labelled MB/s is the same reading off by 8x from all of them. Disk deliberately
// stays in bytes for the mirror-image reason -- drives are specified in MB/s.
const BIT_UNITS = ['b', 'Kb', 'Mb', 'Gb', 'Tb'];

function scaleBytes(bytes, base, units = BYTE_UNITS) {
    let value = Math.abs(Number(bytes));
    let step = 0;
    while (value >= base && step < units.length - 1) { value /= base; step += 1; }
    return { value: Number(bytes) < 0 ? -value : value, unit: units[step] };
}

// Throughput scales in 1000s, capacity in 1024s -- deliberately different, because each
// matches what the operator is comparing against. Chart.js picks round tick values in raw
// bytes (400000, 800000...), so a binary axis would label them 390.6 KB/s and 781.3 KB/s;
// decimal makes those gridlines land on 400 KB/s and 800 KB/s, and it is what network gear
// reports anyway. Disk capacity stays binary so "476 GB" matches what Explorer shows for
// the same drive.
//
// `asBits` converts the same stored bytes/s to bits for display. Presentation only -- the
// agent reports bytes, the database stores bytes, and rule thresholds on metric.net_*_bps
// stay in bytes. Only the two network panels pass it.
function formatRate(bytesPerSecond, asBits) {
    if (!Number.isFinite(Number(bytesPerSecond))) return '--';
    const units = asBits ? BIT_UNITS : BYTE_UNITS;
    const scaled = asBits ? Number(bytesPerSecond) * 8 : Number(bytesPerSecond);
    const { value, unit } = scaleBytes(scaled, 1000, units);
    // Whole numbers below 1 KB/s: a "0.0 B/s" axis tick reads as broken. One decimal above
    // -- enough to tell 1.4 from 1.9 MB/s without noise. Round values keep their integer
    // form, so an 800 KB/s gridline is labelled "800 KB/s", not "800.0 KB/s".
    if (unit === units[0] || Number.isInteger(value)) return `${Math.round(value)} ${unit}/s`;
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
    if (!Number.isFinite(memTotalGb)) return t('machine.percent', { value: pct.toFixed(1) });
    const usedGb = (pct / 100) * memTotalGb;
    return t('machine.memory_tooltip', {
        used: usedGb.toFixed(1), total: memTotalGb.toFixed(0), percent: pct.toFixed(0),
    });
}

// Reflect the machine's total RAM into the Memory panel: title becomes "Memory (16 GB)"
// so 100% is unambiguous, and the tooltip (via memTotalGb) starts reporting GB.
function updateMemoryTotal(totalGb) {
    if (!Number.isFinite(totalGb)) return;
    memTotalGb = totalGb;
    const panel = panels.find((p) => p.metric.key === 'memory');
    if (panel && panel.titleEl) {
        panel.titleEl.textContent = t('machine.memory_with_total',
                                      { total: totalGb.toFixed(0) });
    }
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
        yScale.title.text = t('machine.per_second');
        yScale.ticks = { callback: (value) => formatRate(value, metric.bits) };
    }
    return {
        type: 'line',
        data: {
            datasets: [{
                label: metricLabel(metric), data: [], parsing: false,
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
                            if (metric.rate) return formatRate(ctx.parsed.y, metric.bits);
                            return `${ctx.parsed.y.toFixed(metric.decimals ?? 1)} ${metric.unit}`;
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
        title.textContent = metricLabel(metric);
        head.appendChild(title);
        container.appendChild(head);

        const chartBox = document.createElement('div');
        chartBox.className = 'metric-panel__chart';
        const canvas = document.createElement('canvas');
        chartBox.appendChild(canvas);
        container.appendChild(chartBox);

        const emptyEl = document.createElement('div');
        emptyEl.className = 'stat-card__meta metric-panel__empty';
        emptyEl.textContent = t('machine.no_data_range');
        emptyEl.style.display = 'none';
        container.appendChild(emptyEl);

        gridEl.appendChild(container);
        const chart = new Chart(canvas.getContext('2d'), panelConfig(metric));
        panels.push({ metric, chart, emptyEl, titleEl: title, container });
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
    // The operator has chosen a range by hand; stop dragging the axis back to the live edge
    // under them. Reset is how they say they're done looking.
    followLive = false;
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
            setPanelVisible(p, panelHasSubject(p, points.length));
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
    // The day stays the PAN BOUND; only the opening view narrows. loadVisibleViewport clamps
    // to selectedDayRange, so panning left off the initial minute still fetches the rest of
    // the day rather than hitting an invisible wall at the window edge.
    selectedDayRange = range;
    await loadInitialRange(date);
}

// Load (or reload) the opening view for `date`. Follow mode is re-armed here rather than at
// the call sites, so "open the page", "pick a day" and "Reset" cannot drift apart.
async function loadInitialRange(date) {
    const view = getInitialRange(date);
    if (!view) return;
    followLive = date === getLocalDateString();
    lastHistoryRequest = null;
    // resetZoom only when we are NOT following: follow mode needs an explicit x min/max to
    // anchor the window, and resetZoom's job is to throw exactly those away.
    await loadHistoryRange(
        view.startMs,
        view.endMs,
        getSelectedResolution(view.endMs - view.startMs),
        !followLive
    );
}

// Set here as well as in onPanelRangeChanged: the plugin's onZoomComplete is documented for
// user gestures, and a button that zooms while the next live reading yanks the axis back is
// worse than one that doesn't zoom at all.
document.getElementById('zoom-in').addEventListener('click', () => {
    followLive = false;
    for (const p of panels) if (typeof p.chart.zoom === 'function') p.chart.zoom(1.2);
    scheduleViewportReload();
});
document.getElementById('zoom-out').addEventListener('click', () => {
    followLive = false;
    for (const p of panels) if (typeof p.chart.zoom === 'function') p.chart.zoom(0.8);
    scheduleViewportReload();
});
document.getElementById('reset-zoom').addEventListener('click', () => {
    if (!selectedDayRange || !dayPicker.value) return;
    // Back to the opening view, not to the whole day: on today that re-arms follow mode,
    // which is what an operator who has finished inspecting a spike actually wants back.
    loadInitialRange(dayPicker.value);
});
dayPicker.addEventListener('change', loadSelectedDay);

// The range currently on screen, clamped to the selected day. Both resolution controls reload
// through this rather than through selectedDayRange: re-fetching the whole day would throw
// away whatever the operator had navigated to (including the opening live window) as a side
// effect of changing how finely it is sampled.
function currentViewRange() {
    if (!selectedDayRange) return null;
    const xs = panels[0]?.chart.scales?.x;
    const min = Number(xs?.min);
    const max = Number(xs?.max);
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return selectedDayRange;
    return {
        startMs: Math.max(selectedDayRange.startMs, Math.floor(min)),
        endMs: Math.min(selectedDayRange.endMs, Math.ceil(max)),
    };
}

function reloadAtCurrentView() {
    const view = currentViewRange();
    if (!view) return;
    lastHistoryRequest = null;
    loadHistoryRange(view.startMs, view.endMs,
                     getSelectedResolution(view.endMs - view.startMs), false);
}

resolutionEl.addEventListener('change', () => {
    if (dynamicResolutionEl.checked) return;
    reloadAtCurrentView();
});
dynamicResolutionEl.addEventListener('change', () => {
    syncResolutionControl();
    reloadAtCurrentView();
});
gridEl.addEventListener('wheel', () => {
    scheduleViewportReload();
}, { passive: true });

let lastCpuLoadPct = null;

function applyTemp(temp) {
    if (temp === undefined || temp === null) return;
    document.getElementById('stat-temp').textContent =
        t('machine.temp_c', { value: Number(temp).toFixed(1) });
    tempCard.classList.remove('stat-card--high-temp');
}

function formatMetric(value, suffix) {
    return typeof value === 'number' && Number.isFinite(value)
        ? `${value.toFixed(1)} ${suffix}` : t('machine.unknown');
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
        name.textContent = disk.name || t('machine.disk_fallback');
        name.title = name.textContent;
        const value = document.createElement('span');
        value.className = 'disk-tile__pct';
        value.textContent = hasPct ? `${pct.toFixed(0)}%` : t('machine.unknown');
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
            meta.textContent = t('machine.disk_usage', {
                used: formatGb(disk.used_gb),
                total: formatGb(disk.total_gb),
                free: formatGb(free),
            });
        } else {
            meta.textContent = t('machine.disk_no_size');
        }

        tile.append(head, bar, meta);
        diskGridEl.appendChild(tile);
    }
}

// ---- Cooling cards ------------------------------------------------------------
// One tile per fan, same live-state argument as the Storage tiles above: how many fans a
// machine has is per-machine, and it CHANGES within a session -- a GPU stops its fans
// entirely when it cools below about 50 °C, so those sensors come and go. Rebuilt each
// reading for the same reason the disk tiles are.
const fanGridEl = document.getElementById('fan-grid');
const fanEmptyEl = document.getElementById('fan-empty');

function renderFans(fans) {
    if (!fanGridEl) return;
    const list = Array.isArray(fans) ? fans : [];
    fanGridEl.replaceChildren();
    fanEmptyEl.style.display = list.length ? 'none' : 'block';

    for (const fan of list) {
        const rpm = Number(fan.rpm);
        const duty = Number(fan.control_pct);
        const hasDuty = Number.isFinite(duty);

        // A fan at 0 RPM while the board is asking for duty is seized, unplugged, or dead --
        // the failure this card exists to make visible, and the one case worth colouring.
        const stalled = hasDuty && duty > 0 && rpm === 0;

        const tile = document.createElement('div');
        tile.className = 'fan-tile';
        // Which chip the fan hangs off ("Nuvoton NCT6687D", "NVIDIA RTX 3060") -- worth
        // having when a box reports "Fan #2" twice, but not worth a line of its own.
        if (fan.hardware) tile.title = fan.hardware;

        const head = document.createElement('div');
        head.className = 'fan-tile__head';
        const name = document.createElement('span');
        name.className = 'fan-tile__name';
        // textContent: sensor names come from the agent, and /api/report is unauthenticated.
        name.textContent = fan.name || t('machine.fan_fallback');
        name.title = name.textContent;
        const value = document.createElement('span');
        value.className = 'fan-tile__rpm';
        value.textContent = Number.isFinite(rpm)
            ? t('machine.fan_rpm', { value: Math.round(rpm) })
            : t('machine.unknown');
        head.append(name, value);

        // The bar shows the DUTY the board is asking for, not the speed: RPM has no
        // meaningful maximum to fill against (a case fan tops out around 1200, a blower at
        // 5000), whereas duty is a percentage by definition.
        const bar = document.createElement('div');
        bar.className = 'fan-tile__bar';
        const fill = document.createElement('div');
        fill.className = `fan-tile__fill ${stalled ? 'fan-tile__fill--stalled' : ''}`.trim();
        fill.style.width = `${hasDuty ? Math.min(100, Math.max(0, duty)) : 0}%`;
        bar.appendChild(fill);

        const meta = document.createElement('div');
        meta.className = 'stat-card__meta';
        if (stalled) {
            meta.textContent = t('machine.fan_stalled', { value: duty.toFixed(0) });
        } else if (hasDuty) {
            meta.textContent = t('machine.fan_duty', { value: duty.toFixed(0) });
        } else {
            meta.textContent = t('machine.fan_no_duty');
        }

        tile.append(head, bar, meta);
        fanGridEl.appendChild(tile);
    }
}

// ---- Show only the hardware this machine actually has --------------------------
// A card whose every reading is absent is hidden outright rather than left showing "--":
// an office PC has no discrete GPU, most laptops expose no fan, and a permanent row of
// dashes reads as "this is broken" rather than "not applicable here".
//
// Gated on diagnostics.has_sensors, which is the hub saying it HAS a sensor block for this
// machine. Without one -- a machine that has never reported -- "absent" and "not yet known"
// are indistinguishable, and hiding on the strength of that would strip the page of a PC
// that is merely offline.
let lastDiagnostics = {};

function hasReading(...values) {
    return values.some((v) => typeof v === 'number' && Number.isFinite(v));
}

function setCardVisible(id, visible) {
    const el = document.getElementById(id);
    if (el) el.hidden = !visible;
}

function applyPresence(d) {
    if (!d.has_sensors) return;
    setCardVisible('card-cpu-load', hasReading(d.cpu_load_pct));
    setCardVisible('card-cpu-clock', hasReading(d.cpu_clock_mhz));
    setCardVisible('card-cpu-power', hasReading(d.cpu_power_w));
    // One card for four GPU readings: it stays as long as the machine reports ANY of them,
    // because a GPU that reports load but no temperature is still a GPU worth showing.
    setCardVisible('card-gpu',
                   hasReading(d.gpu_temp, d.gpu_load_pct, d.gpu_clock_mhz, d.gpu_power_w));
    setCardVisible('card-storage', Array.isArray(d.disks) && d.disks.length > 0);
    setCardVisible('card-cooling', Array.isArray(d.fans) && d.fans.length > 0);
    for (const p of panels) setPanelVisible(p, panelHasSubject(p));
}

// Whether a chart panel has anything to be about. `points` is this panel's history for the
// loaded range when we have just fetched it; the live reading counts too, so a machine that
// only started reporting a sensor five minutes ago still gets its panel.
function panelHasSubject(panel, points) {
    // Temperature is the core metric and drives the alerts -- it keeps its panel even on a
    // machine reporting nothing, where an empty chart is itself the answer.
    if (panel.metric.key === 'temp') return true;
    if (!lastDiagnostics.has_sensors) return true;
    if (points) return true;
    if (hasReading(lastDiagnostics[panel.metric.diag])) return true;
    // Not yet loaded: leave whatever the last decision was rather than flickering the
    // panel out between a diagnostics update and the history that follows it.
    return points === undefined ? !panel.container.hidden : false;
}

function setPanelVisible(panel, visible) {
    const wasHidden = panel.container.hidden;
    panel.container.hidden = !visible;
    // Chart.js measures its canvas on creation; one built (or resized) while its container
    // was display:none comes back 0 px tall, so re-measure on the way in.
    if (wasHidden && visible) panel.chart.resize();
}

function applyDiagnostics(diagnostics) {
    const d = diagnostics || {};
    lastDiagnostics = d;
    renderDisks(d.disks);
    renderFans(d.fans);
    applyPresence(d);
    if (typeof d.mem_total_gb === 'number') updateMemoryTotal(d.mem_total_gb);
    lastCpuLoadPct = typeof d.cpu_load_pct === 'number' ? d.cpu_load_pct : null;
    document.getElementById('stat-cpu-load').textContent = formatMetric(d.cpu_load_pct, '%');
    document.getElementById('stat-cpu-clock').textContent = formatMetric(d.cpu_clock_mhz, 'MHz');
    document.getElementById('stat-cpu-power').textContent = formatMetric(d.cpu_power_w, 'W');
    // Through the catalog, like the template's server-rendered first paint: the row labels
    // ("Temp: {value}") are translated text, and hardcoding them here reverted the whole
    // card to English on the first live reading.
    document.getElementById('stat-gpu-temp').textContent =
        t('machine.gpu_temp', { value: formatMetric(d.gpu_temp, '°C') });
    document.getElementById('stat-gpu-load').textContent =
        t('machine.gpu_load', { value: formatMetric(d.gpu_load_pct, '%') });
    document.getElementById('stat-gpu-clock').textContent =
        t('machine.gpu_clock', { value: formatMetric(d.gpu_clock_mhz, 'MHz') });
    document.getElementById('stat-gpu-power').textContent =
        t('machine.gpu_power', { value: formatMetric(d.gpu_power_w, 'W') });
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
        const dash = t('machine.unknown');
        document.getElementById('stat-version').textContent = info.companion_version || dash;
        document.getElementById('stat-manufacturer').textContent =
            t('machine.manufacturer', { value: info.manufacturer || dash });
        document.getElementById('stat-model').textContent =
            t('machine.model', { value: info.model || dash });
        document.getElementById('stat-serial').textContent =
            t('machine.serial', { value: info.serial_number || dash });
        document.getElementById('stat-service').textContent =
            t('machine.service_tag', { value: info.service_tag || dash });
        document.getElementById('stat-asset').textContent =
            t('machine.asset_tag', { value: info.asset_tag || dash });
        showOperatingSystem(info);
        showDirectoryFacts(info);
    } catch (e) { /* non-critical */ }
}

// What this PC is running, with its build where one is known.
//
// The hub does the bucketing (normalize_os in app.py) and hands back the raw label, so this
// only decides how to say it. Hidden entirely when nothing is known: on a fleet whose
// agents predate OS reporting and whose hub has no directory sync, a permanent "--" reads
// as broken rather than as not-yet-collected.
//
// The build is appended rather than replacing the caption, because the two answer different
// questions -- "Windows 11 Pro" is what somebody asks for, "26100" is what decides whether
// a patch applies -- and an early Windows 11 machine reports a caption that still says 10,
// which is precisely when seeing both is worth something.
function showOperatingSystem(info) {
    const el = document.getElementById('stat-os');
    if (!el) return;
    const os = info.os || {};
    if (!os.label) { el.hidden = true; return; }
    el.hidden = false;
    const build = info.os_build ? t('machine.os_build', { build: info.os_build }) : '';
    el.textContent = t('machine.os', { value: os.label + build });
    // Where it came from. Only worth saying when it is NOT the machine's own answer.
    el.title = os.source === 'ad' ? t('inventory.os_from_directory') : '';
}

// Active Directory facts (roadmap #4). Each row appears only when the directory actually
// supplied it: on a hub with no AD these stay hidden rather than showing "--" forever,
// and a machine AD has no computer object for shows nothing rather than a stale OU.
function showDirectoryFacts(info) {
    const ouEl = document.getElementById('stat-ad-ou');
    const ownerEl = document.getElementById('stat-ad-owner');
    const statusEl = document.getElementById('stat-ad-status');
    if (!ouEl || !ownerEl || !statusEl) return;

    ouEl.hidden = !info.ad_ou;
    if (info.ad_ou) ouEl.textContent = t('machine.ad_ou', { value: info.ad_ou });

    ownerEl.hidden = !info.ad_owner;
    if (info.ad_owner) ownerEl.textContent = t('machine.ad_owner', { value: info.ad_owner });

    // Worth calling out: a machine still reporting telemetry whose computer account has
    // been disabled is usually a half-finished decommission, and nothing else on this
    // page would show it.
    statusEl.hidden = !info.ad_disabled;
    if (info.ad_disabled) {
        statusEl.replaceChildren();
        const pill = document.createElement('span');
        pill.className = 'status-pill status-pill--danger';
        const dot = document.createElement('span');
        dot.className = 'status-pill__dot';
        pill.append(dot, document.createTextNode(t('machine.ad_disabled')));
        statusEl.appendChild(pill);
    }
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
    followOpt.textContent = t('machine.sensor.follow_fleet');
    primarySensorSelect.appendChild(followOpt);

    primarySensorOrder.textContent = (body.preference && body.preference.length)
        ? t('machine.sensor_order', { order: body.preference.join(' → ') })
        : '';

    for (const s of body.sensors || []) {
        const opt = document.createElement('option');
        opt.value = s.name;
        // textContent, never innerHTML: these names come from the agent, and /api/report
        // is unauthenticated.
        opt.textContent = s.value === null || s.value === undefined
            ? s.name
            : t('machine.sensor_with_temp', { name: s.name, value: s.value });
        primarySensorSelect.appendChild(opt);
    }

    // A pinned sensor the machine isn't currently reporting would otherwise vanish from
    // the list and look unset. Show it, flagged, so the operator can see why the pin
    // isn't taking effect.
    if (savedPrimarySensor && !(body.sensors || []).some((s) => s.name === savedPrimarySensor)) {
        const missing = document.createElement('option');
        missing.value = savedPrimarySensor;
        missing.textContent = t('machine.sensor_missing', { name: savedPrimarySensor });
        primarySensorSelect.appendChild(missing);
    }

    primarySensorSelect.value = savedPrimarySensor;
    primarySensorSave.hidden = true;
    primarySensorStatus.textContent = (!body.sensors || !body.sensors.length)
        ? t('machine.sensor_none')
        : '';
}

// ------------------------------------------------------------------- release channel
//
// Which agent build this machine follows (roadmap #21). The picker is only built when the
// server said `can_manage`: an operator with `view` should see which train the PC is on --
// that is inventory -- without being handed a control that would 403.

const channelLine = document.getElementById('stat-channel');
const channelNote = document.getElementById('stat-channel-note');
const channelPicker = document.getElementById('channel-picker');
const channelSelect = document.getElementById('channel-select');
let savedChannel = '';

async function loadChannel() {
    const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/channel`);
    if (!resp.ok) return;
    const body = await resp.json();

    const label = (body.channels.find((c) => c.name === body.effective_channel) || {}).label
        || body.effective_channel;
    channelLine.textContent = body.pinned
        ? t('machine.channel_pinned', { value: label })
        : t('machine.channel', { value: label });

    // The line that stops a deliberately-stalled machine looking broken. Moving a PC off
    // beta does not roll it back -- every updater installs only what is strictly newer -- so
    // it sits on the build it has until stable passes it. Saying that here is the difference
    // between an explained state and a support call.
    channelNote.hidden = !body.ahead_of_stable;
    if (body.ahead_of_stable) {
        channelNote.textContent = t('machine.channel_ahead', {
            running: body.running_version, stable: body.latest_stable_version,
        });
    }

    channelPicker.hidden = !body.can_manage;
    if (!body.can_manage) return;

    savedChannel = body.channel || '';
    channelSelect.replaceChildren();
    const follow = document.createElement('option');
    follow.value = '';
    follow.textContent = t('machine.channel_follow_fleet');
    channelSelect.appendChild(follow);
    for (const c of body.channels) {
        const opt = document.createElement('option');
        opt.value = c.name;
        // textContent, never innerHTML -- these are catalog strings, but the rule here is
        // absolute rather than per-source.
        opt.textContent = c.label;
        channelSelect.appendChild(opt);
    }
    channelSelect.value = savedChannel;
}

let channelSaveSeq = 0;
channelSelect.addEventListener('change', saveChannel);

async function saveChannel() {
    if (channelSelect.value === savedChannel) return;
    const seq = ++channelSaveSeq;
    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/channel`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: channelSelect.value || null }),
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${resp.status}`);
        }
        if (seq !== channelSaveSeq) return;
        await loadChannel();
    } catch (e) {
        channelSelect.value = savedChannel;
        window.alert(e.message);
    }
}

loadChannel();

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
        primarySensorStatus.textContent = t('machine.sensor_saved');
    } catch (e) {
        if (seq !== primarySensorSaveSeq) return;
        primarySensorStatus.textContent = t('machine.sensor_save_failed', { error: e.message });
    }
}

// ---- Every sensor the machine reports ------------------------------------------
// The cards and charts above are a chosen dozen readings. This is the rest: the whole
// flattened LHM tree the agent already sends, grouped hardware -> category -> sensor, so
// the VRM temperature or the +12V rail is there when somebody needs it -- without any of
// them having to be promoted to a card first.
//
// Polled only while the section is open AND the tab is in front. Collapsed (the default)
// it costs nothing, which is what makes it affordable to show several hundred readings on
// a page that is otherwise a summary.
const sensorBrowserEl = document.getElementById('sensor-browser');
const sensorBrowserBody = document.getElementById('sensor-browser-body');
const sensorBrowserCount = document.getElementById('sensor-browser-count');
const sensorFilterEl = document.getElementById('sensor-filter');
const SENSOR_REFRESH_MS = 10000;        // the agent's own sensor reporting cadence
let sensorTree = [];
let sensorTimer = null;

async function loadAllSensors() {
    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/sensors/all`);
        if (!resp.ok) return;
        const body = await resp.json();
        sensorTree = Array.isArray(body.hardware) ? body.hardware : [];
        sensorBrowserCount.textContent =
            tPlural('machine.sensors.count', Number(body.count) || 0);
        renderSensorTree();
    } catch (e) { /* non-critical */ }
}

// Value as the AGENT formatted it (text: "61.0 °C", "1120.0 RPM"), falling back to the raw
// number. That is what lets this table show a sensor type the hub has never heard of with
// the right unit -- the agent knows LHM's vocabulary, and the hub does not have to.
function sensorValueText(sensor) {
    if (typeof sensor.text === 'string' && sensor.text) return sensor.text;
    if (typeof sensor.value === 'number' && Number.isFinite(sensor.value)) {
        return String(sensor.value);
    }
    return t('machine.unknown');
}

function renderSensorTree() {
    const needle = (sensorFilterEl.value || '').trim().toLowerCase();
    sensorBrowserBody.replaceChildren();
    let shown = 0;

    for (const hw of sensorTree) {
        const hwName = hw.name || '';
        const groups = [];
        for (const group of hw.groups || []) {
            const matches = (group.sensors || []).filter((s) => !needle ||
                `${hwName} ${group.name || ''} ${s.name || ''}`.toLowerCase().includes(needle));
            if (matches.length) groups.push({ name: group.name || '', sensors: matches });
        }
        if (!groups.length) continue;

        const section = document.createElement('div');
        section.className = 'sensor-hw';
        const heading = document.createElement('div');
        heading.className = 'sensor-hw__name';
        // textContent throughout: every name here came off an agent, and /api/report is
        // unauthenticated.
        heading.textContent = hwName;
        section.appendChild(heading);

        for (const group of groups) {
            const groupEl = document.createElement('div');
            groupEl.className = 'sensor-group';
            groupEl.textContent = group.name;
            section.appendChild(groupEl);

            const table = document.createElement('table');
            table.className = 'data-table sensor-table';
            const tbody = document.createElement('tbody');
            for (const sensor of group.sensors) {
                const tr = document.createElement('tr');
                const nameCell = document.createElement('td');
                nameCell.textContent = sensor.name || '';
                const valueCell = document.createElement('td');
                valueCell.className = 'sensor-table__value';
                valueCell.textContent = sensorValueText(sensor);
                tr.append(nameCell, valueCell);
                tbody.appendChild(tr);
                shown += 1;
            }
            table.appendChild(tbody);
            section.appendChild(table);
        }
        sensorBrowserBody.appendChild(section);
    }

    if (!shown) {
        const empty = document.createElement('div');
        empty.className = 'stat-card__meta';
        empty.textContent = needle ? t('machine.sensors.no_match') : t('machine.sensors.none');
        sensorBrowserBody.appendChild(empty);
    }
}

function syncSensorPolling() {
    const active = sensorBrowserEl.open && document.visibilityState === 'visible';
    if (active && sensorTimer === null) {
        loadAllSensors();
        sensorTimer = setInterval(loadAllSensors, SENSOR_REFRESH_MS);
    } else if (!active && sensorTimer !== null) {
        clearInterval(sensorTimer);
        sensorTimer = null;
    }
}

sensorBrowserEl.addEventListener('toggle', syncSensorPolling);
document.addEventListener('visibilitychange', syncSensorPolling);

// The History section folds too now, and Chart.js measures a canvas when it is built: one
// built inside a folded section comes back zero pixels tall and STAYS that way, because
// nothing re-measures it on the way out. Same fix as setPanelVisible() applies to a panel
// hidden for having no readings, applied to the whole section at once.
const historyCardEl = document.getElementById('history-card');
if (historyCardEl) {
    historyCardEl.addEventListener('toggle', () => {
        if (!historyCardEl.open) return;
        for (const panel of panels) {
            if (!panel.container.hidden) panel.chart.resize();
        }
    });
}
// Re-render from what we already hold: filtering is a view of the last poll, so typing
// doesn't wait on the network.
sensorFilterEl.addEventListener('input', renderSensorTree);

// ---- "Somebody is watching this" ------------------------------------------------
// A machine reports every five seconds normally, with a full sensor block every other
// report -- so the panels on this page, whose window is sixty seconds wide, drew about a
// dozen points a minute and a three-second spike was one dot or none. While this page is
// OPEN AND IN FRONT we tell the hub so, and that machine reports every second instead (see
// hub/live.py). Nobody looking, nothing extra: the cost is bounded by attention.
//
// Pinging IS the subscription, and there is no unsubscribe -- the watch lapses ~20 seconds
// after the last ping, which is what covers the tab being closed, the laptop sleeping, or
// the browser being killed, none of which get to send a farewell.
//
// Deliberately NOT conditional on follow mode: an operator who panned to look at the last
// thirty seconds is still watching this machine live, and every reading still lands on the
// panels whether the axis is following or not.
const LIVE_WATCH_MS = (Number(config.dataset.livePollSeconds) || 5) * 1000;
let liveWatchTimer = null;

function observingLive() {
    return viewingToday && document.visibilityState === 'visible';
}

async function pingLiveWatch() {
    if (!observingLive()) return;
    try {
        await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/live/watch`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: '{}' });
    } catch (e) {
        // A missed ping costs one slower cadence cycle; the next one renews. Never worth
        // anything visible on the page.
    }
}

// One timer for the life of the page rather than start/stop on every state change: the
// condition is re-tested each tick, so switching days, hiding the tab or coming back needs
// no wiring. Only the "came back to the tab" case is worth being prompt about, and that is
// the listener below -- otherwise the operator watches five-second steps for one more tick
// after alt-tabbing back.
liveWatchTimer = setInterval(pingLiveWatch, LIVE_WATCH_MS);
document.addEventListener('visibilitychange', pingLiveWatch);
pingLiveWatch();

// At 1 Hz a page left open all day would otherwise accumulate ~86k points per panel, so the
// live tail is bounded. Trimmed only while follow mode is on -- that is precisely when
// everything being dropped is off-screen to the left -- and generously, so panning back over
// the last couple of hours still finds the points it had. Anything older than the tail comes
// back from the history endpoint on the next viewport load, or on Reset.
const LIVE_TAIL_MS = 2 * 60 * 60 * 1000;

function trimLiveTail(chart, newestMs) {
    const data = chart.data.datasets[0].data;
    const cutoff = newestMs - LIVE_TAIL_MS;
    if (!data.length || data[0].x >= cutoff) return;
    let drop = 0;
    while (drop < data.length && data[drop].x < cutoff) drop += 1;
    data.splice(0, drop);
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
    // Follow an agent self-update without a refresh. Only present when the client
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
    // Re-anchor the window on this reading BEFORE the per-panel loop, and apply it to every
    // panel rather than only the ones that carried a value this tick -- a grid whose panels
    // sat on two different time axes because one sensor went quiet is exactly the confusion
    // the cross-panel mirroring elsewhere in this file exists to prevent.
    // Date.now() as well as x: a machine whose clock runs behind the browser's would
    // otherwise park the window in the past and never show the live edge.
    const followMax = followLive ? Math.max(x, Date.now()) : null;
    for (const p of panels) {
        if (followLive) {
            p.chart.options.scales.x.min = followMax - LIVE_WINDOW_MS;
            p.chart.options.scales.x.max = followMax;
        }
        const y = p.metric.key === 'temp' ? Number(msg.temp) : Number(diagnostics[p.metric.diag]);
        if (!Number.isFinite(y)) {
            if (followLive) p.chart.update('none');
            continue;
        }
        p.chart.data.datasets[0].data.push({ x, y });
        if (followLive) trimLiveTail(p.chart, followMax);
        p.emptyEl.style.display = 'none';
        p.chart.update('none');
    }
    if (selectedDayRange) selectedDayRange.endMs = Math.max(selectedDayRange.endMs, x);
    noDataEl.style.display = 'none';
});
