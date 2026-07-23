// Asset Inventory: the full roster of every machine the hub has ever seen, with
// online/offline status and a per-row hard-delete. Reads the same /api/machines the
// Dashboard uses (which carries a `status` field), but shows offline machines too.
//
// Search and sort are done client-side over the already-loaded rows. /api/machines is
// scope-filtered and, on the fleet sizes this hub serves, small enough that filtering in
// the browser is instant and avoids a round-trip per keystroke (roadmap #6 left the
// server-vs-client choice open and preferred client-side until fleet size demands
// otherwise). Sort state persists in localStorage so it survives the 30 s auto-refresh
// and a page reload.

const inventoryBody = document.getElementById('inventory-body');
const inventoryEmpty = document.getElementById('inventory-empty');
const inventoryNoMatch = document.getElementById('inventory-no-match');
const searchInput = document.getElementById('inventory-search');
const countEl = document.getElementById('inventory-count');
const headRow = document.getElementById('inventory-head');

const SORT_STORAGE_KEY = 'fleethub.inventory.sort';
// The columns a row can be searched against -- name plus the three identifiers.
const SEARCH_FIELDS = ['machine', 'asset_tag', 'serial_number', 'service_tag'];

let allRows = [];          // the last fetch, unfiltered/unsorted
let searchQuery = '';
let sort = loadSort();     // { key, dir: 'asc' | 'desc' }

function loadSort() {
    try {
        const saved = JSON.parse(localStorage.getItem(SORT_STORAGE_KEY));
        if (saved && saved.key && (saved.dir === 'asc' || saved.dir === 'desc')) {
            return saved;
        }
    } catch (e) { /* ignore malformed storage */ }
    // Default: online first, then alphabetical -- the machines you can act on sort up top.
    return { key: 'status', dir: 'asc' };
}

function saveSort() {
    try { localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(sort)); } catch (e) { /* private mode */ }
}

function formatLastSeen(updatedAt) {
    if (!updatedAt) return '--';
    // updated_at is a server-local "YYYY-MM-DD HH:MM:SS" string; show it as-is.
    return updatedAt;
}

function formatTemp(temp) {
    return (temp === null || temp === undefined) ? '--' : `${Number(temp).toFixed(1)} °C`;
}

// ---- sorting ------------------------------------------------------------------
// Each sortable column maps to a comparable key. Most are the raw field; a few need a
// derived value so the sort reads the way a human expects (online before offline,
// numeric temp, name as the tiebreak everywhere).
function sortValue(row, key) {
    switch (key) {
        case 'status':
            // Online sorts before offline in ascending order.
            return row.status === 'online' ? 0 : 1;
        case 'temp':
            // Missing temps sort last regardless of direction feel; -Infinity keeps them
            // at the bottom ascending and the numbers ordered.
            return (row.temp === null || row.temp === undefined) ? -Infinity : Number(row.temp);
        default:
            return (row[key] === null || row[key] === undefined) ? '' : row[key];
    }
}

function compareRows(a, b) {
    const key = sort.key;
    let av = sortValue(a, key);
    let bv = sortValue(b, key);
    let cmp;
    if (typeof av === 'number' && typeof bv === 'number') {
        cmp = av - bv;
    } else {
        cmp = String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: 'base' });
    }
    if (cmp === 0 && key !== 'machine') {
        // Stable, predictable tiebreak: machine name, always ascending.
        cmp = String(a.machine).localeCompare(String(b.machine), undefined, { sensitivity: 'base' });
    }
    return sort.dir === 'desc' ? -cmp : cmp;
}

