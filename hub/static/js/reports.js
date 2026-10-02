// The fleet report page (roadmap #25): the selection as summary rows, the exports, and the
// fleet software catalog.
//
// **The export links always describe what is on screen.** The selection is a device group,
// ticked rows handed over from Devices (?machines=a,b), or the whole visible fleet, and the
// search box narrows it further. Each export link is rebuilt on every change so it names the
// same machines the table shows -- the rule the Devices export already keeps, because an
// export of "everything behind a filter you forgot was set" is the one that gets mailed to
// the wrong person. With no search, the link names the group rather than listing machines, so
// a group export stays a short URL and is re-resolved by the hub at download time.
//
// **Summary cells distinguish "none" from "not reported".** The hub sends a blank for a
// section the machine has not reported (reports.summary_row), and this page prints a dash for
// it rather than a 0 -- a zero in the Software column would read as a PC with nothing
// installed, which is exactly the misreading #25 exists to prevent.
//
// Scoping is the hub's. This page never decides which machines an operator may see.
(function () {
    'use strict';

    const body = document.getElementById('reports-body');
    if (!body) return;

    const groupEl = document.getElementById('reports-group');
    const searchEl = document.getElementById('reports-search');
    const countEl = document.getElementById('reports-count');
    const clearEl = document.getElementById('reports-clear-selection');
    const catalogBody = document.getElementById('catalog-body');
    const catalogSearch = document.getElementById('catalog-search');
    const catalogCount = document.getElementById('catalog-count');

    // Ticked rows from Devices. Held until the operator clears it or picks a group.
    let handedOver = new URLSearchParams(window.location.search).get('machines') || '';
    let rows = [];
    let visible = [];

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function dash(value) {
        return value === '' || value === null || value === undefined ? '—' : String(value);
    }

    function selectionQuery() {
        const params = new URLSearchParams();
        if (handedOver) params.set('machines', handedOver);
        else if (groupEl.value) params.set('group', groupEl.value);
        return params;
    }

    function exportQuery() {
        // A search narrows to exactly the rows shown; otherwise the selection itself.
        if (searchEl.value.trim()) {
            const params = new URLSearchParams();
            params.set('machines', visible.map((r) => r.machine).join(','));
            return params;
        }
        return selectionQuery();
    }

    function updateExports() {
        const query = exportQuery();
        document.querySelectorAll('[data-reports-export]').forEach((link) => {
            const kind = link.dataset.reportsExport;
            const params = new URLSearchParams(query);
            if (kind === 'json') {
                link.href = `/api/reports/export.json?${params}`;
            } else {
                params.set('section', kind);
                link.href = `/api/reports/export.csv?${params}`;
            }
            // Nothing selected: a link to an empty file is a click that seems to do nothing.
            link.hidden = visible.length === 0;
        });
    }

    function render() {
        const query = searchEl.value.trim().toLowerCase();
        visible = !query ? rows : rows.filter((r) =>
            [r.machine, r.model, r.os_caption, r.serial_number, r.ad_owner]
                .some((v) => String(v || '').toLowerCase().includes(query)));

        countEl.textContent = tPlural('reports.count', visible.length);
        clearEl.hidden = !handedOver;
        updateExports();

        if (visible.length === 0) {
            const tr = el('tr');
            const td = el('td', 'stat-card__meta', t('reports.empty'));
            td.colSpan = 7;
            tr.appendChild(td);
            body.replaceChildren(tr);
            return;
        }

        body.replaceChildren(...visible.map((r) => {
            const tr = el('tr');
            const name = el('td');
            const link = el('a', null, r.machine);
            link.href = `/reports/machines/${encodeURIComponent(r.machine)}`;
            name.appendChild(link);
            tr.appendChild(name);
            tr.appendChild(el('td', null, dash([r.manufacturer, r.model].filter(Boolean).join(' '))));
            tr.appendChild(el('td', null, dash(r.os_caption)));
            tr.appendChild(el('td', null, dash(r.software_count)));
            tr.appendChild(el('td', null, dash(r.pending_patches)));
            tr.appendChild(el('td', null, dash(r.unprotected_volumes)));
            tr.appendChild(el('td', null, dash(r.last_seen)));
            return tr;
        }));
    }

    async function loadRows() {
        let response;
        try {
            response = await fetch(`/api/reports/fleet?${selectionQuery()}`);
        } catch (e) {
            body.replaceChildren(el('tr'));
            countEl.textContent = t('report.load_failed');
            return;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            rows = [];
            render();
            countEl.textContent = data.error || t('report.load_failed');
            return;
        }
        rows = data.rows || [];
        render();
    }

    // ---- the fleet software catalog -------------------------------------------------
    let catalogTimer = null;

    async function showMachines(row, cell) {
        let response;
        try {
            const params = new URLSearchParams({ name: row.name, version: row.version });
            response = await fetch(`/api/software/catalog/machines?${params}`);
        } catch (e) {
            return;
        }
        if (!response.ok) return;
        const data = await response.json();
        const list = el('div', 'stat-card__meta');
        (data.machines || []).forEach((m, i) => {
            if (i) list.appendChild(document.createTextNode(', '));
            const link = el('a', null, m);
            link.href = `/reports/machines/${encodeURIComponent(m)}`;
            list.appendChild(link);
        });
        cell.replaceChildren(list);
    }

    async function loadCatalog() {
        const params = new URLSearchParams();
        const q = catalogSearch.value.trim();
        if (q) params.set('q', q);
        let response;
        try {
            response = await fetch(`/api/software/catalog?${params}`);
        } catch (e) {
            catalogCount.textContent = t('report.load_failed');
            return;
        }
        if (!response.ok) {
            catalogCount.textContent = t('report.load_failed');
            return;
        }
        const catalog = (await response.json()).catalog || [];
        catalogCount.textContent = tPlural('reports.catalog.count', catalog.length);
        if (catalog.length === 0) {
            const tr = el('tr');
            // Empty is worded as "nothing reported", not "nothing installed": until the agent
            // release that carries #25 B reaches a machine, it contributes nothing here.
            const td = el('td', 'stat-card__meta', t('reports.catalog.empty'));
            td.colSpan = 4;
            tr.appendChild(td);
            catalogBody.replaceChildren(tr);
            return;
        }
        catalogBody.replaceChildren(...catalog.map((row) => {
            const tr = el('tr');
            tr.appendChild(el('td', null, row.name));
            tr.appendChild(el('td', null, row.version));
            tr.appendChild(el('td', null, row.publisher));
            const cell = el('td');
            const button = el('button', 'btn btn--ghost', String(row.machines));
            button.type = 'button';
            button.title = t('reports.catalog.show_machines');
            button.addEventListener('click', () => showMachines(row, cell));
            cell.appendChild(button);
            tr.appendChild(cell);
            return tr;
        }));
    }

    // ---- shadow AI (roadmap #19) -----------------------------------------------------
    // Loaded once. It is a filter over the same inventory as the catalog, which does not
    // change while somebody is looking at this page.
    async function loadGenai() {
        const genaiBody = document.getElementById('genai-body');
        const watching = document.getElementById('genai-watching');
        if (!genaiBody) return;
        let response;
        try {
            response = await fetch('/api/software/genai');
        } catch (e) {
            watching.textContent = t('report.load_failed');
            return;
        }
        if (!response.ok) {
            watching.textContent = t('report.load_failed');
            return;
        }
        const data = await response.json();
        watching.textContent = t('reports.genai.watching', { names: (data.patterns || []).join(', ') });
        const findings = data.findings || [];
        if (findings.length === 0) {
            const tr = el('tr');
            const td = el('td', 'stat-card__meta', t('reports.genai.empty'));
            td.colSpan = 4;
            tr.appendChild(td);
            genaiBody.replaceChildren(tr);
            return;
        }
        genaiBody.replaceChildren(...findings.map((f) => {
            const tr = el('tr');
            const cell = el('td');
            const link = el('a', null, f.machine);
            link.href = `/reports/machines/${encodeURIComponent(f.machine)}`;
            cell.appendChild(link);
            tr.appendChild(cell);
            tr.appendChild(el('td', null, f.name));
            tr.appendChild(el('td', null, f.version));
            tr.appendChild(el('td', null, f.matched));
            return tr;
        }));
    }

    // ---- security posture (roadmap #25 D) ---------------------------------------------
    // Loaded once, like the shadow-AI list: a posture is re-read hourly on each machine, and
    // nothing about it changes while somebody is reading this page.
    const FAILING_SHOWN = 10;

    async function loadPosture() {
        const body = document.getElementById('posture-fleet-body');
        const coverage = document.getElementById('posture-coverage');
        if (!body || !window.PostureLabels) return;
        let response;
        try {
            response = await fetch('/api/posture/fleet');
        } catch (e) {
            coverage.textContent = t('report.load_failed');
            return;
        }
        if (!response.ok) {
            coverage.textContent = t('report.load_failed');
            return;
        }
        const data = await response.json();
        if (!data.reporting) {
            const tr = el('tr');
            const td = el('td', 'stat-card__meta', t('reports.posture.empty'));
            td.colSpan = 6;
            tr.appendChild(td);
            body.replaceChildren(tr);
            coverage.textContent = '';
            return;
        }
        coverage.textContent = t('reports.posture.coverage', {
            reporting: data.reporting, missing: data.not_reported,
        });
        body.replaceChildren(...(data.checks || []).map((row) => {
            const tr = el('tr');
            tr.appendChild(el('td', null, window.PostureLabels.checkTitle(row.id)));
            tr.appendChild(el('td', null, row.cis || ''));
            tr.appendChild(el('td', null, row.pass));
            tr.appendChild(el('td', null, row.fail));
            tr.appendChild(el('td', null, row.unknown));
            // Each failing machine links to its sheet, which is where the detail is. The list
            // is cut short on screen only; the count beside it is the whole number.
            const cell = el('td');
            (row.failing || []).slice(0, FAILING_SHOWN).forEach((machine, i) => {
                if (i) cell.appendChild(document.createTextNode(', '));
                const link = el('a', null, machine);
                link.href = `/reports/machines/${encodeURIComponent(machine)}`;
                cell.appendChild(link);
            });
            const more = (row.failing || []).length - FAILING_SHOWN;
            if (more > 0) {
                cell.appendChild(document.createTextNode(' '));
                cell.appendChild(el('span', 'stat-card__meta', t('reports.posture.more', { count: more })));
            }
            tr.appendChild(cell);
            return tr;
        }));
    }

    groupEl.addEventListener('change', () => {
        handedOver = '';
        loadRows();
    });
    clearEl.addEventListener('click', () => {
        handedOver = '';
        loadRows();
    });
    searchEl.addEventListener('input', render);
    catalogSearch.addEventListener('input', () => {
        clearTimeout(catalogTimer);
        catalogTimer = setTimeout(loadCatalog, 250);
    });

    loadRows();
    loadCatalog();
    loadGenai();
    loadPosture();
})();
