// Devices (formerly Asset Inventory): the full roster of every machine the hub has ever seen,
// with online/offline status, and -- since hub 1.113.0 -- the place to act on them. Reads the
// same /api/machines the Dashboard uses (which carries a `status` field), but shows offline
// machines too.
//
// Search and sort are done client-side over the already-loaded rows. /api/machines is
// scope-filtered and, on the fleet sizes this hub serves, small enough that filtering in
// the browser is instant and avoids a round-trip per keystroke (roadmap #6 left the
// server-vs-client choice open and preferred client-side until fleet size demands
// otherwise). Sort state persists in localStorage so it survives the 30 s auto-refresh
// and a page reload.
//
// What 1.113.0 added, and the decision behind each:
//   * A row menu (Open, Terminal, Files, Network, Wake now, Delete). The name link was the only
//     way into a PC, and every job then cost a second navigation to the right tab. Delete moved
//     into the menu: a red button on every row, one misclick from an irreversible action, was
//     the loudest thing on the page for the rarest thing anyone does here.
//   * Row selection and a bulk bar (Wake selected, Deploy a package, Export). Deploying to N PCs
//     used to be N rounds of type-a-name-click-Add in the Packages dialog.
//   * Selection is held by machine NAME and survives the 30 s refresh and a search change. A
//     selection that silently emptied itself every thirty seconds would be worse than none.
//     Names that leave the roster (deleted, left scope) are dropped from it on refresh, so the
//     bar never acts on a PC the list no longer shows.
//
// Rejected: a server-side bulk endpoint for wake. /api/wake/fleet already takes a `machines`
// list and applies scope itself, so the selection is simply that list.

const inventoryBody = document.getElementById('inventory-body');
const inventoryEmpty = document.getElementById('inventory-empty');
const inventoryNoMatch = document.getElementById('inventory-no-match');
const searchInput = document.getElementById('inventory-search');
const countEl = document.getElementById('inventory-count');
const headRow = document.getElementById('inventory-head');
const devicesRoot = document.getElementById('devices-root');
const selectAllEl = document.getElementById('inventory-select-all');
const bulkBar = document.getElementById('inventory-bulk');
const bulkCount = document.getElementById('inventory-bulk-count');

// Only issue_commands is read here: it decides which ROW MENU entries are built. Deploy has no
// per-row entry; its bulk button is rendered or not by the template, and the script null-guards it.
const CAN_ISSUE = devicesRoot && devicesRoot.dataset.canIssueCommands === '1';

const SORT_STORAGE_KEY = 'fleethub.inventory.sort';
// Shared with packages.js, which reads it when the Packages page opens. sessionStorage rather
// than the url: a list of fifty hostnames does not belong in the address bar, in a bookmark,
// or in the history of whoever uses this browser next. Same-origin frames share it with the
// shell, so the hand-off survives shell navigation.
const DEPLOY_PRESET_KEY = 'fleethub:deploy-preset';
// The columns a row can be searched against -- name, the three identifiers, and the
// manufacturer, so "dell" narrows the list to one vendor's machines. Every field here has
// a visible column: a row that matches on something not on screen looks like a bug.
// os_label, not the nested row.os.label: this list is read flat, and the hub flattens the
// label onto the row for exactly that reason. "windows 10" narrows the fleet to the
// machines still on it, which is the question this column exists to answer.
const SEARCH_FIELDS = ['machine', 'asset_tag', 'serial_number', 'service_tag',
                       'manufacturer', 'os_label'];
// Export columns, in the order a spreadsheet wants them. Field names, not the translated
// headers: an export is read by scripts and by people in other languages alike.
const CSV_FIELDS = ['machine', 'status', 'manufacturer', 'model', 'os_label', 'serial_number',
                    'service_tag', 'asset_tag', 'temp', 'updated_at'];

let allRows = [];          // the last fetch, unfiltered/unsorted
let visibleRows = [];      // what render() last put on screen, in order
let searchQuery = '';
let sort = loadSort();     // { key, dir: 'asc' | 'desc' }
const selected = new Set();

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

