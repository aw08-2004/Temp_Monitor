// Machine page, Firmware tab: what this PC's BIOS is set to, and changing it (roadmap #9).
//
// **Nothing here ever says "reboot to apply".** Whether a firmware write takes effect
// immediately is per vendor AND per setting, so the hub decides after the agent re-reads the
// attribute: `applied` says nothing about restarting, and only `pending_reboot` asks. A
// front-end that guessed would be wrong most of the time on Dell alone.
//
// **Edits are staged, not sent per row.** One apply sends one change, which is what makes the
// hub's "one change in flight per machine" rule survivable -- a per-row control would put an
// operator changing four settings into a 409 on the second one.
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

    const PANEL_ID = 'tool-firmware';
    const pane = document.getElementById(PANEL_ID);
    if (!pane) return;

    // The machine this panel is currently showing. A call, not a constant: on the Tools
    // page the operator picks a different PC without the page reloading, and every URL
    // below has to be built against the machine chosen now rather than the one that
    // happened to be current when this file was parsed.
    const currentMachine = () => window.MachineContext.current();

    const statusPill = document.getElementById('firmware-status');
    const statusText = document.getElementById('firmware-status-text');
    const metaLine = document.getElementById('firmware-meta');
    const body = document.getElementById('firmware-body');
    const search = document.getElementById('firmware-search');
    const refreshBtn = document.getElementById('firmware-refresh');
    const changeBox = document.getElementById('firmware-change');
    const applyBar = document.getElementById('firmware-apply');
    const applyCount = document.getElementById('firmware-apply-count');
    const applyBtn = document.getElementById('firmware-apply-btn');
    const resetBtn = document.getElementById('firmware-reset-btn');

    let data = null;
    let filter = '';
    let notice = '';
    // name -> chosen value, for rows the operator has changed. Keyed on the machine's own
    // attribute name because that is the identity the write targets; a row index would break
    // the moment the search filter narrows the table under it.
    let edits = new Map();
    // Set while a change is in flight, so the poll below can stop on its own. A change resolves
    // in seconds once the agent picks the command up, but the agent may be asleep -- so this
    // polls rather than assuming, and gives up rather than polling a laptop all weekend.
    let pollTimer = null;
    let pollsLeft = 0;
    const POLL_INTERVAL_MS = 5000;
    const MAX_POLLS = 60;

    async function api(path, options) {
        const resp = await fetch(path, options);
        let payload = null;
        try { payload = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
        return payload;
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

    // Same rule, for a change's state and for one attribute's outcome. Literal keys again.
    const CHANGE_LABELS = {
        pending: () => t('machine.firmware.change.pending'),
        running: () => t('machine.firmware.change.running'),
        applied: () => t('machine.firmware.change.applied'),
        pending_reboot: () => t('machine.firmware.change.pending_reboot'),
        partial: () => t('machine.firmware.change.partial'),
        failed: () => t('machine.firmware.change.failed'),
    };
    const OUTCOME_LABELS = {
        applied: () => t('machine.firmware.outcome.applied'),
        pending_reboot: () => t('machine.firmware.outcome.pending_reboot'),
        failed: () => t('machine.firmware.outcome.failed'),
        unknown: () => t('machine.firmware.outcome.unknown'),
    };
    const CHANGE_TONES = {
        pending: 'muted', running: 'muted', applied: 'ok',
        pending_reboot: 'warn', partial: 'warn', failed: 'danger',
    };

    function labelFor(map, value) {
        const fn = map[value];
        return fn ? fn() : value;
    }

    function isOpen(change) {
        return change && (change.status === 'pending' || change.status === 'running');
    }

    // The comparison the hub uses when it verifies a change, mirrored here for one purpose
    // only: deciding whether a row has actually been edited. Firmware values differ in case
    // and padding between what you pick and what the machine reported, and treating "Enable"
    // and "enable " as a change would send a write the hub then refuses as a no-op.
    function sameValue(a, b) {
        return String(a === undefined || a === null ? '' : a).trim().toLowerCase()
            === String(b === undefined || b === null ? '' : b).trim().toLowerCase();
    }

    // Loaded lazily on first reveal and re-loaded on a machine switch, like the Backup
    // tab: most machine pages are opened to look at a temperature graph, not a BIOS
    // attribute list. ToolPanels owns that sequencing; see tool-panels.js.
    ToolPanels.register('firmware', {
        panelId: PANEL_ID,
        load,
        teardown: reset,
        requires: (machine) => !!machine
    });

    // A staged edit belongs to the machine it was staged against, so leaving one behind on
    // a machine switch would apply somebody's Dell settings to a Lenovo.
    function reset() {
        stopPolling();
        data = null;
        filter = '';
        notice = '';
        edits = new Map();
        pollsLeft = 0;
    }

    async function load() {
        setStatus('muted', t('common.loading'));
        try {
            data = await api(`/api/bios/${encodeURIComponent(currentMachine())}`);
        } catch (e) {
            data = null;
            setStatus('danger', t('machine.firmware.state.error'));
            body.replaceChildren(el('p', 'setting__error', e.message));
            return;
        }
        // Opening the tab onto a change someone else queued starts the watch too -- the
        // status is the same fact whoever asked for it, and a stale "running" that only
        // updates on a manual reload is how an operator concludes the feature is broken.
        if (isOpen((data.changes || [])[0]) && pollTimer === null) startPolling();
        render();
        // Not awaited: the settings table is what the operator opened the tab for, and the
        // update history is a second request that must not delay it.
        loadUpdates();
    }

    function setStatus(tone, text) {
        statusPill.className = `status-pill status-pill--${tone}`;
        statusText.textContent = text;
        // The dot is re-created rather than kept: replaceChildren is how every other
        // renderer here clears a node, and the label element is re-attached after it so the
        // pill keeps its markup order.
        statusPill.replaceChildren(el('span', 'status-pill__dot'), statusText);
    }

    // ---------------------------------------------------------------- change panel
    function renderChange() {
        changeBox.replaceChildren();
        const change = (data && data.changes && data.changes[0]) || null;
        if (!change) return;

        const card = el('div', `notice notice--${CHANGE_TONES[change.status] || 'muted'}`);
        card.appendChild(el('div', 'section-title',
                            labelFor(CHANGE_LABELS, change.status)));

        if (change.status === 'pending_reboot') {
            // The ONLY place a restart is ever suggested, and it is suggested because the
            // machine read the attribute back and it was still the old value -- not because
            // this vendor is on a list of vendors that need reboots.
            card.appendChild(el('p', 'stat-card__meta',
                                t('machine.firmware.change.reboot_help')));
        }
        if (change.error) card.appendChild(el('p', 'setting__error', change.error));

        const rows = (change.results && change.results.length)
            ? change.results : change.changes;
        const list = el('ul', 'plain-list');
        for (const row of rows) {
            const item = el('li');
            item.appendChild(el('span', null, `${row.name}: `));
            item.appendChild(el('span', null,
                t('machine.firmware.change.transition',
                  { from: row.from || '—', to: row.to || '—' })));
            if (row.outcome) {
                item.appendChild(document.createTextNode(' · '));
                item.appendChild(el('span', null, labelFor(OUTCOME_LABELS, row.outcome)));
            }
            // The value the firmware actually reported afterwards, shown only when it is
            // neither the old nor the new one -- that is the `unknown` case, and the observed
            // string is the only thing that explains it (some firmware normalises what you
            // write, some substitutes something else).
            if (row.outcome === 'unknown' && row.observed) {
                item.appendChild(el('div', 'stat-card__meta',
                    t('machine.firmware.change.observed', { value: row.observed })));
            }
            if (row.error) item.appendChild(el('div', 'setting__error', row.error));
            list.appendChild(item);
        }
        card.appendChild(list);

        const meta = el('p', 'stat-card__meta',
            t('machine.firmware.change.meta',
              { who: change.requested_by || '—', when: fmtTime(change.requested_at) }));
        card.appendChild(meta);

        if (change.status === 'pending' && data.can_manage_firmware) {
            const cancel = el('button', 'btn btn--ghost',
                              t('machine.firmware.change.cancel'));
            cancel.type = 'button';
            cancel.addEventListener('click', async () => {
                cancel.disabled = true;
                try {
                    await api(`/api/bios/${encodeURIComponent(currentMachine())}/changes/`
                              + encodeURIComponent(change.id), { method: 'DELETE' });
                    notice = '';
                } catch (e) {
                    notice = e.message;
                }
                await load();
            });
            card.appendChild(cancel);
        }
        changeBox.appendChild(card);
    }

    // ---------------------------------------------------------------- staged edits
    function refreshApplyBar() {
        if (!edits.size) {
            applyBar.hidden = true;
            return;
        }
        applyBar.hidden = false;
        applyCount.textContent = tPlural('machine.firmware.staged', edits.size);
    }

    function stage(name, value, current) {
        // An edit back to the current value is an un-edit, not a change to send. The hub
        // refuses no-ops (verification has nothing to compare), so catching it here is the
        // difference between a disabled apply and a 400.
        if (sameValue(value, current)) edits.delete(name);
        else edits.set(name, value);
        refreshApplyBar();
    }

    function valueControl(attr, editable) {
        const staged = edits.get(attr.name);
        const shown = staged === undefined ? attr.value : staged;

        if (!editable) {
            const cell = el('div', null, attr.value || '—');
            return cell;
        }

        // An enum with a known option list gets a <select> -- the accepted values come from
        // the machine itself, so this is the one control that cannot offer an invalid choice.
        if (attr.kind === 'enum' && attr.possible_values && attr.possible_values.length) {
            const select = el('select', 'input');
            let matched = false;
            for (const option of attr.possible_values) {
                const node = el('option', null, option);
                node.value = option;
                if (sameValue(option, shown)) { node.selected = true; matched = true; }
                select.appendChild(node);
            }
            if (!matched) {
                // The current value is not in the machine's own option list. Shown as a
                // selected, disabled entry rather than dropped: silently pre-selecting the
                // first option would make the row look like a change nobody made, and
                // applying it would write it.
                const node = el('option', null, shown || '—');
                node.value = shown;
                node.selected = true;
                node.disabled = true;
                select.insertBefore(node, select.firstChild);
            }
            select.addEventListener('change',
                () => stage(attr.name, select.value, attr.value));
            return select;
        }

        const input = el('input', 'input');
        input.type = attr.kind === 'integer' ? 'number' : 'text';
        input.value = shown === undefined || shown === null ? '' : shown;
        input.addEventListener('input', () => stage(attr.name, input.value, attr.value));
        return input;
    }

    // ---------------------------------------------------------------- BIOS updates
    //
    // The flash half of roadmap #9, read-only on this page: an update is aimed from the
    // Firmware page, where the images are. What belongs beside a machine is whether it has
    // been flashed and whether it took -- and `rebooting` is why this exists at all, since
    // that state can last hours and is invisible everywhere else on the machine page.
    const updatesBody = document.getElementById('firmware-updates-body');

    const UPDATE_LABELS = {
        pending: () => t('machine.firmware.update.pending'),
        in_flight: () => t('machine.firmware.update.in_flight'),
        flashing: () => t('machine.firmware.update.flashing'),
        rebooting: () => t('machine.firmware.update.rebooting'),
        applied: () => t('machine.firmware.update.applied'),
        failed: () => t('machine.firmware.update.failed'),
        unknown: () => t('machine.firmware.update.unknown'),
        refused: () => t('machine.firmware.update.refused'),
        expired: () => t('machine.firmware.update.expired'),
        cancelled: () => t('machine.firmware.update.cancelled'),
    };
    const UPDATE_TONES = {
        pending: 'muted', in_flight: 'muted', flashing: 'warn', rebooting: 'warn',
        applied: 'ok', failed: 'danger', unknown: 'warn', refused: 'muted',
        expired: 'muted', cancelled: 'muted',
    };

    async function loadUpdates() {
        if (!updatesBody) return;   // the panel is gated on manage_firmware
        let jobs;
        try {
            const resp = await api(
                `/api/firmware/jobs?machine=${encodeURIComponent(currentMachine())}`);
            jobs = resp.jobs || [];
        } catch (e) {
            updatesBody.replaceChildren(el('p', 'setting__error', e.message));
            return;
        }
        updatesBody.replaceChildren();
        if (!jobs.length) {
            updatesBody.appendChild(el('p', 'stat-card__meta',
                                       t('machine.firmware.no_updates')));
            return;
        }
        const list = el('ul', 'plain-list');
        for (const job of jobs) {
            // One row per job, and the status shown is THIS machine's -- a fleet-wide job
            // that applied on thirty-nine PCs and failed here must read as failed here.
            const detail = await api(`/api/firmware/jobs/${encodeURIComponent(job.id)}`)
                .catch(() => null);
            const mine = detail && (detail.targets || [])
                .find((target) => target.machine === currentMachine());
            const item = el('li');
            const pill = el('span',
                            `status-pill status-pill--${UPDATE_TONES[mine && mine.status] || 'muted'}`);
            pill.appendChild(el('span', 'status-pill__dot'));
            pill.appendChild(el('span', null,
                labelFor(UPDATE_LABELS, mine ? mine.status : job.status)));
            item.appendChild(pill);
            item.appendChild(document.createTextNode(' '));
            item.appendChild(el('span', null, t('machine.firmware.update_line', {
                name: job.payload_name || '—',
                version: job.payload_version || '—',
                when: fmtTime(job.created_at),
            })));
            if (mine && mine.error) {
                item.appendChild(el('div', 'stat-card__meta', mine.error));
            }
            list.appendChild(item);
        }
        updatesBody.appendChild(list);
    }

    function render() {
        if (!data) return;
        body.replaceChildren();
        metaLine.replaceChildren();
        // Hidden by default and re-shown only by the table renderer, so every early return
        // below (never reported / unsupported / error / no matches) leaves no apply button
        // over a page with nothing to apply.
        applyBar.hidden = true;
        renderChange();

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
            // Said explicitly because it looks like a bug otherwise: firmware errors are
            // Windows' own words on the reporting machine, so a Spanish message shows up in an
            // English console whenever that PC runs a Spanish Windows. Translating it is not
            // an option -- we would be inventing text the machine never said.
            if (data.error) {
                body.appendChild(el('p', 'stat-card__meta',
                                    t('machine.firmware.error_from_machine')));
            }
            return;
        }

        const all = data.settings || [];
        setStatus('ok', tPlural('machine.firmware.state.count', all.length));

        // Editable only when the operator holds manage_firmware AND nothing is in flight. A
        // second change would race the first in the firmware, and verification -- which reads
        // one current value per attribute -- could not say which write it was looking at. The
        // hub refuses it too; disabling the controls is so nobody types into a form that is
        // going to 409.
        const inFlight = isOpen((data.changes || [])[0]);
        const canEdit = !!data.can_manage_firmware && !inFlight;
        if (inFlight && data.can_manage_firmware) {
            body.appendChild(el('p', 'stat-card__meta', t('machine.firmware.locked')));
        }

        if (data.password_set) {
            // Worth saying before anyone tries to change anything: on all three vendors a
            // setup password blocks writes, and finding that out one machine at a time is
            // the expensive way. Which of the two lines shows depends on whether the hub has
            // a password stored -- "the firmware wants one and we have none" is a different
            // problem from "the firmware wants one and we will send one".
            body.appendChild(el('p', 'stat-card__meta',
                data.password_stored ? t('machine.firmware.password_ready')
                                     : t('machine.firmware.password_set')));
        } else if (data.password_set === null) {
            // The vendor gave the agent no way to ask. Deliberately not rendered as "no
            // password": the two lead to different advice the moment a write is refused.
            body.appendChild(el('p', 'stat-card__meta',
                                t('machine.firmware.password_unknown')));
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

            const editable = canEdit && !attr.read_only;
            const valueCell = el('td');
            valueCell.appendChild(valueControl(attr, editable));
            if (!editable && attr.possible_values && attr.possible_values.length > 1) {
                // Only worth listing when there is no control offering them. With a <select>
                // the options ARE the list, and repeating them below it is noise.
                valueCell.appendChild(el('div', 'stat-card__meta',
                                         attr.possible_values.join(' / ')));
            }
            if (edits.has(attr.name)) {
                valueCell.appendChild(el('div', 'stat-card__meta',
                    t('machine.firmware.was', { value: attr.value || '—' })));
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
        refreshApplyBar();
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
                await api(`/api/bios/${encodeURIComponent(currentMachine())}/refresh`, {
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

    // ---------------------------------------------------------------- apply
    if (applyBtn) {
        applyBtn.addEventListener('click', async () => {
            if (!edits.size) return;
            const changes = Array.from(edits, ([name, value]) => ({ name, value }));
            applyBtn.disabled = true;
            notice = '';
            try {
                await api(`/api/bios/${encodeURIComponent(currentMachine())}/settings`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ changes }),
                });
                // Cleared only on success. A rejected change (a stale attribute list, a
                // machine that already has one in flight) leaves the staged edits alone --
                // the operator's work survives the error they are about to read.
                edits = new Map();
                notice = t('machine.firmware.apply_queued');
                startPolling();
            } catch (e) {
                notice = e.message;
            }
            applyBtn.disabled = false;
            await load();
        });
    }

    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            edits = new Map();
            render();
        });
    }

    // While a change is in flight, poll for its outcome. Bounded rather than open-ended: the
    // command may be sitting in the queue for a laptop that is shut in a bag, and a tab left
    // open over a weekend must not keep asking. The hub's own stale-change sweep is what
    // eventually closes that row; this just stops watching.
    function startPolling() {
        stopPolling();
        pollsLeft = MAX_POLLS;
        pollTimer = setInterval(async () => {
            if (pollsLeft-- <= 0) { stopPolling(); return; }
            try {
                const fresh = await api(`/api/bios/${encodeURIComponent(currentMachine())}`);
                data = fresh;
                if (!isOpen((fresh.changes || [])[0])) {
                    notice = '';
                    stopPolling();
                }
                render();
            } catch (e) {
                // A failed poll is not worth surfacing -- the next one usually works, and an
                // error banner replacing a change's real status would be the wrong news.
                stopPolling();
            }
        }, POLL_INTERVAL_MS);
    }

    function stopPolling() {
        if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null; }
    }

    // ---------------------------------------------------------------- setup password
    const passwordBtn = document.getElementById('firmware-password');
    const passwordDialog = document.getElementById('firmware-password-dialog');

    if (passwordBtn && passwordDialog) {
        const scope = document.getElementById('firmware-password-scope');
        const field = document.getElementById('firmware-password-value');
        const errorLine = document.getElementById('firmware-password-error');

        function url() {
            return scope.value === 'fleet'
                ? '/api/bios-password'
                : `/api/bios-password/${encodeURIComponent(currentMachine())}`;
        }

        function fail(message) {
            errorLine.textContent = message;
            errorLine.hidden = false;
        }

        passwordBtn.addEventListener('click', () => {
            field.value = '';
            errorLine.hidden = true;
            passwordDialog.showModal();
        });

        document.getElementById('firmware-password-cancel')
            .addEventListener('click', () => passwordDialog.close());

        document.getElementById('firmware-password-save')
            .addEventListener('click', async () => {
                errorLine.hidden = true;
                if (!field.value) { fail(t('machine.firmware.password_required')); return; }
                try {
                    await api(url(), {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password: field.value }),
                    });
                } catch (e) { fail(e.message); return; }
                // Wiped from the DOM as soon as it is stored. There is no endpoint that reads
                // a password back, so nothing repopulates this field and nothing should.
                field.value = '';
                passwordDialog.close();
                notice = t('machine.firmware.password_saved');
                await load();
            });

        document.getElementById('firmware-password-clear')
            .addEventListener('click', async () => {
                errorLine.hidden = true;
                try {
                    await api(url(), { method: 'DELETE' });
                } catch (e) { fail(e.message); return; }
                field.value = '';
                passwordDialog.close();
                notice = t('machine.firmware.password_cleared');
                await load();
            });
    }
})();
