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
// The columns a row can be searched against -- name, the three identifiers, and the
// manufacturer, so "dell" narrows the list to one vendor's machines. Every field here has
// a visible column: a row that matches on something not on screen looks like a bug.
// os_label, not the nested row.os.label: this list is read flat, and the hub flattens the
// label onto the row for exactly that reason. "windows 10" narrows the fleet to the
// machines still on it, which is the question this column exists to answer.
const SEARCH_FIELDS = ['machine', 'asset_tag', 'serial_number', 'service_tag',
                       'manufacturer', 'os_label'];

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
            // Online before offline, and within each the machines the console can actually
            // act on first: online+enrolled, online+not enrolled, offline, offline+not
            // enrolled. An unenrolled machine is the one you can do least with, so it does
            // not outrank a working one just for being unusual -- but it groups together,
            // which is what makes "how many of these do I have?" a glance rather than a scan.
            return (row.status === 'online' ? 0 : 2) + (row.enrolled === false ? 1 : 0);
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
    if (!window.confirm(t('inventory.confirm_delete', { machine }))) {
        return;
    }
    btnEl.disabled = true;
    btnEl.textContent = t('common.deleting');
    try {
        const resp = await fetch('/api/machines/' + encodeURIComponent(machine), { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        allRows = allRows.filter((r) => r.machine !== machine);
        render();
    } catch (e) {
        btnEl.disabled = false;
        btnEl.textContent = t('common.delete');
        window.alert(t('inventory.delete_failed', { machine, error: e.message }));
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
    setMachineStatusPill(pill, row);
    statusTd.appendChild(pill);

    const makeTd = document.createElement('td');
    makeTd.textContent = row.manufacturer || '--';
    const modelTd = document.createElement('td');
    modelTd.textContent = row.model || '--';
    // textContent, like every other cell: this string is whatever a machine reported to the
    // unauthenticated /api/report, or whatever a directory returned.
    const osTd = document.createElement('td');
    osTd.textContent = row.os_label || '--';
    // Where it came from, as a tooltip rather than a second column: on a hub with directory
    // sync the difference between "the machine said so" and "AD said so last night" matters
    // when the two disagree, and nowhere else.
    if (row.os && row.os.source === 'ad') osTd.title = t('inventory.os_from_directory');
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
    actionTd.className = 'data-table__actions';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--ghost';
    btn.style.color = 'var(--danger, #e5484d)';
    btn.textContent = t('common.delete');
    btn.addEventListener('click', () => deleteMachine(row.machine, tr, btn));
    actionTd.appendChild(btn);

    tr.append(nameTd, statusTd, makeTd, modelTd, osTd, serialTd, serviceTd, assetTd, tempTd,
              seenTd, actionTd);
    return tr;
}

function render() {
    updateSortIndicators();

    const total = allRows.length;
    inventoryEmpty.style.display = total ? 'none' : 'block';

    const visible = allRows.filter(matchesSearch).sort(compareRows);
    inventoryNoMatch.style.display = (total && !visible.length) ? 'block' : 'none';

    // Pluralised through tPlural rather than a trailing `s`: the count and its noun agree
    // differently per language, and a hand-built "machine(s)" cannot be translated at all.
    if (searchQuery && total) {
        countEl.textContent = tPlural('inventory.count_filtered', total, { visible: visible.length });
    } else if (total) {
        countEl.textContent = tPlural('inventory.count', total);
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
        // Built as DOM rather than an innerHTML string: the message is now catalog text,
        // and a translation is not something to interpolate into markup.
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 10;
        td.className = 'stat-card__meta';
        td.textContent = t('inventory.load_failed');
        tr.appendChild(td);
        inventoryBody.replaceChildren(tr);
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

// Wake every offline PC in scope (roadmap #10). The hub decides who relays for whom, so
// this sends no machine list at all -- narrowing it here would mean the console and the
// scheduler disagreed about which machines are asleep, and the console's copy is up to
// thirty seconds old.
//
// The answer is a set of COUNTS by outcome, not a success, because that is what actually
// happened: some PCs were already awake, some have no wired adapter, and some are waiting
// for a peer on their subnet to come online. Reporting "woken" over that would be a claim
// nothing supports -- nothing acknowledges a magic packet.
const wakeAllBtn = document.getElementById('inventory-wake-all');
if (wakeAllBtn) {
    const wakeStatus = document.getElementById('inventory-wake-status');
    wakeAllBtn.addEventListener('click', async () => {
        wakeAllBtn.disabled = true;
        wakeStatus.textContent = '';
        try {
            const resp = await fetch('/api/wake/fleet', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason: 'inventory page' }),
            });
            const payload = await resp.json().catch(() => null);
            if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
            const counts = (payload && payload.counts) || {};
            // Only the requests that will actually send a packet are counted here.
            // Including the already-awake ones would report forty wakes on a fleet that
            // was never asleep.
            const asked = (counts.pending || 0) + (counts.relaying || 0) + (counts.sent || 0);
            wakeStatus.textContent = asked
                ? t('inventory.wake_all_result', { count: asked })
                : t('inventory.wake_all_none');
            loadInventory();
        } catch (e) {
            wakeStatus.textContent = `${t('inventory.wake_all_failed')} ${e.message}`;
        } finally {
            wakeAllBtn.disabled = false;
        }
    });
}

loadInventory();
// Keep status fresh without a manual reload. Search box and sort are preserved because
// render() reads them from module state, not the DOM rows.
setInterval(loadInventory, 30000);