function machineHref(machine, tab) {
    const path = '/machine/' + encodeURIComponent(machine);
    return tab ? `${path}?tab=${tab}` : path;
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

// ---- actions on one machine -----------------------------------------------------
async function deleteMachine(machine) {
    if (!window.confirm(t('inventory.confirm_delete', { machine }))) {
        return;
    }
    try {
        const resp = await fetch('/api/machines/' + encodeURIComponent(machine), { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        allRows = allRows.filter((r) => r.machine !== machine);
        selected.delete(machine);
        render();
    } catch (e) {
        toast(t('inventory.delete_failed', { machine, error: e.message }), { kind: 'error' });
    }
}

// The JSON content-type is load-bearing: the hub only reads application/json bodies, which is
// what stops a cross-site form POST from issuing a wake as the signed-in operator.
async function postJson(url, body) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const payload = await resp.json().catch(() => null);
    if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
    return payload;
}

async function wakeMachine(machine) {
    try {
        await postJson('/api/wake/machines/' + encodeURIComponent(machine), { reason: 'devices page' });
        // "Asked for", not "woken": nothing acknowledges a magic packet, and the relay may be
        // waiting for a peer on that subnet to come online. The Network tab has the outcome.
        toast(t('inventory.wake_one_result', { machine }), { kind: 'success' });
        loadInventory();
    } catch (e) {
        toast(t('inventory.wake_one_failed', { machine, error: e.message }), { kind: 'error' });
    }
}

// ---- the row menu -------------------------------------------------------------
// One menu element for the whole table, rebuilt for the row that opened it and positioned
// against the viewport. Not one per row: the table re-renders every thirty seconds, and a
// menu living inside a row would vanish from under the pointer mid-choice.
const rowMenu = document.createElement('div');
rowMenu.id = 'inventory-menu';
rowMenu.setAttribute('role', 'menu');
rowMenu.className = 'fixed z-50 hidden min-w-44 flex-col rounded-lg border border-card-border bg-card p-1 text-sm shadow-lg';
document.body.appendChild(rowMenu);
let menuOwner = null;

function menuItem(label, { href = null, onSelect = null, danger = false } = {}) {
    const item = document.createElement(href ? 'a' : 'button');
    item.setAttribute('role', 'menuitem');
    item.tabIndex = -1;
    item.className = 'block w-full rounded-md border-0 bg-transparent px-3 py-1.5 text-left no-underline hover:bg-control focus:bg-control '
        + (danger ? 'text-danger' : 'text-text');
    item.textContent = label;
    if (href) {
        item.href = href;
        item.addEventListener('click', () => closeMenu(false));
    } else {
        item.type = 'button';
        item.addEventListener('click', () => { closeMenu(false); onSelect(); });
    }
    return item;
}

function menuSeparator() {
    const sep = document.createElement('div');
    sep.setAttribute('role', 'separator');
    sep.className = 'my-1 h-px bg-card-border';
    return sep;
}

function openMenu(row, button) {
    const items = [
        menuItem(t('inventory.action_open'), { href: machineHref(row.machine) }),
        menuItem(t('tools.tab.terminal'), { href: machineHref(row.machine, 'terminal') }),
    ];
    // Files is the one tab gated on the operator rather than on the device; offering it to
    // somebody without issue_commands would land them on Overview with no explanation.
    if (CAN_ISSUE) items.push(menuItem(t('tools.tab.files'), { href: machineHref(row.machine, 'files') }));
    items.push(menuItem(t('tools.tab.network'), { href: machineHref(row.machine, 'network') }));
    // Only for a PC that is actually off: waking an online one records a request that does
    // nothing, and a menu entry that does nothing reads as broken.
    if (CAN_ISSUE && row.status !== 'online') {
        items.push(menuSeparator(), menuItem(t('inventory.action_wake'), { onSelect: () => wakeMachine(row.machine) }));
    }
    items.push(menuSeparator(),
               menuItem(t('common.delete'), { danger: true, onSelect: () => deleteMachine(row.machine) }));
    rowMenu.replaceChildren(...items);

    rowMenu.classList.remove('hidden');
    rowMenu.classList.add('flex');
    const box = button.getBoundingClientRect();
    const width = rowMenu.offsetWidth;
    const height = rowMenu.offsetHeight;
    // Right-aligned under the button; flipped above it when it would run off the bottom.
    const left = Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8));
    const top = (box.bottom + 4 + height > window.innerHeight) ? Math.max(8, box.top - height - 4) : box.bottom + 4;
    rowMenu.style.left = `${left}px`;
    rowMenu.style.top = `${top}px`;

    menuOwner = button;
    button.setAttribute('aria-expanded', 'true');
    const first = rowMenu.querySelector('[role="menuitem"]');
    if (first) first.focus();
}

