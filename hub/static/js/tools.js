// Tools page: the machine column, and the url that names what you are looking at.
//
// The four panels beside it (Terminal, Backup, Firmware, Network) are the same modules that
// ran on the machine page. Nothing here knows what any of them does -- it only decides
// WHICH machine they are about, hands that to MachineContext, and lets ToolPanels do the
// rest. That separation is the point of the rewrite: adding a fifth tool means registering
// a fifth panel, not touching this file.
//
// The rows are built with createElement/textContent, never innerHTML: a machine name is
// whatever a host reported to the unauthenticated /api/report, so it is arbitrary text.
(function () {
    'use strict';

    const root = document.getElementById('tools-root');
    if (!root) return;

    const listEl = document.getElementById('tools-machine-list');
    const searchEl = document.getElementById('tools-machine-search');
    const emptyEl = document.getElementById('tools-machine-empty');
    const tabsEl = document.getElementById('tools-tabs');
    const promptEl = document.getElementById('tools-pick-prompt');

    // Which machine this browser was last working on. A convenience only: it loses to an
    // explicit ?machine= in the url, because a link somebody sent you must win over what
    // you happened to be doing yesterday.
    const STORAGE_KEY = 'tempmonitor:tools:machine';
    // Slow enough not to matter, fast enough that a PC that just came up appears before you
    // go looking for it. Matches the Dashboard's cadence.
    const REFRESH_MS = 30_000;

    /** [{ machine, online, raw }], online first then alphabetical. */
    let pickable = [];
    let selected = null;
    let notice = '';

    // ---------------- The url ----------------
    // ?machine= alongside tabs.js's ?tab=, so one link carries both. replaceState via
    // syncUrl(), never pushState -- see common.js for why, and note that a frame-side
    // replaceState alone would leave the shell's address bar (and therefore any bookmark or
    // copied link) still naming the machine you left.
    function writeUrl() {
        const url = new URL(location.href);
        if (selected) url.searchParams.set('machine', selected);
        else url.searchParams.delete('machine');
        if (url.search === location.search) return;
        syncUrl(url.pathname + url.search + url.hash);
    }

    function wantedFromUrl() {
        return new URLSearchParams(location.search).get('machine');
    }

    function stored() {
        try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
    }

    function remember(machine) {
        try {
            if (machine) localStorage.setItem(STORAGE_KEY, machine);
            else localStorage.removeItem(STORAGE_KEY);
        } catch (e) { /* private mode */ }
    }

    // ---------------- Selection ----------------
    function select(machine, { persist = true } = {}) {
        if (machine === selected) return;
        selected = machine || null;
        if (persist) remember(selected);
        writeUrl();
        markActiveRow();
        applyEmptyState();
        // Everything else follows from this: each panel is listening for machine:changed and
        // decides for itself whether to reload now or when it is next shown.
        MachineContext.set(selected);
    }

    /**
     * Pick one, in priority order: an explicit ?machine=, then whatever this browser was
     * last on, then the first machine that is actually up, then the first at all.
     *
     * A ?machine= naming something outside the caller's scope (or simply gone) is DROPPED
     * rather than passed on. Handing it to MachineContext would fire four requests that all
     * 403, and four panels' worth of error text is a worse answer than one sentence.
     */
    function chooseInitial() {
        const wanted = wantedFromUrl();
        if (wanted) {
            if (pickable.some((p) => p.machine === wanted)) return wanted;
            notice = t('tools.machine_unavailable', { machine: wanted });
        }
        const last = stored();
        if (last && pickable.some((p) => p.machine === last)) return last;
        const online = pickable.find((p) => p.online);
        if (online) return online.machine;
        return pickable.length ? pickable[0].machine : null;
    }

    // ---------------- The list ----------------
    function orderOf(entry) {
        // Online first, then alphabetical. An offline PC can still be picked -- Firmware and
        // Network both have something to say about one -- so it is sorted down, not hidden.
        return `${entry.online ? '0' : '1'}${entry.machine.toLowerCase()}`;
    }

    function buildRow(entry) {
        // A <button>, not a styled <div>: Enter/Space, focus and the click itself all come
        // from the element rather than from handlers reimplementing them.
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'picker-list__item';
        item.setAttribute('role', 'listitem');
        item.dataset.machine = entry.machine;

        const name = document.createElement('span');
        name.className = 'picker-list__name';
        name.textContent = entry.machine;

        const pill = document.createElement('span');
        pill.className = 'status-pill';
        setMachineStatusPill(pill, entry.raw);

        item.append(name, pill);
        item.addEventListener('click', () => select(entry.machine));
        return item;
    }

    function renderList() {
        const q = searchEl.value.trim().toLowerCase();
        const rows = q ? pickable.filter((p) => p.machine.toLowerCase().includes(q)) : pickable;
        listEl.replaceChildren(...rows.map(buildRow));
        markActiveRow();

        if (rows.length) {
            emptyEl.hidden = true;
        } else {
            emptyEl.hidden = false;
            emptyEl.textContent = pickable.length ? t('tools.no_match') : t('tools.no_machines');
        }
    }

    // Read the rows back out of the DOM rather than holding a node on each entry. A
    // refresh replaces every entry object even when it decides not to re-render, so a
    // cached node would go stale on the first quiet tick -- and the symptom is the worst
    // kind: the highlight stays on the machine you LEFT while everything else follows the
    // one you picked.
    function markActiveRow() {
        for (const row of listEl.querySelectorAll('.picker-list__item')) {
            const active = row.dataset.machine === selected;
            row.classList.toggle('picker-list__item--active', active);
            // aria-current rather than aria-selected: these rows are a list of links to a
            // place, not a listbox of values.
            if (active) row.setAttribute('aria-current', 'true');
            else row.removeAttribute('aria-current');
        }
    }

    /**
     * Hide the halves that have nothing to act on.
     *
     * Only the per-machine halves: with no machine chosen each of them would render an
     * error or an empty frame, four times over, and one sentence is the better answer. The
     * FLEET halves of Firmware and Backup stay -- an image library and a destination list
     * are not about one PC, and making them wait behind picking an irrelevant one would be
     * a worse page than the standalone ones they replaced.
     *
     * On a hub where the operator's scope is empty there is no machine to pick at all, so
     * the tab strip itself is noise.
     */
    function applyEmptyState() {
        const has = !!selected;
        if (tabsEl) tabsEl.hidden = !pickable.length;
        root.classList.toggle('tools--no-machine', !has);
        if (!promptEl) return;
        promptEl.hidden = has && !notice;
        promptEl.textContent = notice || t('tools.pick_a_machine');
        promptEl.classList.toggle('setting__error', !!notice);
    }

    // ---------------- Loading the roster ----------------
    async function refresh({ initial = false } = {}) {
        let rows;
        try {
            rows = await FleetApi.getJson('/api/machines');
        } catch (e) {
            if (initial) {
                emptyEl.hidden = false;
                emptyEl.textContent = e.message;
            }
            return;             // a later tick will get it; the panels are unaffected
        }

        const next = rows.map((row) => ({
            machine: row.machine,
            online: row.status === 'online',
            raw: row,
        }));
        next.sort((a, b) => orderOf(a).localeCompare(orderOf(b)));

        // Re-render only when the membership or the order actually moved. A refresh that
        // rebuilt the list every thirty seconds would drop the row under the operator's
        // pointer and reset the search box's scroll position for no reason.
        const before = pickable.map((p) => `${p.machine}:${p.online}`).join('|');
        const after = next.map((p) => `${p.machine}:${p.online}`).join('|');
        pickable = next;
        if (initial || before !== after) renderList();

        if (initial) {
            select(chooseInitial(), { persist: false });
            applyEmptyState();
        } else if (selected && !pickable.some((p) => p.machine === selected)) {
            // The selected machine left the caller's scope (or was deleted) while the page
            // was open. Say so rather than leaving four panels quietly 403ing.
            notice = t('tools.machine_unavailable', { machine: selected });
            select(null);
        }
    }

    // ---------------- Init ----------------
    searchEl.addEventListener('input', renderList);

    // The shell navigates the frame with the History API, so arriving at a different
    // ?machine= is a same-document change that no load event will announce.
    window.addEventListener('popstate', () => {
        const wanted = wantedFromUrl();
        if (wanted && wanted !== selected && pickable.some((p) => p.machine === wanted)) {
            select(wanted);
        }
    });

    refresh({ initial: true });
    setInterval(refresh, REFRESH_MS);
})();
