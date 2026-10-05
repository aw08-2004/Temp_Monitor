// Tools page, Files tab: the disk usage History panel (roadmap #27).
//
// **The questions, in the order an operator asks them.** How full is this volume and when
// will it be full (the chart, with the projection drawn on it); what made it grow (the folders
// and the files that changed between two scans); how has THIS folder or file grown (its own
// chart, for any path at any depth); and what did this folder look like on some past day (the
// compare picker, which adds a column to the folder list beside the panel). The first works
// for every Windows machine, because the hub builds it from volume sensors agents have sent for
// years. The rest need the daily MFT scan, and the panel says so instead of drawing empty tables.
//
// **It follows the browser rather than holding its own place.** files.js calls follow(path)
// after every navigation: the volume picker moves to the drive being browsed, and the folder
// chart is for the folder on screen. A change row navigates the browser, which then calls
// follow() back -- one source of truth for "where are we", which is the browser's.
//
// **Nothing here is live**, for the reason files.js gives: this is history, measured once a
// day. It is fetched when the panel opens and when the volume or day changes, and never polled.
//
// Same two rules as the rest of the console: textContent/createElement only -- every path in
// here is remote text -- and every string from the catalog.

(function () {
    'use strict';

    const section = document.getElementById('files-history');
    if (!section) return;

    const t = window.t;
    const tPlural = window.tPlural;

    const volumeSelect = document.getElementById('files-history-volume');
    const closeBtn = document.getElementById('files-history-close');
    const forecastEl = document.getElementById('files-history-forecast');
    const errorEl = document.getElementById('files-history-error');
    const chartCanvas = document.getElementById('files-history-chart');
    const pathWrap = document.getElementById('files-history-path');
    const pathTitle = document.getElementById('files-history-path-title');
    const pathCanvas = document.getElementById('files-history-path-chart');
    const compareSelect = document.getElementById('files-history-compare');
    const filesWrap = document.getElementById('files-history-files');
    const filesNote = document.getElementById('files-history-files-note');
    const filesBody = document.getElementById('files-history-files-body');
    const daySelect = document.getElementById('files-history-day');
    const changesNote = document.getElementById('files-history-changes-note');
    const changesBody = document.getElementById('files-history-changes');
    const largeWrap = document.getElementById('files-history-large');
    const largeBody = document.getElementById('files-history-large-body');

    const styles = getComputedStyle(document.documentElement);
    const token = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
    const GB = 1024 ** 3;

    let machine = null;
    let volume = null;
    let folder = null;             // the folder being browsed, or null on the drive list
    let picked = null;             // a path chosen with Size history; wins over `folder`
    let navigateTo = null;         // files.js's navigate(), for clicks on a change row
    let generation = 0;            // orphans a fetch that lands after a close or a switch
    let chart = null;
    let pathChart = null;

    // ================================================================
    // plumbing
    // ================================================================
    async function api(url) {
        const resp = await fetch(url);
        let payload = null;
        try { payload = await resp.json(); } catch (e) { /* empty body */ }
        if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
        return payload;
    }

    function base() {
        return `/api/disk-usage/machines/${encodeURIComponent(machine)}`;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function formatSize(bytes) {
        if (bytes === null || bytes === undefined || !Number.isFinite(Number(bytes))) return '—';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let value = Math.abs(Number(bytes));
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
        const sign = Number(bytes) < 0 ? '-' : '';
        return unit === 0 ? `${sign}${value} B` : `${sign}${value.toFixed(1)} ${units[unit]}`;
    }

    function formatDelta(bytes) {
        return bytes > 0 ? `+${formatSize(bytes)}` : formatSize(bytes);
    }

    function formatDay(day) {
        return new Date(`${day}T00:00:00`).toLocaleDateString();
    }

    function volumeOf(path) {
        const text = String(path || '');
        return /^[A-Za-z]:/.test(text) ? text.slice(0, 2).toUpperCase() : null;
    }

    function showError(message) {
        errorEl.textContent = message || '';
        errorEl.hidden = !message;
    }

    // ================================================================
    // the volume chart and forecast
    // ================================================================
    async function loadVolumes() {
        const mine = generation;
        const summary = await api(base());
        if (mine !== generation) return;
        volumeSelect.replaceChildren();
        const volumes = (summary.volumes || []).map((v) => v.volume);
        volumes.forEach((letter) => volumeSelect.appendChild(el('option', null, letter)));
        if (!volumes.length) {
            volume = null;
            forecastEl.textContent = t('files.history.no_data');
            return;
        }
        if (!volume || !volumes.includes(volume)) volume = volumes[0];
        volumeSelect.value = volume;
    }

    async function loadVolume() {
        if (!volume) return;
        const mine = generation;
        const [history, changes] = await Promise.all([
            api(`${base()}/history?volume=${encodeURIComponent(volume)}`),
            api(`${base()}/changes?volume=${encodeURIComponent(volume)}`)
        ]);
        if (mine !== generation) return;
        drawChart(history);
        renderForecast(history.forecast);
        renderCompareDays(history.history_days || []);
        renderChanges(changes);
        await loadPath();
    }

    /**
     * Used space per day, the volume's size as a flat ceiling, and -- when the forecast says
     * the volume is filling -- a dashed line from the last point to the day it meets that
     * ceiling. The projection is capped at a year ahead so a slow fill does not squash the
     * real history into the left edge of the chart.
     */
    function drawChart(history) {
        const points = history.points || [];
        const used = points.map((p) => ({ x: Date.parse(`${p.day}T00:00:00`), y: p.used_bytes / GB }));
        const total = points.length ? points[points.length - 1].total_bytes / GB : null;
        const datasets = [{
            label: t('files.history.used'),
            data: used,
            borderColor: token('--accent', '#3b82f6'),
            backgroundColor: 'transparent',
            borderWidth: 2, tension: 0.2, pointRadius: 2, parsing: false
        }];

        const forecast = history.forecast || {};
        let lastX = used.length ? used[used.length - 1].x : null;
        if (forecast.status === 'filling' && used.length && forecast.full_on) {
            const horizon = Date.now() + 365 * 86400000;
            const fullX = Math.min(Date.parse(`${forecast.full_on}T00:00:00`), horizon);
            const lastY = used[used.length - 1].y;
            const fullY = fullX >= horizon
                ? lastY + (forecast.bytes_per_day / GB) * ((fullX - used[used.length - 1].x) / 86400000)
                : total;
            datasets.push({
                label: t('files.history.projection'),
                data: [{ x: used[used.length - 1].x, y: lastY }, { x: fullX, y: fullY }],
                borderColor: token('--warning', '#eab308'),
                borderDash: [6, 4], backgroundColor: 'transparent',
                borderWidth: 2, pointRadius: 0, parsing: false
            });
            lastX = fullX;
        }
        if (total !== null && used.length) {
            datasets.push({
                label: t('files.history.capacity'),
                data: [{ x: used[0].x, y: total }, { x: lastX, y: total }],
                borderColor: token('--muted', '#8b8b95'),
                borderDash: [2, 3], backgroundColor: 'transparent',
                borderWidth: 1, pointRadius: 0, parsing: false
            });
        }

        if (chart) chart.destroy();
        if (!window.Chart) return;
        chart = new window.Chart(chartCanvas.getContext('2d'), {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
                interaction: { mode: 'nearest', intersect: false },
                scales: {
                    x: { type: 'time', time: { tooltipFormat: 'PP' },
                         grid: { color: token('--card-border', '#333') } },
                    y: { min: 0, title: { display: true, text: 'GB' },
                         grid: { color: token('--card-border', '#333') } }
                },
                plugins: {
                    legend: { display: true, position: 'bottom' },
                    tooltip: { callbacks: { label: (ctx) =>
                        `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} GB` } }
                }
            }
        });
    }

    function renderForecast(forecast) {
        if (!forecast) { forecastEl.textContent = ''; return; }
        const rate = forecast.bytes_per_day_7d !== null && forecast.bytes_per_day_7d !== undefined
            ? t('files.history.rate_week', { rate: formatDelta(forecast.bytes_per_day_7d) })
            : '';
        let text;
        if (forecast.status === 'filling') {
            text = t('files.history.forecast_filling', {
                date: formatDay(forecast.full_on),
                days: Math.round(forecast.days_to_full),
                rate: formatDelta(forecast.bytes_per_day)
            });
            text += ` ${CONFIDENCE[forecast.confidence] ? CONFIDENCE[forecast.confidence]() : ''}`;
        } else if (forecast.status === 'not_filling') {
            text = t('files.history.forecast_not_filling', {
                rate: formatDelta(forecast.bytes_per_day || 0)
            });
        } else {
            text = tPlural('files.history.forecast_insufficient', forecast.points || 0);
        }
        forecastEl.textContent = rate ? `${text} ${rate}` : text;
    }

    // One literal key per value, so the catalog scan can see every one of them.
    const CONFIDENCE = {
        high: () => t('files.history.confidence.high'),
        medium: () => t('files.history.confidence.medium'),
        low: () => t('files.history.confidence.low')
    };

    // ================================================================
    // what changed
    // ================================================================
    function renderChanges(payload) {
        // The day picker lists every day with a stored change list; the newest is selected.
        const keep = daySelect.value;
        daySelect.replaceChildren();
        (payload.days || []).forEach((day) => {
            const option = el('option', null, formatDay(day));
            option.value = day;
            daySelect.appendChild(option);
        });
        daySelect.value = payload.day || keep || '';
        daySelect.hidden = !(payload.days || []).length;

        changesBody.replaceChildren();
        largeBody.replaceChildren();
        const changes = payload.changes || [];
        const large = payload.new_large_files || [];

        if (!payload.day) {
            changesNote.textContent = t('files.history.no_scans');
        } else if (!payload.previous_scanned_at) {
            // The first scan: nothing to compare with, which is not "nothing changed".
            changesNote.textContent = t('files.history.first_scan');
        } else if (!changes.length) {
            changesNote.textContent = t('files.history.no_changes', {
                since: new Date(payload.previous_scanned_at * 1000).toLocaleString()
            });
        } else {
            changesNote.textContent = t('files.history.changes_since', {
                since: new Date(payload.previous_scanned_at * 1000).toLocaleString(),
                when: new Date(payload.scanned_at * 1000).toLocaleString()
            });
        }

        changes.forEach((change) => {
            const row = el('tr');
            const cell = el('td');
            const link = el('button', 'files-name files-name--dir', change.path);
            link.type = 'button';
            link.addEventListener('click', () => { if (navigateTo) navigateTo(change.path); });
            cell.appendChild(link);
            row.appendChild(cell);
            row.appendChild(el('td', change.delta > 0 ? 'files-delta files-delta--up'
                                                      : 'files-delta files-delta--down',
                               formatDelta(change.delta)));
            row.appendChild(el('td', null, formatSize(change.after)));
            changesBody.appendChild(row);
        });

        large.forEach((file) => {
            const row = el('tr');
            row.appendChild(el('td', 'files-name', file.path));
            row.appendChild(el('td', null, formatSize(file.size)));
            largeBody.appendChild(row);
        });
        largeWrap.hidden = !large.length;
        renderFileChanges(payload.file_changes);
    }

    /** The same scan, file by file, from the full-depth history. A file's name opens the
     *  folder that holds it, with the file's own chart below. */
    function renderFileChanges(result) {
        filesBody.replaceChildren();
        const rows = (result && result.files) || [];
        filesWrap.hidden = !result || (!rows.length && !result.first);
        if (filesWrap.hidden) return;
        filesNote.textContent = result.first
            ? t('files.history.first_scan')
            : (result.total > rows.length
                ? t('files.history.files_more', { count: result.total, shown: rows.length })
                : '');
        rows.forEach((file) => {
            const row = el('tr');
            const cell = el('td');
            const link = el('button', 'files-name', file.path);
            link.type = 'button';
            link.addEventListener('click', () => {
                picked = file.path;
                const parent = file.path.slice(0, file.path.lastIndexOf('\\')) || volume;
                if (navigateTo) navigateTo(parent.length === 2 ? `${parent}\\` : parent);
                loadPath().catch((e) => showError(e.message));
            });
            cell.appendChild(link);
            if (file.status === 'new') cell.appendChild(el('span', 'files-badge', t('files.history.status.new')));
            if (file.status === 'deleted') cell.appendChild(el('span', 'files-badge', t('files.history.status.deleted')));
            row.appendChild(cell);
            row.appendChild(el('td', file.delta > 0 ? 'files-delta files-delta--up'
                                                    : 'files-delta files-delta--down',
                               formatDelta(file.delta)));
            row.appendChild(el('td', null, file.status === 'deleted' ? '—' : formatSize(file.after)));
            filesBody.appendChild(row);
        });
    }

    /** The compare picker: every day the hub can rebuild, newest first. */
    function renderCompareDays(days) {
        const keep = compareSelect.value;
        compareSelect.replaceChildren();
        const none = el('option', null, t('files.history.compare_none'));
        none.value = '';
        compareSelect.appendChild(none);
        days.forEach((d) => {
            const option = el('option', null, formatDay(d.day));
            option.value = d.day;
            compareSelect.appendChild(option);
        });
        compareSelect.value = days.some((d) => d.day === keep) ? keep : '';
        compareSelect.disabled = !days.length;
    }

    async function loadDay(day) {
        const mine = generation;
        const payload = await api(`${base()}/changes?volume=${encodeURIComponent(volume)}`
                                  + `&day=${encodeURIComponent(day)}`);
        if (mine !== generation) return;
        renderChanges(payload);
    }

    // ================================================================
    // one folder or file
    // ================================================================
    /**
     * The size history of one path: the file picked with Size history, else the folder being
     * browsed. Drawn as a stepped line, because the hub stores change points -- the value holds
     * until the next one -- and joining them diagonally would invent the days in between.
     */
    async function loadPath() {
        const target = picked || folder;
        if (!target || volumeOf(target) !== volume) {
            pathWrap.hidden = true;
            return;
        }
        const mine = generation;
        const params = new URLSearchParams({ volume, path: target });
        const payload = await api(`${base()}/path-history?${params}`);
        if (mine !== generation) return;
        const points = payload.points || [];
        pathWrap.hidden = false;
        if (pathChart) { pathChart.destroy(); pathChart = null; }
        if (!payload.known || !points.length) {
            pathTitle.textContent = t('files.history.path_unknown', { path: target });
            pathCanvas.hidden = true;
            return;
        }
        pathCanvas.hidden = false;
        const live = points.filter((p) => !p.deleted);
        const first = live.length ? live[0].allocated : 0;
        const last = points[points.length - 1];
        pathTitle.textContent = last.deleted
            ? t('files.history.path_deleted', { path: target, date: formatDay(last.day) })
            : t('files.history.path_title', {
                path: target,
                delta: formatDelta((last.allocated || 0) - (first || 0)),
                since: formatDay(points[0].day)
            });
        // Carried to today, so a value that has not changed since March still reaches the
        // right edge instead of stopping at its only point.
        const data = points.map((p) => ({ x: Date.parse(`${p.day}T00:00:00`),
                                          y: p.deleted ? null : p.allocated / GB }));
        if (!last.deleted) data.push({ x: Date.now(), y: last.allocated / GB });
        if (!window.Chart) return;
        pathChart = new window.Chart(pathCanvas.getContext('2d'), {
            type: 'line',
            data: { datasets: [{
                label: target, data, stepped: 'before', spanGaps: false,
                borderColor: token('--accent', '#3b82f6'), backgroundColor: 'transparent',
                borderWidth: 2, pointRadius: 2, parsing: false
            }] },
            options: {
                responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
                scales: {
                    x: { type: 'time', time: { tooltipFormat: 'PP' },
                         grid: { color: token('--card-border', '#333') } },
                    y: { min: 0, title: { display: true, text: 'GB' },
                         grid: { color: token('--card-border', '#333') } }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: (ctx) => formatSize(ctx.parsed.y * GB) } }
                }
            }
        });
    }

    // ================================================================
    // lifecycle
    // ================================================================
    async function refresh() {
        showError('');
        try {
            await loadVolumes();
            await loadVolume();
        } catch (e) {
            showError(e.message);
        }
    }

    function close() {
        generation++;
        section.hidden = true;
        picked = null;
        if (chart) { chart.destroy(); chart = null; }
        if (pathChart) { pathChart.destroy(); pathChart = null; }
        // The comparison is part of this panel; closing it takes the extra column away too.
        compareSelect.value = '';
        if (window.FilesBrowser) window.FilesBrowser.setCompare(null);
    }

    volumeSelect.addEventListener('change', () => {
        volume = volumeSelect.value;
        loadVolume().catch((e) => showError(e.message));
    });
    compareSelect.addEventListener('change', () => {
        if (window.FilesBrowser) window.FilesBrowser.setCompare(compareSelect.value || null);
    });
    daySelect.addEventListener('change', () => {
        loadDay(daySelect.value).catch((e) => showError(e.message));
    });
    closeBtn.addEventListener('click', () => {
        close();
        const opener = document.getElementById('files-history-btn');
        if (opener) { opener.setAttribute('aria-expanded', 'false'); opener.focus(); }
    });

    window.FilesHistory = {
        /** Open or close the panel. Returns whether it is now open. */
        toggle(options) {
            if (!section.hidden) { close(); return false; }
            machine = options.machine;
            navigateTo = options.navigate;
            folder = options.path || null;
            volume = volumeOf(folder) || volume;
            section.hidden = false;
            generation++;
            refresh();
            return true;
        },
        /** Open the panel on one folder's or file's history (the menu's Size history). */
        showPath(path, options) {
            if (section.hidden) {
                window.FilesHistory.toggle(options);
            }
            picked = path;
            const letter = volumeOf(path);
            if (letter && letter !== volume
                && [...volumeSelect.options].some((o) => o.value === letter)) {
                volume = letter;
                volumeSelect.value = letter;
                loadVolume().catch((e) => showError(e.message));
            } else {
                loadPath().catch((e) => showError(e.message));
            }
            section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        },
        /** The browser moved. Follow it, if the panel is open. */
        follow(path) {
            folder = path || null;
            // A file picked from the list stays on screen while its own folder is shown; any
            // other move means the operator has moved on from it.
            if (picked && volumeOf(picked) && folder
                && picked.toLowerCase().slice(0, picked.lastIndexOf('\\')) !== folder.toLowerCase()
                && picked.toLowerCase() !== folder.toLowerCase()) picked = null;
            if (section.hidden) return;
            const letter = volumeOf(folder);
            if (letter && letter !== volume && [...volumeSelect.options].some((o) => o.value === letter)) {
                volume = letter;
                volumeSelect.value = letter;
                loadVolume().catch((e) => showError(e.message));
            } else {
                loadPath().catch((e) => showError(e.message));
            }
        },
        /** The machine changed: forget everything about the last one. */
        reset() {
            close();
            machine = null;
            volume = null;
            folder = null;
        }
    };
})();