function closeMenu(restoreFocus) {
    if (!menuOwner) return;
    rowMenu.classList.add('hidden');
    rowMenu.classList.remove('flex');
    menuOwner.setAttribute('aria-expanded', 'false');
    if (restoreFocus && document.contains(menuOwner)) menuOwner.focus();
    menuOwner = null;
}

rowMenu.addEventListener('keydown', (e) => {
    const items = Array.from(rowMenu.querySelectorAll('[role="menuitem"]'));
    const at = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') {
        e.preventDefault();
        items[(at + 1) % items.length].focus();
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        items[(at - 1 + items.length) % items.length].focus();
    } else if (e.key === 'Escape') {
        e.preventDefault();
        closeMenu(true);
    } else if (e.key === 'Tab') {
        closeMenu(false);
    }
});
document.addEventListener('click', (e) => {
    if (menuOwner && !rowMenu.contains(e.target) && !menuOwner.contains(e.target)) closeMenu(false);
}, true);
window.addEventListener('scroll', () => closeMenu(false), true);
window.addEventListener('resize', () => closeMenu(false));

// ---- selection ----------------------------------------------------------------
function updateBulk() {
    const count = selected.size;
    bulkBar.classList.toggle('hidden', count === 0);
    bulkBar.classList.toggle('flex', count > 0);
    if (count) bulkCount.textContent = tPlural('inventory.bulk.count', count);

    // The header box speaks for the VISIBLE rows only, so it reads as mixed when some of what
    // is on screen is ticked, even if the selection also holds rows the search has hidden.
    const onScreen = visibleRows.filter((r) => selected.has(r.machine)).length;
    selectAllEl.checked = visibleRows.length > 0 && onScreen === visibleRows.length;
    selectAllEl.indeterminate = onScreen > 0 && onScreen < visibleRows.length;
}

selectAllEl.addEventListener('change', () => {
    for (const row of visibleRows) {
        if (selectAllEl.checked) selected.add(row.machine);
        else selected.delete(row.machine);
    }
    render();
});

// ---- rendering ----------------------------------------------------------------
function renderRow(row) {
    const tr = document.createElement('tr');
    if (selected.has(row.machine)) tr.className = 'bg-accent-soft';

    const selectTd = document.createElement('td');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'checkbox';
    box.checked = selected.has(row.machine);
    box.setAttribute('aria-label', t('inventory.select_row', { machine: row.machine }));
    box.addEventListener('change', () => {
        if (box.checked) selected.add(row.machine);
        else selected.delete(row.machine);
        tr.classList.toggle('bg-accent-soft', box.checked);
        updateBulk();
    });
    selectTd.appendChild(box);

    const nameTd = document.createElement('td');
    const link = document.createElement('a');
    link.href = machineHref(row.machine);
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
    const menuBtn = document.createElement('button');
    menuBtn.type = 'button';
    menuBtn.className = 'btn btn--ghost btn--icon';
    menuBtn.setAttribute('aria-haspopup', 'menu');
    menuBtn.setAttribute('aria-expanded', 'false');
    menuBtn.setAttribute('aria-label', t('inventory.actions_label', { machine: row.machine }));
    menuBtn.title = t('inventory.actions_label', { machine: row.machine });
    menuBtn.textContent = '⋯';
    menuBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (menuOwner === menuBtn) closeMenu(true);
        else { closeMenu(false); openMenu(row, menuBtn); }
    });
    actionTd.appendChild(menuBtn);

    tr.append(selectTd, nameTd, statusTd, makeTd, modelTd, osTd, serialTd, serviceTd, assetTd,
              tempTd, seenTd, actionTd);
    return tr;
}

function render() {
    closeMenu(false);
    updateSortIndicators();

    const total = allRows.length;
    inventoryEmpty.style.display = total ? 'none' : 'block';

    visibleRows = allRows.filter(matchesSearch).sort(compareRows);
    inventoryNoMatch.style.display = (total && !visibleRows.length) ? 'block' : 'none';

    // Pluralised through tPlural rather than a trailing `s`: the count and its noun agree
    // differently per language, and a hand-built "machine(s)" cannot be translated at all.
    if (searchQuery && total) {
        countEl.textContent = tPlural('inventory.count_filtered', total, { visible: visibleRows.length });
    } else if (total) {
        countEl.textContent = tPlural('inventory.count', total);
    } else {
        countEl.textContent = '';
    }

    inventoryBody.replaceChildren(...visibleRows.map(renderRow));
    updateBulk();
}

