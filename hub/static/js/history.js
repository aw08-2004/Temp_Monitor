const zoomPlugin = window['chartjs-plugin-zoom'];
if (zoomPlugin) {
    Chart.register(zoomPlugin.default || zoomPlugin);
}

const rootStyles = getComputedStyle(document.documentElement);
const chartGridColor = rootStyles.getPropertyValue('--card-border').trim();
const chartTextColor = rootStyles.getPropertyValue('--text').trim();

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
            x: { type: 'time', time: { tooltipFormat: 'HH:mm:ss' }, title: { display: true, text: 'Time' }, grid: { color: chartGridColor } },
            y: { title: { display: true, text: 'Temperature (°C)' }, grid: { color: chartGridColor } }
        },
        plugins: {
            decimation: {
                enabled: true,
                algorithm: 'lttb',
                samples: 400
            },
            legend: { labels: { color: chartTextColor } },
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
        cell.className = 'stat-card__meta';
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
