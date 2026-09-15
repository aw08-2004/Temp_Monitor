// Machine page: the switcher beside the name, and keeping your tab when you use it.
//
// This is what is left of the Tools page's machine column (hub 1.114.0). The tools moved back
// onto /machine/<name> as tabs, and the one thing the column was genuinely good at -- doing
// the same job on the next PC without hunting for it -- lives here: every row links to that
// PC's page WITH the tab you are on, so Terminal on PC-1 -> Terminal on PC-2 is two clicks.
//
// Rows are plain <a href>, not buttons that navigate from script. shell.js already routes
// link clicks through the frame and keeps the address bar honest; a location assignment from
// here would be a second navigation path to keep in step with it. The cost is that the hrefs
// have to follow the tab, which is what refreshHrefs() on tab:shown is for.
//
// The roster is fetched on first OPEN, not at page load: most visits to a machine page never
// switch, and /api/machines is the whole scoped fleet. It is the same scoped list the Tools
// column used (access.filter_rows), so the switcher cannot enumerate a machine the operator
// could not already see.
//
// Rows are built with createElement/textContent, never innerHTML: a machine name is whatever
// a host reported to the unauthenticated /api/report, so it is arbitrary text.
(function () {
    'use strict';

    const root = document.getElementById('machine-switcher');
    if (!root) return;

    const listEl = document.getElementById('machine-switcher-list');
    const searchEl = document.getElementById('machine-switcher-search');
    const emptyEl = document.getElementById('machine-switcher-empty');
    const summary = root.querySelector('summary');
    const current = window.MachineContext ? window.MachineContext.current() : null;

    /** [{ machine, online, raw }], online first then alphabetical. null until first open. */
    let roster = null;

    function hrefFor(machine) {
        // Only ?tab= travels. Anything else in the query (?machine= from an old Tools link
        // that was redirected here) is about the page being left.
        const tab = new URLSearchParams(location.search).get('tab');
        const path = `/machine/${encodeURIComponent(machine)}`;
        return tab ? `${path}?tab=${encodeURIComponent(tab)}` : path;
    }

    function buildRow(entry) {
        const item = document.createElement('a');
        item.className = 'picker-list__item';
        item.setAttribute('role', 'listitem');
        item.dataset.machine = entry.machine;
        item.href = hrefFor(entry.machine);
        if (entry.machine === current) {
            item.classList.add('picker-list__item--active');
            // aria-current: this row is the place you already are.
            item.setAttribute('aria-current', 'page');
        }

        const name = document.createElement('span');
        name.className = 'picker-list__name';
        name.textContent = entry.machine;

        const pill = document.createElement('span');
        pill.className = 'status-pill';
        setMachineStatusPill(pill, entry.raw);

        item.append(name, pill);
        return item;
    }

    function render() {
        if (!roster) return;
        const q = searchEl.value.trim().toLowerCase();
        const rows = q ? roster.filter((p) => p.machine.toLowerCase().includes(q)) : roster;
        listEl.replaceChildren(...rows.map(buildRow));
        emptyEl.hidden = rows.length > 0;
        if (!rows.length) {
            emptyEl.textContent = roster.length ? t('machine.switcher.none') : t('tools.no_machines');
        }
    }

    function refreshHrefs() {
        for (const row of listEl.querySelectorAll('a.picker-list__item')) {
            row.href = hrefFor(row.dataset.machine);
        }
    }

    async function load() {
        try {
            const rows = await FleetApi.getJson('/api/machines');
            roster = rows.map((row) => ({
                machine: row.machine,
                online: row.status === 'online',
                raw: row,
            }));
            // Online first, then alphabetical -- same order the Tools column and the Remote
            // picker use, so a PC is where an operator's hands expect it.
            const key = (e) => `${e.online ? '0' : '1'}${e.machine.toLowerCase()}`;
            roster.sort((a, b) => key(a).localeCompare(key(b)));
            render();
        } catch (e) {
            emptyEl.hidden = false;
            emptyEl.textContent = e.message;
        }
    }

    function close({ focus = false } = {}) {
        root.open = false;
        if (focus) summary.focus();
    }

    root.addEventListener('toggle', () => {
        if (!root.open) return;
        if (!roster) load();
        searchEl.focus();
        searchEl.select();
    });

    searchEl.addEventListener('input', render);
    searchEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            e.preventDefault();
            close({ focus: true });
        } else if (e.key === 'Enter') {
            // Enter takes the first match: type "042", Enter, and you are there.
            const first = listEl.querySelector('a.picker-list__item');
            if (first) {
                e.preventDefault();
                first.click();
            }
        } else if (e.key === 'ArrowDown') {
            const first = listEl.querySelector('a.picker-list__item');
            if (first) {
                e.preventDefault();
                first.focus();
            }
        }
    });
    listEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') close({ focus: true });
    });

    // A popover that stays open after you click elsewhere is one you have to dismiss by
    // hand before doing anything else on the page.
    document.addEventListener('click', (e) => {
        if (root.open && !root.contains(e.target)) close();
    });

    // tabs.js dispatches tab:shown (bubbling) whenever a tab becomes the visible one, and it
    // has already written ?tab= by then.
    document.addEventListener('tab:shown', refreshHrefs);

    // ---------------- Charts in a tab that was hidden at load ----------------
    // Chart.js measures a canvas when it is built, and machine.js builds the history charts
    // at load. On a page opened as ?tab=terminal the Overview panel is hidden at that moment,
    // so they come back zero pixels tall -- the same failure a folded <details> causes, and
    // machine.js already answers that one on the fold's `toggle`. Re-sending that event is
    // the smallest fix that keeps the answer in one place.
    const overview = document.getElementById('machine-overview');
    const history = document.getElementById('history-card');
    if (overview && history) {
        let shownOnce = !overview.hidden;
        overview.addEventListener('tab:shown', () => {
            if (shownOnce) return;
            shownOnce = true;
            if (history.open) history.dispatchEvent(new Event('toggle'));
        });
    }

    // ---------------- A linked tab this device cannot answer ----------------
    // machine-tools-gate.js hides tabs a device has said it cannot serve, but a link can still
    // arrive naming one (?tab=terminal on a phone). Fall back to Overview rather than leave a
    // hidden tab selected over a panel that will only ever show a refusal.
    if (window.MachineCapabilities) {
        window.MachineCapabilities.ready().then(() => {
            const active = document.querySelector('#machine-tabs .tabs__tab--active');
            const fallback = document.getElementById('tab-btn-overview');
            if (active && active.hidden && fallback) fallback.click();
        });
    }
})();