function updateSortIndicators() {
    headRow.querySelectorAll('th[data-sort]').forEach((th) => {
        const active = th.dataset.sort === sort.key;
        th.setAttribute('aria-sort', active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
        th.classList.toggle('is-sorted', active);
        th.dataset.dir = active ? sort.dir : '';
    });
}

function onHeaderClick(key) {
    if (sort.key === key) {
        sort.dir = sort.dir === 'asc' ? 'desc' : 'asc';
    } else {
        sort.key = key;
        sort.dir = 'asc';
    }
    saveSort();
    render();
}

// ---- filtering ----------------------------------------------------------------
function matchesSearch(row) {
    if (!searchQuery) return true;
    return SEARCH_FIELDS.some((field) => {
        const value = row[field];
        return value && String(value).toLowerCase().includes(searchQuery);
    });
}

// ---- rendering ----------------------------------------------------------------
async function deleteMachine(machine, rowEl, btnEl) {
    if (!window.confirm(`Permanently delete "${machine}"?\n\nThis removes its identity, all temperature history, and its fleet enrollment. This cannot be undone.`)) {
        return;
    }
    btnEl.disabled = true;
    btnEl.textContent = 'Deleting…';
    try {
        const resp = await fetch('/api/machines/' + encodeURIComponent(machine), { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        allRows = allRows.filter((r) => r.machine !== machine);
        render();
    } catch (e) {
        btnEl.disabled = false;
        btnEl.textContent = 'Delete';
        window.alert(`Could not delete "${machine}": ${e.message}`);
    }
}

function renderRow(row) {
    const tr = document.createElement('tr');

    const nameTd = document.createElement('td');
    const link = document.createElement('a');
    link.href = '/machine/' + encodeURIComponent(row.machine);
    link.textContent = row.machine;
    nameTd.appendChild(link);

    const statusTd = document.createElement('td');
    const pill = document.createElement('span');
    pill.className = 'status-pill';
    const online = row.status === 'online';
    setStatusPill(pill, online ? 'ok' : 'muted', online ? 'Online' : 'Offline');
    statusTd.appendChild(pill);

    const modelTd = document.createElement('td');
    modelTd.textContent = row.model || '--';
    const serialTd = document.createElement('td');
    serialTd.textContent = row.serial_number || '--';
    const serviceTd = document.createElement('td');
    serviceTd.textContent = row.service_tag || '--';
    const assetTd = document.createElement('td');
    assetTd.textContent = row.asset_tag || '--';
    const tempTd = document.createElement('td');
    tempTd.textContent = formatTemp(row.temp);
    const seenTd = document.createElement('td');
    seenTd.textContent = formatLastSeen(row.updated_at);

    const actionTd = document.createElement('td');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--ghost';
    btn.style.color = 'var(--danger, #e5484d)';
    btn.textContent = 'Delete';
    btn.addEventListener('click', () => deleteMachine(row.machine, tr, btn));
    actionTd.appendChild(btn);

    tr.append(nameTd, statusTd, modelTd, serialTd, serviceTd, assetTd, tempTd, seenTd, actionTd);
    return tr;
}

function render() {
    updateSortIndicators();

    const total = allRows.length;
    inventoryEmpty.style.display = total ? 'none' : 'block';

    const visible = allRows.filter(matchesSearch).sort(compareRows);
    inventoryNoMatch.style.display = (total && !visible.length) ? 'block' : 'none';

    if (searchQuery && total) {
        countEl.textContent = `${visible.length} of ${total} machine${total === 1 ? '' : 's'}`;
    } else if (total) {
        countEl.textContent = `${total} machine${total === 1 ? '' : 's'}`;
    } else {
        countEl.textContent = '';
    }

    inventoryBody.replaceChildren();
    for (const row of visible) {
        inventoryBody.appendChild(renderRow(row));
    }
}

async function loadInventory() {
    try {
        const resp = await fetch('/api/machines');
        if (!resp.ok) return;
        allRows = await resp.json();
        render();
    } catch (e) {
        inventoryBody.innerHTML = '<tr><td colspan="9" class="stat-card__meta">Failed to load inventory.</td></tr>';
    }
}

// ---- wiring -------------------------------------------------------------------
headRow.querySelectorAll('th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => onHeaderClick(th.dataset.sort));
});

searchInput.addEventListener('input', () => {
    searchQuery = searchInput.value.trim().toLowerCase();
    render();
});

loadInventory();
// Keep status fresh without a manual reload. Search box and sort are preserved because
// render() reads them from module state, not the DOM rows.
setInterval(loadInventory, 30000);
