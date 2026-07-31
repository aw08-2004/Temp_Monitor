// Machine page, Firmware tab: what this PC's BIOS is actually set to (roadmap #9).
//
// Read-only in this release. The write half (`set_bios_settings`) lands next, and this
// renderer is deliberately shaped for it: every attribute already carries its kind, its
// accepted values and whether it is read-only, so the editor becomes a control per row
// rather than a second view of the same data.
//
// **Four states, not two.** `null` (no agent has ever reported), `unsupported` (this machine
// has told us it has no manageable BIOS -- a VM, a whitebox), `error` (an interface exists
// and reading it failed) and a real attribute list. The middle two are the ones that get
// collapsed by accident, and collapsing them is what makes every VM in a fleet show a red
// error forever. They render differently on purpose: unsupported is neutral and final,
// error is a warning with the machine's own message attached.
//
// Same two rules as the rest of the console: built with textContent/createElement, never
// innerHTML -- attribute names and vendor error strings are arbitrary text arriving from a
// remote machine -- and every string comes from the catalog.

(function () {
    'use strict';

    const pane = document.getElementById('tab-firmware');
    if (!pane) return;

    const machineConfig = document.getElementById('machine-config');
    const MACHINE = machineConfig.dataset.machine;

    const statusPill = document.getElementById('firmware-status');
    const statusText = document.getElementById('firmware-status-text');
    const metaLine = document.getElementById('firmware-meta');
    const body = document.getElementById('firmware-body');
    const search = document.getElementById('firmware-search');
    const refreshBtn = document.getElementById('firmware-refresh');

    let data = null;
    let loaded = false;
    let filter = '';
    let notice = '';

    async function api(path, options) {
        const resp = await fetch(path, options);
        let payload = null;
        try { payload = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
        return payload;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function fmtTime(epoch) {
        return epoch ? new Date(epoch * 1000).toLocaleString() : '—';
    }

    // A wire value shown to an operator needs a display name, and the map is spelled out --
    // one literal t() per value, so the key scan in tests/test_i18n.py can see them.
    const KIND_LABELS = {
        enum: () => t('machine.firmware.kind.enum'),
        string: () => t('machine.firmware.kind.string'),
        integer: () => t('machine.firmware.kind.integer'),
        unknown: () => t('machine.firmware.kind.unknown'),
    };

    function kindLabel(kind) {
        const fn = KIND_LABELS[kind];
        // An unrecognised kind shows itself: a newer agent reporting a kind this hub has no
        // word for is better rendered as its own name than as a missing catalog key.
        return fn ? fn() : kind;
    }

    // Loaded lazily on first reveal, like the Backup tab: most machine pages are opened to
    // look at a temperature graph, not a BIOS attribute list.
    pane.addEventListener('tab:shown', () => { if (!loaded) load(); });

    async function load() {
        loaded = true;
        setStatus('muted', t('common.loading'));
        try {
            data = await api(`/api/bios/${encodeURIComponent(MACHINE)}`);
        } catch (e) {
            data = null;
            setStatus('danger', t('machine.firmware.state.error'));
            body.replaceChildren(el('p', 'setting__error', e.message));
            return;
        }
        render();
    }

    function setStatus(tone, text) {
        statusPill.className = `status-pill status-pill--${tone}`;
        statusText.textContent = text;
        // The dot is re-created rather than kept: replaceChildren is how every other
        // renderer here clears a node, and the label element is re-attached after it so the
        // pill keeps its markup order.
        statusPill.replaceChildren(el('span', 'status-pill__dot'), statusText);
    }

    function render() {
        if (!data) return;
        body.replaceChildren();
        metaLine.replaceChildren();

        if (notice) body.appendChild(el('p', 'stat-card__meta', notice));

        if (data.support === null || data.support === undefined) {
            // Never reported. Deliberately not "unsupported": before the agent release that
            // collects this, EVERY machine is in this state, and telling an operator their
            // fleet has no manageable firmware would be false on every row.
            setStatus('muted', t('machine.firmware.state.unknown'));
            body.appendChild(el('p', 'stat-card__meta', t('machine.firmware.unknown_help')));
            return;
        }

        metaLine.appendChild(el('span', null, t('machine.firmware.reported_at',
                                                { when: fmtTime(data.reported_at) })));
        if (data.vendor) {
            metaLine.appendChild(document.createTextNode(' · '));
            // The vendor and the interface it answered on are the machine's own words --
            // product/namespace names, not prose, so they are shown verbatim.
            metaLine.appendChild(el('span', null,
                data.interface ? `${data.vendor} (${data.interface})` : data.vendor));
        }
        if (data.bios_version) {
            metaLine.appendChild(document.createTextNode(' · '));
            metaLine.appendChild(el('span', null,
                t('machine.firmware.bios_version', { version: data.bios_version })));
        }

        if (data.support === 'unsupported') {
            setStatus('muted', t('machine.firmware.state.unsupported'));
            body.appendChild(el('p', 'stat-card__meta', t('machine.firmware.unsupported_help')));
            return;
        }
        if (data.support === 'error') {
            setStatus('danger', t('machine.firmware.state.error'));
            body.appendChild(el('p', 'setting__error',
                               data.error || t('machine.firmware.error_unknown')));
            return;
        }

        const all = data.settings || [];
        setStatus('ok', tPlural('machine.firmware.state.count', all.length));

        if (data.password_set) {
            // Worth saying before anyone tries to change anything: on all three vendors a
            // setup password blocks writes, and finding that out one machine at a time is
            // the expensive way.
            body.appendChild(el('p', 'stat-card__meta', t('machine.firmware.password_set')));
        }

        const needle = filter.trim().toLowerCase();
        const rows = needle
            ? all.filter((a) => a.name.toLowerCase().includes(needle)
                             || (a.display_name || '').toLowerCase().includes(needle)
                             || String(a.value).toLowerCase().includes(needle))
            : all;

        if (!rows.length) {
            body.appendChild(el('p', 'stat-card__meta', t('machine.firmware.no_matches')));
            return;
        }

        const table = el('table', 'data-table');
        const thead = el('thead');
        const headRow = el('tr');
        // Literal keys, not a loop over a suffix: a key assembled by concatenation is
        // invisible to the key scan in tests/test_i18n.py, so a column added without a
        // catalog entry would caption itself with its own key and raise nowhere.
        headRow.appendChild(el('th', null, t('machine.firmware.col.setting')));
        headRow.appendChild(el('th', null, t('machine.firmware.col.value')));
        headRow.appendChild(el('th', null, t('machine.firmware.col.kind')));
        thead.appendChild(headRow);
        table.appendChild(thead);

        const tbody = el('tbody');
        for (const attr of rows) {
            const tr = el('tr');

            const nameCell = el('td');
            // The machine's own attribute name is the identity a future write targets, so it
            // is always shown -- a friendlier display name goes BESIDE it, never instead of
            // it. v1 maps no vendor vocabulary onto a common one on purpose (see bios.py).
            nameCell.appendChild(el('div', null, attr.name));
            if (attr.display_name && attr.display_name !== attr.name) {
                nameCell.appendChild(el('div', 'stat-card__meta', attr.display_name));
            }
            tr.appendChild(nameCell);

            const valueCell = el('td');
            valueCell.appendChild(el('div', null, attr.value || '—'));
            if (attr.possible_values && attr.possible_values.length > 1) {
                valueCell.appendChild(el('div', 'stat-card__meta',
                                         attr.possible_values.join(' / ')));
            }
            tr.appendChild(valueCell);

            const kindCell = el('td');
            kindCell.appendChild(el('span', null, kindLabel(attr.kind)));
            if (attr.read_only) {
                kindCell.appendChild(document.createTextNode(' · '));
                kindCell.appendChild(el('span', 'stat-card__meta',
                                        t('machine.firmware.read_only')));
            }
            tr.appendChild(kindCell);

            tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        body.appendChild(table);
    }

    if (search) {
        search.addEventListener('input', () => {
            filter = search.value;
            if (data) render();
        });
    }

    if (refreshBtn) {
        refreshBtn.addEventListener('click', async () => {
            refreshBtn.disabled = true;
            notice = '';
            try {
                await api(`/api/bios/${encodeURIComponent(MACHINE)}/refresh`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}',
                });
                // Queued, not done: the agent picks the command up on its next poll and the
                // answer arrives on the heartbeat after that. Saying "refreshed" here would
                // be a lie on any machine that is asleep -- the same honesty "Back up now"
                // needs, for the same reason.
                notice = t('machine.firmware.refresh_queued');
            } catch (e) {
                notice = e.message;
            }
            refreshBtn.disabled = false;
            if (data) render(); else load();
        });
    }
})();
