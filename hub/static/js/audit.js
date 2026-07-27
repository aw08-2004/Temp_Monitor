// Audit Log tab -- the read view over the hub's append-only audit trail.
//
// Three rules, in order of how badly breaking them hurts:
//
//  * The LEVEL PERIMETER IS THE SERVER'S. /api/audit returns only the levels this
//    operator may read (see audit_web.py); the level <select> here narrows what is
//    already permitted, it never unhides anything. Never filter security rows in this
//    file -- a row filtered here would still have been sent to the browser.
//  * Everything is built with textContent / createElement, never innerHTML. Actor,
//    action, target and the whole detail payload are operator- and agent-supplied.
//  * Paging is the server's keyset cursor, not an offset. Audit timestamps are whole
//    seconds and bulk operations land many rows on one, so "skip N" would duplicate and
//    drop lines while an operator reads.
//
// No polling: an audit trail is evidence, not a live dashboard, and a list that reorders
// itself under a reader is worse than one they refresh when they want to.

const auditHost = document.getElementById('audit-host');
const auditStatus = document.getElementById('audit-status');
const moreBtn = document.getElementById('audit-more');
const searchInput = document.getElementById('audit-search');
const actorInput = document.getElementById('audit-actor');
const actorList = document.getElementById('audit-actor-list');
const levelSelect = document.getElementById('audit-level');
const fromInput = document.getElementById('audit-from');
const toInput = document.getElementById('audit-to');
const clearBtn = document.getElementById('audit-clear');

// Labels are hard-coded, never the server's string: setStatusPill builds its label with
// innerHTML, so passing a value through from the API would be an injection point.
const LEVELS = {
    info: { label: 'Info', pill: 'muted' },
    notice: { label: 'Notice', pill: 'ok' },
    security: { label: 'Security', pill: 'danger' },
};

let cursor = null;          // {ts, id} from the last page, or null for the first
let loading = false;
let searchDebounce = null;
let levelOptionsBuilt = false;
let tbody = null;           // the current table body, appended to when paging

async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
    if (!resp.ok) {
        throw new Error((body && body.error) || `HTTP ${resp.status}`);
    }
    return body;
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function formatEpoch(epoch) {
    return epoch ? new Date(epoch * 1000).toLocaleString() : '--';
}

function buildQuery(append) {
    const params = new URLSearchParams();
    const q = searchInput.value.trim();
    const actor = actorInput.value.trim();
    if (q) params.set('q', q);
    if (actor) params.set('actor', actor);
    if (levelSelect.value) params.set('level', levelSelect.value);
    if (fromInput.value) params.set('from', fromInput.value);
    if (toInput.value) params.set('to', toInput.value);
    if (append && cursor) {
        params.set('before_ts', cursor.ts);
        params.set('before_id', cursor.id);
    }
    return params.toString();
}

// ---------------------------------------------------------------- rendering

function buildTable() {
    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const label of ['Time', 'Level', 'Actor', 'Action', 'Target', '']) {
        headRow.appendChild(el('th', null, label));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);
    const body = document.createElement('tbody');
    table.appendChild(body);
    card.appendChild(table);
    auditHost.replaceChildren(card);
    return body;
}

