// Patches page: what the fleet is missing, what is approved, when it may run.
//
// Same two rules as packages.js and permissions.js, for the same reasons:
//
//  * Everything is built with textContent / createElement, never innerHTML. Update
//    titles come from Windows Update and winget, hostnames come from operators, and
//    failure text is echoed straight from an agent -- all arbitrary strings.
//  * The vocabularies (classifications, sources, reboot policies, retry defaults) come
//    from GET /api/patches, not a copy here. A hardcoded list silently stops offering a
//    new kind, which reads to an operator as "the feature is broken".
//
// The management controls are built only when the server said `can_manage`. That is not
// the security boundary -- every endpoint re-checks the capability -- it is so an
// operator holding `view` is not handed an Approve button that 403s.
//
// The run view polls while a run is unresolved. The hub's scheduler ticks on its own
// interval, so this page is a viewer of that state and never a driver of it: there is no
// client-side dispatch or retry, and closing the tab does not stop a patch night.

(function () {
    const t = window.t;

    const updatesPane = document.getElementById('updates-pane');
    const approvedPane = document.getElementById('approved-pane');
    const declinedPane = document.getElementById('declined-pane');
    const windowsPane = document.getElementById('windows-pane');
    const runsPane = document.getElementById('runs-pane');
    const actions = document.getElementById('patches-actions');
    const summaryBox = document.getElementById('patches-summary');

    const windowModal = document.getElementById('window-modal');
    const runModal = document.getElementById('run-modal');

    let vocab = { classifications: [], sources: [], reboot_policies: [], scope_kinds: [] };
    let canManage = false;
    let updates = [];
    let editingWindowId = null;
    let pollTimer = null;

    // Statuses that mean "nothing more will happen here". Mirrors patches.TARGET_TERMINAL;
    // used only to decide whether to keep polling, so drift costs a wasted request rather
    // than correctness.
    const TERMINAL = ['applied', 'partial', 'failed', 'nothing_to_do', 'expired',
                      'cancelled'];

    // Monday-first, matching patches.py's days_mask (bit 0 = Monday). The order here IS
    // the bit order -- getting it wrong would schedule Sunday's window on Monday.
    const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

    async function api(path, options) {
        const resp = await fetch(path, options);
        let body = null;
        try { body = await resp.json(); } catch (e) { /* an empty body is fine */ }
        if (!resp.ok) throw new Error((body && body.error) || t('patches.request_failed'));
        return body;
    }

    function post(path, payload) {
        return api(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {}),
        });
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function labelFor(list, name) {
        const found = (list || []).find((entry) => entry.name === name);
        return found ? found.label : name;
    }

    function empty(pane, message) {
        pane.replaceChildren(el('p', 'stat-card__meta', message));
    }

    // ------------------------------------------------------------------ summary

    function renderSummary(summary) {
        summaryBox.replaceChildren();
        const cards = [
            [t('patches.summary.machines'), summary.machines_with_updates],
            [t('patches.summary.updates'), summary.updates],
            [t('patches.summary.security'),
             (summary.by_classification && summary.by_classification.security) || 0],
        ];
        for (const [label, value] of cards) {
            const card = el('div', 'stat-card');
            card.append(el('div', 'stat-card__label', label),
                        el('div', 'stat-card__value', value));
            summaryBox.append(card);
        }
    }

    // ------------------------------------------------------------------ updates

    // The three panes, in tab order, and the ONLY thing that sorts a row into one of them
    // is the decision the server sent: '' when nobody has ruled on it, then the two values
    // of patches.APPROVAL_DECISIONS. Absence is a state here exactly as it is in the
    // patch_approvals schema -- see patches.py -- so undecided is the empty string and not
    // a third stored word.
    //
    // No Decision column in any of them: the tab a row is sitting in IS its decision, and
    // a column whose every cell reads "Approved" is noise in the one place an operator is
    // scanning titles. `emptyKey` is the message for a pane that is empty while the fleet
    // is reporting something; a fleet reporting nothing at all gets patches.no_updates in
    // all three, because "no update is waiting for you" would be a true sentence hiding
    // the more useful fact that no machine has reported yet.
    const GROUPS = [
        { decision: '', pane: updatesPane, count: 'count-updates',
          emptyKey: 'patches.none_undecided' },
        { decision: 'approved', pane: approvedPane, count: 'count-approved',
          emptyKey: 'patches.none_approved' },
        { decision: 'declined', pane: declinedPane, count: 'count-declined',
          emptyKey: 'patches.none_declined' },
    ];

    async function decide(uid, decision, title) {
        try {
            await post('/api/patches/approvals', { uid, decision, title });
            await load();
        } catch (e) {
            window.alert(e.message);
        }
    }

    function renderGroup(group, rows) {
        document.getElementById(group.count).textContent = rows.length;
        if (!rows.length) {
            empty(group.pane, updates.length ? t(group.emptyKey) : t('patches.no_updates'));
            return;
        }
        const table = el('table', 'table');
        const head = el('tr');
        [t('patches.col.update'), t('patches.col.classification'), t('patches.col.source'),
         t('patches.col.machines')]
            .forEach((label) => head.append(el('th', null, label)));
        if (canManage) head.append(el('th', null, t('patches.col.actions')));
        table.append(el('thead').appendChild(head).parentNode);

        const body = el('tbody');
        for (const row of rows) {
            const tr = el('tr');
            const title = el('td');
            title.append(el('div', null, row.title));
            if (row.kb) title.append(el('div', 'stat-card__meta', row.kb));
            tr.append(title);
            tr.append(el('td', null, labelFor(vocab.classifications, row.classification)));
            tr.append(el('td', null, labelFor(vocab.sources, row.source)));
            tr.append(el('td', null, row.machines));
            if (canManage) {
                const cell = el('td');
                // Only the buttons that would change something. Approve on a row already
                // in the Approved pane is a no-op that still costs a POST and an audit
                // entry, and offering it invites the reading that it does something more.
                if (row.decision !== 'approved') {
                    const approve = el('button', 'btn', t('patches.approve'));
                    approve.type = 'button';
                    approve.addEventListener(
                        'click', () => decide(row.uid, 'approved', row.title));
                    cell.append(approve);
                }
                if (row.decision !== 'declined') {
                    const decline = el('button', 'btn', t('patches.decline'));
                    decline.type = 'button';
                    decline.addEventListener(
                        'click', () => decide(row.uid, 'declined', row.title));
                    cell.append(decline);
                }
                if (row.decision) {
                    // Back to Available -- the only way out of a decided pane that is not
                    // itself a decision.
                    const clear = el('button', 'btn', t('patches.undecide'));
                    clear.type = 'button';
                    clear.addEventListener('click', () => decide(row.uid, '', row.title));
                    cell.append(clear);
                }
                tr.append(cell);
            }
            body.append(tr);
        }
        table.append(body);
        group.pane.replaceChildren(table);
    }

    // All three panes are rendered from the one /api/patches response, rather than each
    // fetching on `tab:shown` the way Windows and Runs do: the rows are already in hand,
    // the partition is a filter, and deciding an update has to move it between two panes
    // at once -- one of which is not the one being looked at.
    function renderUpdates() {
        for (const group of GROUPS) {
            renderGroup(group,
                        updates.filter((row) => (row.decision || '') === group.decision));
        }
    }

    // ------------------------------------------------------------------ windows

    function describeDays(mask) {
        const named = DAY_KEYS.filter((_, i) => mask & (1 << i))
            .map((key) => t(`patches.day.${key}`));
        return named.length === 7 ? t('patches.window.every_day') : named.join(', ');
    }

    function describeTime(window) {
        const hh = String(Math.floor(window.start_minute / 60)).padStart(2, '0');
        const mm = String(window.start_minute % 60).padStart(2, '0');
        return t('patches.window.summary',
                 { start: `${hh}:${mm}`, minutes: window.duration_minutes });
    }

    async function renderWindows() {
        let doc;
        try {
            doc = await api('/api/patches/windows');
        } catch (e) {
            empty(windowsPane, e.message);
            return;
        }
        if (!doc.windows.length) {
            empty(windowsPane, t('patches.no_windows'));
            return;
        }
        const table = el('table', 'table');
        const head = el('tr');
        [t('patches.col.window'), t('patches.col.days'), t('patches.col.time'),
         t('patches.col.scope'), t('patches.col.reboot')]
            .forEach((label) => head.append(el('th', null, label)));
        if (canManage) head.append(el('th', null, t('patches.col.actions')));
        table.append(el('thead').appendChild(head).parentNode);

        const body = el('tbody');
        for (const win of doc.windows) {
            const tr = el('tr');
            const name = el('td');
            name.append(el('div', null, win.name));
            if (!win.enabled) {
                name.append(el('div', 'stat-card__meta', t('patches.window.disabled')));
            }
            tr.append(name);
            tr.append(el('td', null, describeDays(win.days_mask)));
            tr.append(el('td', null, describeTime(win)));
            tr.append(el('td', null, win.scope_kind === 'all'
                ? t('patches.window.scope_all')
                : t('patches.window.scope_count', { count: win.machines.length })));
            tr.append(el('td', null, t(`patches.reboot.${win.reboot_policy}`)));
            if (canManage) {
                const cell = el('td');
                const edit = el('button', 'btn', t('common.edit'));
                edit.type = 'button';
                edit.addEventListener('click', () => openWindowEditor(win));
                const remove = el('button', 'btn', t('common.delete'));
                remove.type = 'button';
                remove.addEventListener('click', async () => {
                    if (!window.confirm(t('patches.window.confirm_delete',
                                          { name: win.name }))) return;
                    try {
                        await api(`/api/patches/windows/${win.id}`, { method: 'DELETE' });
                        await renderWindows();
                    } catch (e) { window.alert(e.message); }
                });
                cell.append(edit, remove);
                tr.append(cell);
            }
            body.append(tr);
        }
        table.append(body);
        windowsPane.replaceChildren(table);
    }

    function openWindowEditor(win) {
        editingWindowId = win ? win.id : null;
        document.getElementById('window-modal-title').textContent =
            win ? t('patches.window.edit_title') : t('patches.window.new_title');
        document.getElementById('win-name').value = win ? win.name : '';
        document.getElementById('win-duration').value = win ? win.duration_minutes : 240;
        const start = win ? win.start_minute : 23 * 60;
        document.getElementById('win-start').value =
            `${String(Math.floor(start / 60)).padStart(2, '0')}:${String(start % 60).padStart(2, '0')}`;

        const days = document.getElementById('win-days');
        days.replaceChildren();
        DAY_KEYS.forEach((key, i) => {
            const label = el('label', 'setting__label');
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.dataset.bit = String(i);
            box.checked = win ? Boolean(win.days_mask & (1 << i)) : i === 6;
            label.append(box, document.createTextNode(' ' + t(`patches.day.${key}`)));
            days.append(label);
        });

        const reboot = document.getElementById('win-reboot');
        reboot.replaceChildren();
        for (const policy of vocab.reboot_policies) {
            const option = el('option', null, t(`patches.reboot.${policy}`));
            option.value = policy;
            if (win && win.reboot_policy === policy) option.selected = true;
            reboot.append(option);
        }

        const scope = document.getElementById('win-scope');
        scope.replaceChildren();
        for (const kind of vocab.scope_kinds) {
            const option = el('option', null, kind === 'all'
                ? t('patches.window.scope_all') : t('patches.window.scope_machines'));
            option.value = kind;
            if (win && win.scope_kind === kind) option.selected = true;
            scope.append(option);
        }
        const machines = document.getElementById('win-machines');
        const input = document.createElement('textarea');
        input.className = 'input';
        input.id = 'win-machine-list';
        input.rows = 4;
        input.style.width = '100%';
        input.placeholder = t('patches.window.machines_placeholder');
        input.value = win ? (win.machines || []).join('\n') : '';
        machines.replaceChildren(input);
        const syncScope = () => { machines.hidden = scope.value === 'all'; };
        scope.onchange = syncScope;
        syncScope();

        windowModal.showModal();
    }

    async function saveWindow() {
        const start = document.getElementById('win-start').value || '23:00';
        const [hh, mm] = start.split(':').map(Number);
        let mask = 0;
        document.querySelectorAll('#win-days input[type=checkbox]').forEach((box) => {
            if (box.checked) mask |= (1 << Number(box.dataset.bit));
        });
        const scopeKind = document.getElementById('win-scope').value;
        const payload = {
            name: document.getElementById('win-name').value,
            days_mask: mask,
            start_minute: (hh * 60) + mm,
            duration_minutes: Number(document.getElementById('win-duration').value),
            scope_kind: scopeKind,
            machines: scopeKind === 'all' ? [] :
                (document.getElementById('win-machine-list').value || '')
                    .split('\n').map((s) => s.trim()).filter(Boolean),
            reboot_policy: document.getElementById('win-reboot').value,
        };
        try {
            if (editingWindowId) {
                await api(`/api/patches/windows/${editingWindowId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            } else {
                await post('/api/patches/windows', payload);
            }
            windowModal.close();
            await renderWindows();
        } catch (e) {
            window.alert(e.message);
        }
    }

    // ------------------------------------------------------------------ runs

    async function renderRuns() {
        let doc;
        try {
            doc = await api('/api/patches/runs');
        } catch (e) {
            empty(runsPane, e.message);
            return;
        }
        if (!doc.runs.length) {
            empty(runsPane, t('patches.no_runs'));
            return;
        }
        const table = el('table', 'table');
        const head = el('tr');
        [t('patches.col.started'), t('patches.col.by'), t('patches.col.progress'),
         t('patches.col.status')]
            .forEach((label) => head.append(el('th', null, label)));
        if (canManage) head.append(el('th', null, t('patches.col.actions')));
        table.append(el('thead').appendChild(head).parentNode);

        const body = el('tbody');
        let unresolved = false;
        for (const run of doc.runs) {
            const tr = el('tr');
            const when = el('td');
            when.append(el('div', null, new Date(run.created_at * 1000).toLocaleString()));
            if (run.emergency) {
                when.append(el('div', 'stat-card__meta', t('patches.run.emergency_badge')));
            }
            if (run.note) when.append(el('div', 'stat-card__meta', run.note));
            tr.append(when);
            tr.append(el('td', null, run.created_by));

            const counts = run.counts || {};
            const total = Object.values(counts).reduce((a, b) => a + b, 0);
            const done = TERMINAL.reduce((sum, s) => sum + (counts[s] || 0), 0);
            if (done < total) unresolved = true;
            tr.append(el('td', null, t('patches.run.progress',
                                       { done, total })));
            tr.append(el('td', null, t(`patches.status.${run.status}`)));

            if (canManage) {
                const cell = el('td');
                if (run.status === 'scheduled' || run.status === 'running') {
                    const cancel = el('button', 'btn', t('patches.run.cancel'));
                    cancel.type = 'button';
                    cancel.addEventListener('click', async () => {
                        try {
                            const res = await post(`/api/patches/runs/${run.id}/cancel`);
                            // "cancelled" and "cancelled 8 of 10" are different facts --
                            // machines already restarting are not recalled, because the
                            // patches are on them. Say which happened.
                            window.alert(t('patches.run.cancelled',
                                           { recalled: res.recalled }));
                            await renderRuns();
                        } catch (e) { window.alert(e.message); }
                    });
                    cell.append(cancel);
                }
                const retry = el('button', 'btn', t('patches.run.retry'));
                retry.type = 'button';
                retry.addEventListener('click', async () => {
                    try {
                        await post(`/api/patches/runs/${run.id}/retry`);
                        await renderRuns();
                    } catch (e) { window.alert(e.message); }
                });
                cell.append(retry);
                tr.append(cell);
            }
            body.append(tr);
        }
        table.append(body);
        runsPane.replaceChildren(table);

        // Poll only while something is still moving, and only while this tab is the one
        // being looked at. The scheduler runs regardless; this is a viewer.
        clearTimeout(pollTimer);
        if (unresolved && !runsPane.hidden) {
            pollTimer = setTimeout(renderRuns, 10000);
        }
    }

    function openRunBuilder() {
        document.getElementById('run-note').value = '';
        document.getElementById('run-emergency').checked = false;
        const box = document.getElementById('run-machines');
        const input = document.createElement('textarea');
        input.className = 'input';
        input.id = 'run-machine-list';
        input.rows = 6;
        input.style.width = '100%';
        input.placeholder = t('patches.run.machines_placeholder');
        box.replaceChildren(input);
        runModal.showModal();
    }

    async function startRun() {
        const machines = (document.getElementById('run-machine-list').value || '')
            .split('\n').map((s) => s.trim()).filter(Boolean);
        const emergency = document.getElementById('run-emergency').checked;
        if (!machines.length) {
            window.alert(t('patches.run.pick_machines'));
            return;
        }
        // An emergency run ignores every maintenance window, which means it restarts these
        // machines now. Naming the count is the point of the confirmation.
        if (emergency && !window.confirm(
                t('patches.run.confirm_emergency', { count: machines.length }))) {
            return;
        }
        try {
            await post('/api/patches/runs', {
                machines,
                emergency,
                note: document.getElementById('run-note').value,
                selection: 'approved',
            });
            runModal.close();
            await renderRuns();
        } catch (e) {
            window.alert(e.message);
        }
    }

    // ------------------------------------------------------------------ boot

    async function load() {
        let doc;
        try {
            doc = await api('/api/patches');
        } catch (e) {
            // Into every pane, not just the first: a reader sitting on Approved when the
            // hub goes away must not be left looking at a list that is no longer true --
            // and the tab counts go with it, because a count beside an error is a number
            // nothing on the page can still vouch for.
            for (const group of GROUPS) {
                document.getElementById(group.count).textContent = '';
                empty(group.pane, e.message);
            }
            return;
        }
        vocab = doc.vocabulary;
        canManage = Boolean(doc.can_manage);
        updates = doc.updates;
        renderSummary(doc.summary);
        renderUpdates();

        actions.replaceChildren();
        if (canManage) {
            const newWindow = el('button', 'btn', t('patches.window.new'));
            newWindow.type = 'button';
            newWindow.addEventListener('click', () => openWindowEditor(null));
            const newRun = el('button', 'btn btn--primary', t('patches.run.new'));
            newRun.type = 'button';
            newRun.addEventListener('click', openRunBuilder);
            actions.append(newWindow, newRun);
        }
    }

    document.getElementById('win-save').addEventListener('click', saveWindow);
    document.getElementById('win-cancel').addEventListener(
        'click', () => windowModal.close());
    document.getElementById('run-start').addEventListener('click', startRun);
    document.getElementById('run-cancel').addEventListener('click', () => runModal.close());

    // tabs.js dispatches `tab:shown` on the panel it just revealed (bubbling), including
    // for the tab it restores on load. The two heavier panes fetch from that rather than
    // on boot, so a page opened on Updates costs one request -- and a run poll started on
    // the Runs tab stops mattering the moment another tab is shown, because renderRuns
    // only re-arms while its own pane is visible.
    windowsPane.addEventListener('tab:shown', renderWindows);
    runsPane.addEventListener('tab:shown', renderRuns);

    load();
})();