async function loadInventory() {
    try {
        const resp = await fetch('/api/machines');
        if (!resp.ok) return;
        allRows = await resp.json();
        // A ticked PC that has since been deleted or left this operator's scope must not stay
        // in a selection the bulk bar is about to act on.
        const present = new Set(allRows.map((r) => r.machine));
        for (const machine of Array.from(selected)) {
            if (!present.has(machine)) selected.delete(machine);
        }
        render();
    } catch (e) {
        // Built as DOM rather than an innerHTML string: the message is now catalog text,
        // and a translation is not something to interpolate into markup.
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 12;
        td.className = 'stat-card__meta';
        td.textContent = t('inventory.load_failed');
        tr.appendChild(td);
        inventoryBody.replaceChildren(tr);
    }
}

// ---- export -------------------------------------------------------------------
// One cell. Quoted always, quotes doubled. And a leading = + - @ (or a tab/CR a spreadsheet
// strips first) is prefixed with an apostrophe: a machine name, a serial and an OS label are
// whatever a host reported to the UNAUTHENTICATED /api/report, and "=HYPERLINK(...)" in the
// asset-tag column is a formula the helpdesk's spreadsheet would run on open.
function csvCell(value) {
    let text = (value === null || value === undefined) ? '' : String(value);
    if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
}

function exportCsv(rows) {
    const lines = [CSV_FIELDS.map(csvCell).join(',')];
    for (const row of rows) lines.push(CSV_FIELDS.map((f) => csvCell(row[f])).join(','));
    // A BOM, so Excel opens UTF-8 as UTF-8 rather than mangling every umlaut in a hostname.
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `fleethub-devices-${new Date().toISOString().slice(0, 10)}.csv`;
    // download attribute set: shell.js leaves links with it alone, so this stays a download.
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- wiring -------------------------------------------------------------------
headRow.querySelectorAll('th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => onHeaderClick(th.dataset.sort));
});

searchInput.addEventListener('input', () => {
    searchQuery = searchInput.value.trim().toLowerCase();
    render();
});

document.getElementById('inventory-export').addEventListener('click', () => exportCsv(visibleRows));
document.getElementById('inventory-bulk-export').addEventListener('click', () => {
    exportCsv(allRows.filter((r) => selected.has(r.machine)).sort(compareRows));
});
document.getElementById('inventory-bulk-clear').addEventListener('click', () => {
    selected.clear();
    render();
});

// Wake the selection. The same fleet endpoint the toolbar button uses, narrowed by its
// `machines` list -- the hub applies scope and decides who relays for whom, and answers with
// counts by outcome rather than a success, for the reason given at the fleet button below.
const bulkWakeBtn = document.getElementById('inventory-bulk-wake');
if (bulkWakeBtn) {
    bulkWakeBtn.addEventListener('click', async () => {
        bulkWakeBtn.disabled = true;
        try {
            const payload = await postJson('/api/wake/fleet', {
                machines: Array.from(selected),
                reason: 'devices page, selected',
            });
            const counts = (payload && payload.counts) || {};
            const asked = (counts.pending || 0) + (counts.relaying || 0) + (counts.sent || 0);
            toast(asked ? t('inventory.wake_all_result', { count: asked })
                        : t('inventory.wake_selected_none'), { kind: asked ? 'success' : 'info' });
            loadInventory();
        } catch (e) {
            toast(`${t('inventory.wake_all_failed')} ${e.message}`, { kind: 'error' });
        } finally {
            bulkWakeBtn.disabled = false;
        }
    });
}

// Deploy to the selection: hand the names to the Packages page, where the operator still has
// to pick a package and confirm in the deploy dialog. Rejected: a package picker here -- that
// would be a second deploy dialog, and the one on Packages already knows windows, retries and
// the refusal list.
const bulkDeployBtn = document.getElementById('inventory-bulk-deploy');
const deployLink = document.getElementById('inventory-deploy-link');
if (bulkDeployBtn && deployLink) {
    bulkDeployBtn.addEventListener('click', () => {
        try {
            sessionStorage.setItem(DEPLOY_PRESET_KEY, JSON.stringify(Array.from(selected)));
        } catch (e) {
            toast(e.message, { kind: 'error' });
            return;
        }
        deployLink.click();
    });
}

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
            const payload = await postJson('/api/wake/fleet', { reason: 'inventory page' });
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
// Keep status fresh without a manual reload. Search box, sort and selection are preserved
// because render() reads them from module state, not the DOM rows.
setInterval(loadInventory, 30000);
