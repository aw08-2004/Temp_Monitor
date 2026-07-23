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

    // ---- restore browser state ----
    // `selected` is a Map of path -> {dir}. A ticked FOLDER is stored as one entry, not
    // expanded here: the browser has only ever seen the folders someone clicked into, so
    // "everything under Documents" cannot be enumerated client-side. The hub resolves it
    // against the manifest, which is also the only place that knows what is still
    // restorable after rotation.
    let manifest = null;         // the current listing/search response
    let manifestPath = '';
    let manifestSearch = '';
    let manifestError = '';
    let selected = new Map();
    let restoreBusy = false;
    let restoreStatus = '';
    let restoreOpen = false;
    let machineOptions = [];
    // Held in state, not read off the DOM at submit time: ticking a checkbox re-renders
    // the whole pane, and a target folder someone typed three clicks ago would otherwise
    // be silently wiped -- restoring to the original locations instead of the safe folder
    // they chose, which is the one mistake this form must not make.
    let restoreTarget = null;       // null = "this machine", resolved at first render
    let restoreDir = '';
    let restoreOverwrite = false;

    // ---- "Back up now" state ----
    // The button reports three outcomes, not two: started, queued-because-offline, and
    // queued-because-throttled. Collapsing the last two into "started" would be a lie an
    // operator only discovers when they go looking for an archive that isn't there.
    let runBusy = false;
    let runMessage = '';
    let runError = '';

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
        pane.appendChild(restoreCard());
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
        mode.addEventListener('change', savePolicy);
        modeWrap.appendChild(mode);
        grid.appendChild(modeWrap);
        card.appendChild(grid);

        card.appendChild(pathEditor(
            'Extra folders for this machine', 'include', draftInclude,
            'ADDED to the fleet list below, not replacing it. Use it for something only '
            + 'this PC has — a local project folder, a line-of-business data directory.',
            'e.g. D:\\Finance or %User%\\Scripts'));
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

        // No Save button: changes to the mode select or the path lists above save
        // themselves (savePolicy). This span is the only feedback -- it shows "Saving…"
        // while a write is in flight and "Saved" or the error once it lands.
        const status = el('span', 'autosave', policyStatus.text);
        status.id = 'backup-save-status';
        if (policyStatus.cls) status.className = `autosave ${policyStatus.cls}`;
        card.appendChild(status);
        return card;
    }

    // Held outside render() so the "Saved"/error message survives the re-render that
    // savePolicy triggers on success -- a status span rebuilt every render would blank
    // the moment it had something to say.
    let policyStatus = { text: '', cls: '' };
    let policySaveSeq = 0;

    function setPolicyStatus(text, cls) {
        policyStatus = { text, cls: cls || '' };
        const node = document.getElementById('backup-save-status');
        if (node) {
            node.textContent = text;
            node.className = cls ? `autosave ${cls}` : 'autosave';
        }
    }

    // Auto-save the backup policy. Sends the whole form every time (mode + both path
    // lists), so there is never a partially-applied state, and a sequence guard drops a
    // slow response that a newer edit has already superseded.
    async function savePolicy() {
        const modeEl = document.getElementById('backup-mode');
        const value = modeEl ? modeEl.value : '';
        const seq = ++policySaveSeq;
        setPolicyStatus('Saving…', '');
        try {
            const body = await api(`/api/backups/machines/${encodeURIComponent(MACHINE)}`,
                             json('PUT', {
                                 enabled: value === '' ? null : value === 'on',
                                 include: draftInclude,
                                 exclude: draftExclude,
                             }));
            if (seq !== policySaveSeq) return;   // a later save is already in flight
            data = body;
            draftInclude = (data.config.include || []).slice();
            draftExclude = (data.config.exclude || []).slice();
            dirty = false;
            policyStatus = { text: 'Saved', cls: 'autosave--saved' };
            render();
        } catch (e) {
            if (seq !== policySaveSeq) return;
            setPolicyStatus(e.message, 'autosave--error');
        }
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
                savePolicy();
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
            savePolicy();
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

    // ================================
    // RESTORE
    // ================================
    // A folder at a time, never the whole manifest. One profile is 100k-500k files, so the
    // shape that works on a demo fleet (fetch everything, filter in the browser) is the
    // shape that times out on a real one. Every click is a request for exactly one folder.
    //
    // Collapsed until asked for: opening a machine page must not cost a manifest query on
    // a table with a row per file version.
    function restoreCard() {
        const card = el('div', 'card');
        card.style.marginTop = 'var(--space-5)';
        card.appendChild(el('h2', 'section-title', 'Restore files'));

        const summary = (data.manifest || {});
        if (!summary.file_count) {
            card.appendChild(el('p', 'setting__default',
                'Nothing to restore yet — this machine has no completed file backups. '
                + 'Once one finishes, its contents are browsable here.'));
            return card;
        }

        const meta = el('p', 'stat-card__meta');
        meta.textContent =
            `${summary.file_count.toLocaleString()} file(s), ${fmtBytes(summary.total_bytes)}`
            + ` recoverable across ${summary.chains} chain(s)`
            + ` — newest ${fmtTime(summary.latest_at)}.`;
        card.appendChild(meta);

        if (!restoreOpen) {
            const open = el('button', 'btn', 'Browse backed-up files');
            open.addEventListener('click', () => {
                restoreOpen = true;
                render();
                loadManifest();
                loadMachineOptions();
            });
            card.appendChild(open);
            card.appendChild(restoreHistory());
            return card;
        }

        card.appendChild(browserToolbar());
        if (manifestError) {
            card.appendChild(el('p', 'setting__error', manifestError));
        } else if (!manifest) {
            card.appendChild(el('p', 'stat-card__meta', 'Loading…'));
        } else {
            card.appendChild(browserTable());
        }
        card.appendChild(selectionBar());
        card.appendChild(restoreHistory());
        return card;
    }

    async function loadManifest() {
        manifestError = '';
        try {
            const query = manifestSearch
                ? `search=${encodeURIComponent(manifestSearch)}`
                : `path=${encodeURIComponent(manifestPath)}`;
            const body = await api(
                `/api/backups/machines/${encodeURIComponent(MACHINE)}/manifest?${query}`);
            manifest = body.result;
            data.manifest = body.summary;
        } catch (e) {
            manifest = null;
            manifestError = e.message;
        }
        render();
    }

    async function loadMachineOptions() {
        if (machineOptions.length) return;
        try {
            const machines = await api('/api/machines');
            machineOptions = machines.map((m) => m.machine || m.name || m).filter(Boolean);
            render();
        } catch (e) {
            // The field still accepts free text; a missing suggestion list is cosmetic.
        }
    }

    function goTo(path) {
        manifestPath = path;
        manifestSearch = '';
        manifest = null;
        render();
        loadManifest();
    }

    function browserToolbar() {
        const bar = el('div', 'bk-browser__bar');

        const crumbs = el('div', 'bk-crumbs');
        const root = el('button', 'bk-crumb');
        root.type = 'button';
        root.textContent = MACHINE;
        root.addEventListener('click', () => goTo(''));
        crumbs.appendChild(root);
        if (!manifestSearch) {
            (manifest && manifest.parents ? manifest.parents : []).forEach((parent) => {
                crumbs.appendChild(el('span', 'bk-crumb__sep', '\\'));
                const link = el('button', 'bk-crumb');
                link.type = 'button';
                link.textContent = parent.name;
                link.addEventListener('click', () => goTo(parent.path));
                crumbs.appendChild(link);
            });
            if (manifestPath) {
                const parts = manifestPath.split('\\');
                crumbs.appendChild(el('span', 'bk-crumb__sep', '\\'));
                crumbs.appendChild(el('span', 'bk-crumb bk-crumb--current',
                                      parts[parts.length - 1]));
            }
        } else {
            crumbs.appendChild(el('span', 'bk-crumb__sep', '›'));
            crumbs.appendChild(el('span', 'bk-crumb bk-crumb--current',
                                  `search: ${manifestSearch}`));
        }
        bar.appendChild(crumbs);

        const find = el('div', 'chip-add');
        const input = el('input', 'input');
        input.id = 'restore-search';
        input.placeholder = 'Find a file across every folder…';
        input.value = manifestSearch;
        input.autocomplete = 'off';
        const run = () => {
            manifestSearch = input.value.trim();
            manifest = null;
            render();
            loadManifest();
        };
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); run(); }
        });
        const go = el('button', 'btn', 'Find');
        go.addEventListener('click', run);
        find.appendChild(input);
        find.appendChild(go);
        bar.appendChild(find);
        return bar;
    }

    function browserTable() {
        const wrap = el('div', 'bk-browser');
        const dirs = manifest.dirs || [];
        const files = manifest.files || [];

        if (!dirs.length && !files.length) {
            wrap.appendChild(el('p', 'setting__default',
                manifestSearch ? 'Nothing matched.' : 'This folder holds no backed-up files.'));
            return wrap;
        }

        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        ['', 'Name', 'Size', 'Modified'].forEach((l) => headRow.appendChild(el('th', null, l)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        dirs.forEach((dir) => {
            const row = el('tr');
            row.appendChild(tickCell(dir.path, true));
            const nameCell = el('td');
            const link = el('button', 'bk-link');
            link.type = 'button';
            link.textContent = `📁 ${dir.name}`;
            link.addEventListener('click', () => goTo(dir.path));
            nameCell.appendChild(link);
            nameCell.appendChild(el('span', 'setting__default',
                ` ${dir.file_count.toLocaleString()} file(s)`));
            row.appendChild(nameCell);
            row.appendChild(el('td', null, fmtBytes(dir.total_bytes)));
            row.appendChild(el('td', null, '—'));
            body.appendChild(row);
        });
        files.forEach((file) => {
            const row = el('tr');
            row.appendChild(tickCell(file.path, false));
            // In search mode the bare filename is useless -- three `report.docx` rows tell
            // you nothing about which is which -- so the whole path is shown there.
            row.appendChild(el('td', null, manifestSearch ? file.path : file.name));
            row.appendChild(el('td', null, fmtBytes(file.size)));
            row.appendChild(el('td', null, fmtTime(file.mtime)));
            body.appendChild(row);
        });
        table.appendChild(body);
        wrap.appendChild(table);

        if (manifest.truncated) {
            wrap.appendChild(el('p', 'setting__default',
                'Only the first part of this folder is shown. Tick the folder itself to '
                + 'restore all of it, or use Find to narrow down.'));
        }
        return wrap;
    }

    function tickCell(path, isDir) {
        const cell = el('td');
        const box = el('input');
        box.type = 'checkbox';
        box.checked = selected.has(path.toLowerCase());
        box.setAttribute('aria-label', `Select ${path}`);
        box.addEventListener('change', () => {
            const key = path.toLowerCase();
            if (box.checked) selected.set(key, { path, dir: isDir });
            else selected.delete(key);
            render();
        });
        cell.appendChild(box);
        return cell;
    }

    function selectionBar() {
        const wrap = el('div', 'bk-restore');
        const chosen = [...selected.values()];
        if (!chosen.length) {
            wrap.appendChild(el('p', 'setting__default',
                'Tick files or folders above to restore them. A ticked folder restores '
                + 'everything under it, including files that are no longer in the folders '
                + 'you can see here.'));
            return wrap;
        }

        wrap.appendChild(el('h3', 'perm-subhead',
            `${chosen.length} item(s) selected`));
        const chips = el('div', 'chip-list');
        chosen.slice(0, 12).forEach((item) => {
            const chip = el('span', 'chip');
            chip.appendChild(el('span', 'chip__name', (item.dir ? '📁 ' : '') + item.path));
            const remove = el('button', 'chip__remove');
            remove.type = 'button';
            remove.textContent = '×';
            remove.setAttribute('aria-label', `Deselect ${item.path}`);
            remove.addEventListener('click', () => {
                selected.delete(item.path.toLowerCase());
                render();
            });
            chip.appendChild(remove);
            chips.appendChild(chip);
        });
        if (chosen.length > 12) {
            chips.appendChild(el('span', 'setting__default',
                `and ${chosen.length - 12} more`));
        }
        wrap.appendChild(chips);

        const grid = el('div', 'bk-schedule-grid');

        const targetWrap = el('div');
        targetWrap.appendChild(el('label', 'setting__label', 'Restore onto'));
        const target = el('input', 'input');
        target.id = 'restore-target';
        target.value = restoreTarget === null ? MACHINE : restoreTarget;
        target.setAttribute('list', 'restore-machine-options');
        target.style.width = '100%';
        target.addEventListener('input', () => { restoreTarget = target.value; });
        targetWrap.appendChild(target);
        // /api/machines is itself scope-filtered, so the picker can never suggest a
        // machine the operator is not allowed to write to -- and the server checks the
        // typed value again anyway, since a datalist is a suggestion, not a constraint.
        const options = el('datalist');
        options.id = 'restore-machine-options';
        machineOptions.forEach((name) => {
            const option = el('option');
            option.value = name;
            options.appendChild(option);
        });
        targetWrap.appendChild(options);
        targetWrap.appendChild(el('p', 'setting__default',
            'Another machine, if you are replacing this one. It needs to be in your '
            + 'scope too, and its agent does the work.'));
        grid.appendChild(targetWrap);

        const dirWrap = el('div');
        dirWrap.appendChild(el('label', 'setting__label', 'Write to'));
        const dir = el('input', 'input');
        dir.id = 'restore-dir';
        dir.placeholder = 'e.g. C:\\Restored — blank means the original locations';
        dir.value = restoreDir;
        dir.style.width = '100%';
        dir.addEventListener('input', () => { restoreDir = dir.value; });
        dirWrap.appendChild(dir);
        dirWrap.appendChild(el('p', 'setting__default',
            'A folder is the safe answer: files land under it in their original tree, '
            + 'and nothing live is touched.'));
        grid.appendChild(dirWrap);
        wrap.appendChild(grid);

        const overwriteLabel = el('label', 'bk-check');
        const overwrite = el('input');
        overwrite.type = 'checkbox';
        overwrite.id = 'restore-overwrite';
        overwrite.checked = restoreOverwrite;
        overwrite.addEventListener('change', () => { restoreOverwrite = overwrite.checked; });
        overwriteLabel.appendChild(overwrite);
        overwriteLabel.appendChild(document.createTextNode(
            ' Overwrite files that already exist'));
        wrap.appendChild(overwriteLabel);

        const actions = el('div', 'card-actions');
        const start = el('button', 'btn btn--primary',
                         restoreBusy ? 'Starting…' : 'Restore selected');
        start.disabled = restoreBusy;
        start.addEventListener('click', startRestore);
        actions.appendChild(start);
        const clear = el('button', 'btn', 'Clear selection');
        clear.addEventListener('click', () => { selected = new Map(); render(); });
        actions.appendChild(clear);
        const status = el('span', 'settings-actions__status');
        status.id = 'restore-status';
        status.textContent = restoreStatus;
        actions.appendChild(status);
        wrap.appendChild(actions);
        return wrap;
    }

    async function startRestore() {
        const targetMachine = (restoreTarget === null ? MACHINE : restoreTarget).trim()
                              || MACHINE;
        const targetDir = restoreDir.trim();
        const overwrite = restoreOverwrite;

        // Asked here rather than trusted to the operator's read of the button: a restore
        // over the original locations rewrites live files on a running PC, and it is the
        // one action on this page that cannot be undone by pressing something else.
        if (!targetDir) {
            const ok = window.confirm(
                `Restore ${selected.size} item(s) back to their ORIGINAL locations on `
                + `${targetMachine}?\n\nFiles there will be replaced`
                + (overwrite ? '.' : ' only where they no longer exist.'));
            if (!ok) return;
        }

        restoreBusy = true;
        restoreStatus = '';
        render();
        try {
            const body = await api(
                `/api/backups/machines/${encodeURIComponent(MACHINE)}/restore`,
                json('POST', {
                    target: targetMachine,
                    target_dir: targetDir,
                    overwrite,
                    paths: [...selected.values()].map((item) => item.path),
                }));
            selected = new Map();
            restoreStatus = `Queued: ${body.file_count.toLocaleString()} file(s) from `
                          + `${body.archives} archive(s).`
                          + (body.missing && body.missing.length
                             ? ` Nothing found for: ${body.missing.join(', ')}.` : '');
        } catch (e) {
            restoreStatus = e.message;
        }
        restoreBusy = false;
        await load();       // picks the new restore row up in the history table
    }

    function restoreHistory() {
        const wrap = el('div');
        const restores = data.restores || [];
        if (!restores.length) return wrap;

        wrap.appendChild(el('h3', 'perm-subhead', 'Restore history'));
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        ['Started', 'Status', 'Files', 'From', 'To'].forEach(
            (l) => headRow.appendChild(el('th', null, l)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        restores.forEach((restore) => {
            const row = el('tr');
            row.appendChild(el('td', null, fmtTime(restore.started_at)));
            const statusCell = el('td');
            statusCell.appendChild(el('span', `bk-dot bk-dot--${restore.status}`));
            statusCell.appendChild(document.createTextNode(restore.status));
            if (restore.error) statusCell.appendChild(el('div', 'bk-error', restore.error));
            row.appendChild(statusCell);
            row.appendChild(el('td', null,
                `${(restore.restored_count === null || restore.restored_count === undefined)
                    ? '—' : restore.restored_count.toLocaleString()}`
                + ` / ${(restore.file_count || 0).toLocaleString()}`));
            row.appendChild(el('td', null, restore.source_machine));
            row.appendChild(el('td', null,
                `${restore.machine}${restore.target_dir ? ' → ' + restore.target_dir : ''}`));
            body.appendChild(row);
        });
        table.appendChild(body);
        wrap.appendChild(table);
        return wrap;
    }

    async function backupNow() {
        if (runBusy) return;
        runBusy = true;
        runMessage = '';
        runError = '';
        render();
        try {
            // The route returns the same body as GET, plus status/message -- so the run
            // history and the pending-request line below refresh from the same response
            // rather than needing a second fetch.
            const body = await api(
                `/api/backups/machines/${encodeURIComponent(MACHINE)}/run`,
                json('POST', {}));
            runMessage = body.message || 'Requested.';
            data = body;
        } catch (e) {
            runError = e.message;
        } finally {
            runBusy = false;
            render();
        }
    }

    // A backup is cancellable if it is queued (a request is pending) or in flight (the
    // most recent run is still running). Both facts are already in the machine payload,
    // so the button appears without a second fetch.
    function cancellable() {
        if ((data.config || {}).run_requested_at) return true;
        const runs = data.runs || [];
        return runs.length > 0 && runs[0].status === 'running';
    }

    async function cancelBackup() {
        if (runBusy) return;
        runBusy = true;
        runMessage = '';
        runError = '';
        render();
        try {
            const body = await api(
                `/api/backups/machines/${encodeURIComponent(MACHINE)}/cancel`,
                json('POST', {}));
            runMessage = body.message || 'Cancelled.';
            data = body;
        } catch (e) {
            runError = e.message;
        } finally {
            runBusy = false;
            render();
        }
    }

    function runsCard() {
        const card = el('div', 'card');
        card.style.marginTop = 'var(--space-5)';
        card.appendChild(el('h2', 'section-title', 'Backup history'));

        const actions = el('div', 'card-actions');
        const run = el('button', 'btn btn--primary',
                       runBusy ? 'Working…' : 'Back up now');
        run.id = 'backup-run-now';
        run.disabled = runBusy;
        run.addEventListener('click', backupNow);
        actions.appendChild(run);
        if (cancellable()) {
            const cancel = el('button', 'btn btn--danger', 'Cancel backup');
            cancel.id = 'backup-cancel';
            cancel.disabled = runBusy;
            cancel.addEventListener('click', cancelBackup);
            actions.appendChild(cancel);
        }
        const status = el('span', 'settings-actions__status');
        if (runError) {
            status.className = 'setting__error';
            status.textContent = runError;
        } else {
            status.textContent = runMessage;
        }
        actions.appendChild(status);
        card.appendChild(actions);

        // Survives a page reload, unlike runMessage: the request is server state, so an
        // operator returning to the tab still sees that a backup is waiting on this PC
        // rather than assuming their earlier click did nothing.
        const pendingAt = (data.config || {}).run_requested_at;
        if (pendingAt) {
            card.appendChild(el('p', 'setting__default',
                `A backup was requested at ${fmtTime(pendingAt)} and will start as soon `
                + 'as this PC is online.'));
        }

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