// One entry is two rows: the summary, and a detail row that starts hidden and is filled
// on first expand (most entries are never expanded, and some detail payloads are large).
function appendEntry(entry) {
    const tr = document.createElement('tr');
    tr.appendChild(el('td', null, formatEpoch(entry.ts)));

    const levelTd = document.createElement('td');
    const meta = LEVELS[entry.level] || { label: 'Unknown', pill: 'warn' };
    const pill = el('span', 'status-pill');
    setStatusPill(pill, meta.pill, meta.label);
    levelTd.appendChild(pill);
    tr.appendChild(levelTd);

    tr.appendChild(el('td', null, entry.actor || '--'));
    tr.appendChild(el('td', null, entry.action || '--'));
    tr.appendChild(el('td', null, entry.target === null || entry.target === undefined
        ? '--' : String(entry.target)));

    const actionTd = document.createElement('td');
    const toggle = el('button', 'btn btn--ghost', 'Details');
    toggle.type = 'button';
    actionTd.appendChild(toggle);
    tr.appendChild(actionTd);

    const detailTr = document.createElement('tr');
    detailTr.className = 'audit-detail';
    detailTr.hidden = true;
    const detailTd = document.createElement('td');
    detailTd.colSpan = 6;
    detailTr.appendChild(detailTd);

    let filled = false;
    toggle.addEventListener('click', () => {
        if (!filled) {
            if (entry.detail === null || entry.detail === undefined) {
                detailTd.appendChild(el('p', 'stat-card__meta', 'No detail recorded.'));
            } else {
                detailTd.appendChild(el('pre', null, JSON.stringify(entry.detail, null, 2)));
            }
            filled = true;
        }
        detailTr.hidden = !detailTr.hidden;
        toggle.textContent = detailTr.hidden ? 'Details' : 'Hide';
    });

    tbody.appendChild(tr);
    tbody.appendChild(detailTr);
}

function renderEmpty() {
    const empty = el('div', 'empty-state');
    empty.appendChild(el('p', null, hasFilters()
        ? 'No audit entries match these filters.'
        : 'No audit entries yet.'));
    auditHost.replaceChildren(empty);
}

function hasFilters() {
    return Boolean(searchInput.value.trim() || actorInput.value.trim()
        || levelSelect.value || fromInput.value || toInput.value);
}

// The Security option only exists for operators who hold view_security_audit -- offering
// a filter that can only ever return nothing would read as a bug in the log.
function buildLevelOptions(levels) {
    if (levelOptionsBuilt) return;
    for (const level of levels) {
        const meta = LEVELS[level];
        if (!meta) continue;
        const option = document.createElement('option');
        option.value = level;
        option.textContent = meta.label;
        levelSelect.appendChild(option);
    }
    levelOptionsBuilt = true;
}

// ---------------------------------------------------------------- loading

async function load(append) {
    if (loading) return;
    loading = true;
    moreBtn.disabled = true;
    auditStatus.textContent = append ? 'Loading more…' : 'Loading…';
    try {
        const page = await api('/api/audit?' + buildQuery(append));
        buildLevelOptions(page.levels || []);
        if (!append) {
            if (!page.entries.length) {
                renderEmpty();
                moreBtn.hidden = true;
                auditStatus.textContent = '';
                return;
            }
            tbody = buildTable();
        }
        for (const entry of page.entries) appendEntry(entry);
        cursor = page.next_cursor;
        moreBtn.hidden = !page.has_more;
        const shown = tbody ? tbody.querySelectorAll('tr:not(.audit-detail)').length : 0;
        auditStatus.textContent = page.has_more
            ? `Showing the ${shown} most recent matching entries.`
            : `Showing all ${shown} matching entries.`;
    } catch (e) {
        auditStatus.textContent = `Could not load the audit log: ${e.message}`;
        moreBtn.hidden = true;
    } finally {
        loading = false;
        moreBtn.disabled = false;
    }
}

function reload() {
    cursor = null;
    tbody = null;
    load(false);
}

async function loadActors() {
    try {
        const body = await api('/api/audit/actors');
        actorList.replaceChildren();
        for (const actor of body.actors || []) {
            const option = document.createElement('option');
            option.value = actor;
            actorList.appendChild(option);
        }
    } catch (e) {
        // A missing suggestion list is a cosmetic loss; the filter still works by typing.
    }
}

for (const input of [searchInput, actorInput]) {
    input.addEventListener('input', () => {
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(reload, 250);
    });
}
for (const input of [levelSelect, fromInput, toInput]) {
    input.addEventListener('change', reload);
}
moreBtn.addEventListener('click', () => load(true));
clearBtn.addEventListener('click', () => {
    searchInput.value = '';
    actorInput.value = '';
    levelSelect.value = '';
    fromInput.value = '';
    toInput.value = '';
    reload();
});

loadActors();
load(false);
