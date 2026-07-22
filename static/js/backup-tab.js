// Machine page, Backup tab: what THIS PC backs up, and what it has backed up.
//
// Deliberately a separate file from backups.js rather than a shared module: the two pages
// render different things (this one is one machine's exceptions and its run history; that
// one is the fleet policy) and the only genuinely shared thing — the token grammar — lives
// on the server and reaches both as resolved data. A shared bundle here would couple two
// pages that have no reason to change together.
//
// Same two rules as the rest of the console:
//  * built with textContent / createElement, never innerHTML — paths and provider errors
//    are arbitrary text from operators and remote machines,
//  * fleet defaults come from the server, never a copy here, so a machine following the
//    fleet shows the real fleet list rather than a stale guess.

(function () {
    const pane = document.getElementById('tab-backup');
    if (!pane) return;      // no manage_backups: the tab was never rendered

    const machineConfig = document.getElementById('machine-config');
    const MACHINE = machineConfig.dataset.machine;

    let data = null;
    let draftInclude = [];
    let draftExclude = [];
    let dirty = false;
    let loaded = false;

    async function api(path, options) {
        const resp = await fetch(path, options);
        let body = null;
        try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);
        return body;
    }

    function json(method, payload) {
        return {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {}),
        };
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

    function fmtBytes(n) {
        if (!n && n !== 0) return '—';
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
        return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
    }

    // Loaded lazily on first reveal: a machine page is opened far more often to look at
    // temperatures than to change a backup policy, and this costs a request plus a path
    // expansion on the server.
    pane.addEventListener('tab:shown', () => { if (!loaded) load(); });

    async function load() {
        loaded = true;
        pane.replaceChildren(el('p', 'stat-card__meta', 'Loading…'));
        try {
            data = await api(`/api/backups/machines/${encodeURIComponent(MACHINE)}`);
        } catch (e) {
            pane.replaceChildren(el('p', 'setting__error', e.message));
            return;
        }
        if (!dirty) {
            draftInclude = (data.config.include || []).slice();
            draftExclude = (data.config.exclude || []).slice();
        }
        render();
    }

    function render() {
        const active = document.activeElement;
        const focusId = active && active.id ? active.id : null;

        pane.replaceChildren();
        pane.appendChild(policyCard());
        pane.appendChild(previewCard());
        pane.appendChild(runsCard());

        if (focusId) {
            const restored = document.getElementById(focusId);
            if (restored) restored.focus();
        }
    }

    function policyCard() {
        const card = el('div', 'card');
        card.appendChild(el('h2', 'section-title', 'Backup policy'));

        const effective = data.effective || {};
        const config = data.config || {};

        // Three states, and the operator needs to know which one they are in before they
        // change anything: following the fleet, forced on, or forced off.
        const state = el('p', 'stat-card__meta');
        if (config.enabled === null || config.enabled === undefined) {
            state.textContent = effective.enabled
                ? 'Following the fleet policy: this machine IS backed up.'
                : 'Following the fleet policy: nothing on this machine is backed up.';
        } else {
            state.textContent = config.enabled
                ? 'Overridden: this machine is backed up even if the fleet policy is off.'
                : 'Overridden: this machine is NOT backed up, even though the fleet policy is on.';
        }
        card.appendChild(state);

        const grid = el('div', 'bk-schedule-grid');

        const modeWrap = el('div');
        modeWrap.appendChild(el('label', 'setting__label', 'Back up this machine'));
        const mode = el('select', 'input');
        mode.id = 'backup-mode';
        mode.style.width = '100%';
        [['', 'Follow the fleet policy'], ['on', 'Always back up'],
         ['off', 'Never back up']].forEach(([value, label]) => {
            const option = el('option', null, label);
            option.value = value;
            if ((config.enabled === null || config.enabled === undefined) ? value === ''
                : (config.enabled ? value === 'on' : value === 'off')) {
                option.selected = true;
            }
            mode.appendChild(option);
        });
        modeWrap.appendChild(mode);
        grid.appendChild(modeWrap);
        card.appendChild(grid);

        card.appendChild(pathEditor(
            'Extra folders for this machine', 'include', draftInclude,
            'ADDED to the fleet list below, not replacing it. Use it for something only '
            + 'this PC has — a local project folder, a line-of-business data directory.',
            'e.g. D:\\Finance or %Users%\\Projects'));
        card.appendChild(pathEditor(
            'Extra exclusions for this machine', 'exclude', draftExclude,
            'Also added to the fleet exclusions.',
            'e.g. D:\\Scratch\\** or *.bak'));

        // The inherited policy, read-only. Shown because "extra paths" is meaningless
        // without knowing what they are extra TO.
        const inherited = el('details', 'bk-tokens');
        inherited.appendChild(el('summary', null,
            'Fleet policy this machine inherits'));
        const list = el('div');
        list.appendChild(el('p', 'setting__default',
            `Includes: ${(data.effective.include || []).join(', ') || 'none'}`));
        list.appendChild(el('p', 'setting__default',
            `Excludes: ${(data.effective.exclude || []).join(', ') || 'none'}`));
        inherited.appendChild(list);
        card.appendChild(inherited);

        const actions = el('div', 'settings-actions');
        const save = el('button', 'btn btn--primary', 'Save');
        const status = el('span', 'settings-actions__status');
        status.id = 'backup-save-status';
        save.addEventListener('click', async () => {
            status.textContent = '';
            const value = document.getElementById('backup-mode').value;
            try {
                data = await api(`/api/backups/machines/${encodeURIComponent(MACHINE)}`,
                                 json('PUT', {
                                     enabled: value === '' ? null : value === 'on',
                                     include: draftInclude,
                                     exclude: draftExclude,
                                 }));
                draftInclude = (data.config.include || []).slice();
                draftExclude = (data.config.exclude || []).slice();
                dirty = false;
                render();
                document.getElementById('backup-save-status').textContent = 'Saved.';
            } catch (e) {
                status.textContent = e.message;
            }
        });
        actions.appendChild(save);
        actions.appendChild(status);
        card.appendChild(actions);
        return card;
    }

    function pathEditor(title, kind, values, help, placeholder) {
        const wrap = el('div');
        wrap.appendChild(el('h3', 'perm-subhead', title));
        wrap.appendChild(el('p', 'setting__default', help));

        const chips = el('div', 'chip-list');
        values.forEach((value, index) => {
            const chip = el('span', 'chip');
            chip.appendChild(el('span', 'chip__name', value));
            const remove = el('button', 'chip__remove');
            remove.type = 'button';
            remove.textContent = '×';
            remove.setAttribute('aria-label', `Remove ${value}`);
            remove.addEventListener('click', () => {
                values.splice(index, 1);
                dirty = true;
                render();
            });
            chip.appendChild(remove);
            chips.appendChild(chip);
        });
        wrap.appendChild(chips);

        const adder = el('div', 'chip-add');
        const input = el('input', 'input');
        input.id = `backup-path-${kind}`;
        input.placeholder = placeholder;
        input.autocomplete = 'off';
        input.spellcheck = false;
        const add = el('button', 'btn', 'Add');
        const commit = () => {
            const value = input.value.trim();
            if (!value) return;
            values.push(value);
            input.value = '';
            dirty = true;
            render();
        };
        add.addEventListener('click', commit);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); commit(); }
        });
        adder.appendChild(input);
        adder.appendChild(add);
        wrap.appendChild(adder);
        return wrap;
    }

    function previewCard() {
        const card = el('div', 'card');
        card.style.marginTop = 'var(--space-5)';
        card.appendChild(el('h2', 'section-title', 'What this resolves to'));

        if (!data.has_profiles) {
            card.appendChild(el('p', 'setting__default',
                'This machine has not reported its user profiles yet — its agent sends '
                + 'them on the next heartbeat after an upgrade. The patterns will still '
                + 'expand correctly on the machine itself.'));
            return card;
        }

        const preview = data.preview || {};
        if (preview.roots && preview.roots.length) {
            const table = el('table', 'data-table');
            const head = el('thead');
            const headRow = el('tr');
            ['Folder', 'User', 'From'].forEach((l) => headRow.appendChild(el('th', null, l)));
            head.appendChild(headRow);
            table.appendChild(head);
            const body = el('tbody');
            preview.roots.forEach((root) => {
                const row = el('tr');
                row.appendChild(el('td', null, root.path));
                row.appendChild(el('td', null, root.user || '—'));
                row.appendChild(el('td', null, root.pattern));
                body.appendChild(row);
            });
            table.appendChild(body);
            card.appendChild(table);
        } else {
            card.appendChild(el('p', 'setting__default',
                'These patterns cover nothing on this machine.'));
        }
        (preview.problems || []).forEach((p) => card.appendChild(el('p', 'setting__error', p)));
        return card;
    }

    function runsCard() {
        const card = el('div', 'card');
        card.style.marginTop = 'var(--space-5)';
        card.appendChild(el('h2', 'section-title', 'Backup history'));

        const runs = data.runs || [];
        if (!runs.length) {
            card.appendChild(el('p', 'setting__default',
                'This machine has not been backed up yet.'));
            return card;
        }

        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        ['Started', 'Status', 'Files', 'Size', 'Trigger'].forEach(
            (l) => headRow.appendChild(el('th', null, l)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        runs.forEach((run) => {
            const row = el('tr');
            row.appendChild(el('td', null, fmtTime(run.started_at)));
            const statusCell = el('td');
            statusCell.appendChild(el('span', `bk-dot bk-dot--${run.status}`));
            statusCell.appendChild(document.createTextNode(run.status));
            if (run.status === 'failed' && run.error) {
                statusCell.appendChild(el('div', 'bk-error', run.error));
            }
            row.appendChild(statusCell);
            row.appendChild(el('td', null,
                run.file_count === null || run.file_count === undefined
                    ? '—' : String(run.file_count)));
            row.appendChild(el('td', null, fmtBytes(run.stored_bytes)));
            row.appendChild(el('td', null, run.trigger || '—'));
            body.appendChild(row);
        });
        table.appendChild(body);
        card.appendChild(table);
        return card;
    }

    // Deep link from the Backups page's exceptions table: /machine/PC-1#backup.
    if (window.location.hash === '#backup') load();
})();
