// Machine page, Overview tab: what is running on this PC, and ending or restarting it.
//
// **Grouped by name, because that is the question.** An operator looking at a PC that is
// pinned at 100% wants to know that "Chrome is eating the CPU", not that pids 4812, 4907
// and eleven of their siblings are each eating 8% of it. Rows are one per NAME with the
// instances summed, expandable to the individual processes -- which is where a pid, a
// window session and the two actions live, because those are per-process facts.
//
// **The list is polled, and the poll IS the subscription.** The machine does no process
// sampling until somebody opens this card and stops again seconds after they close it, so
// this stops polling when the card is collapsed, when the browser tab goes to the
// background, and when the page is left. See hub/processes.py for the other half.
//
// **Nothing here pretends to be Task Manager's refresh rate.** A snapshot crosses a
// heartbeat, so it is a few seconds old by the time it renders and the card says so rather
// than implying live numbers. The first one takes longer still (the machine has to be told
// to start sampling), which is why "waiting" is a rendered state and not a spinner.
//
// **Every action is confirmed against the name and pid the operator SAW.** The list is
// always slightly stale and Windows recycles pids within minutes, so both travel to the
// agent, which re-reads the live process and refuses a mismatch rather than ending whatever
// inherited the id. The confirmation exists for the other half of that: on the far end of
// this button is somebody's unsaved work.
//
// Same two rules as the rest of the console: built with textContent/createElement, never
// innerHTML -- process names, image paths and usernames are arbitrary text arriving from a
// remote machine -- and every string comes from the catalog.

(function () {
    'use strict';

    const card = document.getElementById('process-browser');
    if (!card) return;

    const t = window.t;
    const tPlural = window.tPlural;
    const machineConfig = document.getElementById('machine-config');
    const MACHINE = machineConfig.dataset.machine;
    const CAN_ISSUE = machineConfig.dataset.canIssueCommands === '1';

    const countEl = document.getElementById('process-count');
    const statusEl = document.getElementById('process-status');
    const resultEl = document.getElementById('process-result');
    const bodyEl = document.getElementById('process-body');
    const filterEl = document.getElementById('process-filter');
    const groupEl = document.getElementById('process-group');

    const dialog = document.getElementById('process-confirm');
    const dialogTitle = document.getElementById('process-confirm-title');
    const dialogText = document.getElementById('process-confirm-text');
    const dialogTreeRow = document.getElementById('process-confirm-tree-row');
    const dialogTree = document.getElementById('process-confirm-tree');
    const dialogOk = document.getElementById('process-confirm-ok');
    const dialogCancel = document.getElementById('process-confirm-cancel');

    // Fallback cadence only. The hub serves its own poll_interval so the console, the watch
    // TTL and the agent's sampling stay one decision rather than three constants drifting
    // apart -- this is what we use until the first response arrives.
    let pollSeconds = 5;
    let pollTimer = null;
    let inFlight = false;

    let snapshot = null;          // the last payload from the hub
    let expanded = new Set();     // group names the operator has opened
    let sort = { key: 'cpu', dir: 'desc' };
    let pending = null;           // the command we are waiting on, if any
    let tooOld = null;            // the agent version, when it predates this feature

    // The release that added process reporting. An agent below it never sends a snapshot, so
    // without this check the card would sit on "waiting for the machine" forever and look
    // broken -- during a fleet rollout that is every PC that has not self-updated yet. Same
    // shape as the terminal's MIN_* gates, and the same reason for existing.
    const MIN_PROCESS_AGENT = '3.24.0';

    function versionLess(a, b) {
        const pa = String(a).split('.').map(Number);
        const pb = String(b).split('.').map(Number);
        for (let i = 0; i < 3; i++) {
            const x = pa[i] || 0, y = pb[i] || 0;
            if (x !== y) return x < y;
        }
        return false;
    }

    // Asked once, the first time the card is opened. The answer only changes when the agent
    // self-updates, which takes it offline briefly and reloads this page's data anyway.
    async function checkAgentVersion() {
        if (tooOld !== null) return;
        try {
            const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}`);
            if (!resp.ok) return;
            const info = await resp.json();
            const version = info && info.companion_version;
            tooOld = (version && versionLess(version, MIN_PROCESS_AGENT)) ? version : false;
            if (tooOld) { render(); renderStatus(); }
        } catch (e) { /* the card works regardless; this only explains an empty one */ }
    }

    // ---- formatting ---------------------------------------------------------------

    function formatPercent(value) {
        if (!Number.isFinite(Number(value))) return t('machine.unknown');
        const n = Number(value);
        // Under 0.05% is noise dressed up as a reading: a hundred idle processes each
        // showing "0.0 %" reads as a hundred measurements, when the honest answer is that
        // they are doing nothing.
        if (n < 0.05) return '–';
        return t('machine.percent', { value: n.toFixed(1) });
    }

    function formatMemory(mb) {
        const n = Number(mb);
        if (!Number.isFinite(n)) return t('machine.unknown');
        if (n >= 1024) return t('machine.processes.gb', { value: (n / 1024).toFixed(2) });
        return t('machine.processes.mb', { value: n.toFixed(1) });
    }

    function formatAge(seconds) {
        const n = Number(seconds);
        if (!Number.isFinite(n)) return t('machine.unknown');
        if (n < 60) return t('machine.ago.seconds', { value: Math.max(0, Math.round(n)) });
        return t('machine.ago.minutes', { value: Math.floor(n / 60) });
    }

    // ---- grouping and sorting -----------------------------------------------------

    // One row per process NAME, instances summed. The name is kept exactly as the machine
    // reported it (case included) -- two files that differ only in case are still two
    // programs -- but grouped case-insensitively, because Windows itself is.
    function group(list) {
        const groups = new Map();
        for (const proc of list) {
            const key = String(proc.name || '').toLowerCase();
            let entry = groups.get(key);
            if (!entry) {
                entry = {
                    key,
                    name: proc.name,
                    cpu: 0,
                    mem: 0,
                    instances: [],
                    users: new Set(),
                    services: [],
                    protected: !!proc.protected,
                };
                groups.set(key, entry);
            }
            entry.cpu += Number(proc.cpu_pct) || 0;
            entry.mem += Number(proc.mem_mb) || 0;
            entry.instances.push(proc);
            if (proc.user) entry.users.add(proc.user);
            for (const svc of proc.services || []) {
                if (!entry.services.includes(svc)) entry.services.push(svc);
            }
        }
        for (const entry of groups.values()) {
            entry.instances.sort((a, b) => (Number(b.cpu_pct) || 0) - (Number(a.cpu_pct) || 0));
        }
        return [...groups.values()];
    }

    function compare(a, b) {
        let cmp;
        if (sort.key === 'cpu' || sort.key === 'mem') {
            cmp = (Number(a[sort.key]) || 0) - (Number(b[sort.key]) || 0);
        } else if (sort.key === 'pid') {
            cmp = (Number(a.pid) || 0) - (Number(b.pid) || 0);
        } else if (sort.key === 'user') {
            cmp = String(a.user || '').localeCompare(String(b.user || ''), undefined,
                                                     { sensitivity: 'base' });
        } else {
            cmp = String(a.name || '').localeCompare(String(b.name || ''), undefined,
                                                     { sensitivity: 'base' });
        }
        // Name is the tiebreak and always ascending, so rows do not shuffle between polls
        // when a column of zeroes is what is being sorted.
        if (cmp === 0 && sort.key !== 'name') {
            cmp = String(a.name || '').localeCompare(String(b.name || ''), undefined,
                                                     { sensitivity: 'base' });
        }
        return sort.dir === 'desc' ? -cmp : cmp;
    }

    // Name, user, image path and hosted services all match: an operator hunting a stuck
    // print job types "spool" and should find spoolsv whether they were thinking of the
    // process or the service.
    function matches(proc, needle) {
        if (!needle) return true;
        const haystack = [proc.name, proc.user, proc.path, (proc.services || []).join(' ')]
            .join(' ').toLowerCase();
        return haystack.includes(needle);
    }

    // ---- rendering ----------------------------------------------------------------

    function headerCell(key, label, numeric) {
        const th = document.createElement('th');
        th.textContent = label;
        th.dataset.sort = key;
        if (numeric) th.className = 'process-table__num';
        const active = sort.key === key;
        th.classList.toggle('is-sorted', active);
        th.dataset.dir = active ? sort.dir : '';
        th.setAttribute('aria-sort',
            active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
        th.addEventListener('click', () => {
            if (sort.key === key) {
                sort.dir = sort.dir === 'asc' ? 'desc' : 'asc';
            } else {
                sort.key = key;
                // Usage columns open on the biggest consumer, names on A-Z. Anything else
                // makes the first click on "CPU" show the idlest processes on the machine.
                sort.dir = (key === 'cpu' || key === 'mem') ? 'desc' : 'asc';
            }
            render();
        });
        return th;
    }

    function cell(text, className) {
        const td = document.createElement('td');
        td.textContent = text;
        if (className) td.className = className;
        return td;
    }

    function actionButton(labelKey, onClick, danger) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `btn ${danger ? 'btn--danger' : 'btn--ghost'}`;
        btn.textContent = t(labelKey);
        btn.addEventListener('click', onClick);
        return btn;
    }

    function disabledNote(labelKey, titleKey) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn--ghost';
        btn.textContent = t(labelKey);
        btn.disabled = true;
        btn.title = t(titleKey);
        return btn;
    }

    function groupRow(entry) {
        const tr = document.createElement('tr');
        tr.className = 'process-row';

        const nameCell = document.createElement('td');
        // A group of one is not a group: showing a twisty on a single process invites a
        // click that reveals the same row again.
        if (entry.instances.length > 1) {
            const toggle = document.createElement('button');
            toggle.type = 'button';
            toggle.className = 'process-row__toggle';
            const open = expanded.has(entry.key);
            toggle.textContent = open ? '▾' : '▸';
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            toggle.setAttribute('aria-label', tPlural('machine.processes.instances',
                                                      entry.instances.length));
            toggle.addEventListener('click', () => {
                if (expanded.has(entry.key)) expanded.delete(entry.key);
                else expanded.add(entry.key);
                render();
            });
            nameCell.appendChild(toggle);
        } else {
            const spacer = document.createElement('span');
            spacer.className = 'process-row__toggle process-row__toggle--empty';
            nameCell.appendChild(spacer);
        }

        const name = document.createElement('span');
        name.className = 'process-row__name';
        name.textContent = entry.name;
        // The image path is the answer to "is this the real svchost?", but it is far too
        // long to be a column -- so it is the row's tooltip, where it costs no layout.
        const first = entry.instances[0] || {};
        if (first.path) name.title = first.path;
        nameCell.appendChild(name);

        if (entry.instances.length > 1) {
            const badge = document.createElement('span');
            badge.className = 'process-row__count';
            badge.textContent = `×${entry.instances.length}`;
            nameCell.appendChild(badge);
        }
        if (entry.services.length) {
            const svc = document.createElement('span');
            svc.className = 'process-row__service';
            svc.textContent = t('machine.processes.services',
                                { names: entry.services.join(', ') });
            nameCell.appendChild(svc);
        }
        tr.appendChild(nameCell);

        tr.appendChild(cell(formatPercent(entry.cpu), 'process-table__num'));
        tr.appendChild(cell(formatMemory(entry.mem), 'process-table__num'));
        tr.appendChild(cell(entry.users.size === 1 ? [...entry.users][0]
                            : entry.users.size ? tPlural('machine.processes.users',
                                                         entry.users.size)
                            : t('machine.unknown')));
        tr.appendChild(cell(entry.instances.length === 1 ? String(first.pid) : '',
                            'process-table__num'));

        const actions = document.createElement('td');
        actions.className = 'process-table__actions';
        if (CAN_ISSUE) {
            if (entry.protected) {
                actions.appendChild(disabledNote('machine.processes.end',
                                                 'machine.processes.protected_title'));
            } else {
                actions.appendChild(actionButton('machine.processes.end',
                    () => confirmEnd(entry.name, entry.instances.map((p) => p.pid)), true));
            }
            if (entry.instances.length === 1) {
                actions.appendChild(actionButton('machine.processes.restart',
                    () => confirmRestart(entry.name, first.pid), false));
            } else {
                actions.appendChild(disabledNote('machine.processes.restart',
                                                 'machine.processes.restart_group_title'));
            }
        }
        tr.appendChild(actions);
        return tr;
    }

    function instanceRow(entry, proc) {
        const tr = document.createElement('tr');
        tr.className = 'process-row process-row--instance';

        const nameCell = document.createElement('td');
        const name = document.createElement('span');
        name.className = 'process-row__name';
        name.textContent = proc.path || proc.name;
        if (proc.path) name.title = proc.path;
        nameCell.appendChild(name);
        tr.appendChild(nameCell);

        tr.appendChild(cell(formatPercent(proc.cpu_pct), 'process-table__num'));
        tr.appendChild(cell(formatMemory(proc.mem_mb), 'process-table__num'));
        tr.appendChild(cell(proc.user || t('machine.unknown')));
        tr.appendChild(cell(String(proc.pid), 'process-table__num'));

        const actions = document.createElement('td');
        actions.className = 'process-table__actions';
        if (CAN_ISSUE) {
            if (entry.protected) {
                actions.appendChild(disabledNote('machine.processes.end',
                                                 'machine.processes.protected_title'));
            } else {
                actions.appendChild(actionButton('machine.processes.end',
                    () => confirmEnd(proc.name, [proc.pid]), true));
            }
            actions.appendChild(actionButton('machine.processes.restart',
                () => confirmRestart(proc.name, proc.pid), false));
        }
        tr.appendChild(actions);
        return tr;
    }

    function flatRow(proc) {
        // Ungrouped: one row per process, and the group's own expander is meaningless. Built
        // from the same instance renderer with a synthetic single-member group so the
        // protected rule is applied in exactly one place.
        return instanceRow({ protected: !!proc.protected }, proc);
    }

    function emptyMessage(text) {
        const div = document.createElement('div');
        div.className = 'stat-card__meta';
        div.textContent = text;
        return div;
    }

    function render() {
        bodyEl.replaceChildren();

        if (snapshot === null) {
            countEl.textContent = '';
            bodyEl.appendChild(emptyMessage(t('common.loading')));
            return;
        }

        const all = Array.isArray(snapshot.processes) ? snapshot.processes : [];
        countEl.textContent = all.length
            ? tPlural('machine.processes.count', all.length) : '';

        // Two genuinely different absences, and collapsing them is how a machine that is
        // merely 8 seconds into its first sample ends up looking broken: nothing reported
        // YET (normal, and it says how long it takes) versus a report that has gone stale
        // (the machine stopped talking to us).
        if (!all.length) {
            if (tooOld) {
                bodyEl.appendChild(emptyMessage(t('machine.processes.needs_agent',
                    { version: tooOld, required: MIN_PROCESS_AGENT })));
                return;
            }
            bodyEl.appendChild(emptyMessage(
                snapshot.reported_at === null
                    ? t('machine.processes.waiting')
                    : t('machine.processes.none')));
            return;
        }

        const needle = (filterEl.value || '').trim().toLowerCase();
        const visible = all.filter((p) => matches(p, needle));
        if (!visible.length) {
            bodyEl.appendChild(emptyMessage(t('machine.processes.no_match')));
            return;
        }

        const table = document.createElement('table');
        table.className = 'data-table data-table--sortable process-table';
        const thead = document.createElement('thead');
        const headRow = document.createElement('tr');
        headRow.append(
            headerCell('name', t('machine.processes.col.name'), false),
            headerCell('cpu', t('machine.processes.col.cpu'), true),
            headerCell('mem', t('machine.processes.col.memory'), true),
            headerCell('user', t('machine.processes.col.user'), false),
            headerCell('pid', t('machine.processes.col.pid'), true),
        );
        const actionsHead = document.createElement('th');
        actionsHead.textContent = CAN_ISSUE ? t('machine.processes.col.actions') : '';
        headRow.appendChild(actionsHead);
        thead.appendChild(headRow);
        table.appendChild(thead);

        const tbody = document.createElement('tbody');
        if (groupEl.checked) {
            const groups = group(visible);
            // The group's own totals are what the sort acts on -- sorting groups by their
            // biggest member would put a 40-process browser below a single hot thread.
            groups.sort((a, b) => compare(
                { name: a.name, cpu: a.cpu, mem: a.mem, pid: a.instances[0]?.pid,
                  user: [...a.users][0] || '' },
                { name: b.name, cpu: b.cpu, mem: b.mem, pid: b.instances[0]?.pid,
                  user: [...b.users][0] || '' }));
            for (const entry of groups) {
                tbody.appendChild(groupRow(entry));
                if (entry.instances.length > 1 && expanded.has(entry.key)) {
                    for (const proc of entry.instances) {
                        tbody.appendChild(instanceRow(entry, proc));
                    }
                }
            }
        } else {
            const rows = visible.slice().sort((a, b) => compare(
                { name: a.name, cpu: a.cpu_pct, mem: a.mem_mb, pid: a.pid, user: a.user },
                { name: b.name, cpu: b.cpu_pct, mem: b.mem_mb, pid: b.pid, user: b.user }));
            for (const proc of rows) tbody.appendChild(flatRow(proc));
        }
        table.appendChild(tbody);
        bodyEl.appendChild(table);

        if (Number(snapshot.truncated) > 0) {
            bodyEl.appendChild(emptyMessage(
                t('machine.processes.truncated', { count: Number(snapshot.truncated) })));
        }
    }

    function renderStatus() {
        if (snapshot === null) { statusEl.textContent = ''; return; }
        if (snapshot.reported_at === null) {
            statusEl.textContent = t('machine.processes.waiting_short');
        } else if (snapshot.stale) {
            statusEl.textContent = t('machine.processes.stale',
                                     { age: formatAge(snapshot.age_seconds) });
        } else {
            statusEl.textContent = t('machine.processes.updated',
                                     { age: formatAge(snapshot.age_seconds) });
        }
        statusEl.classList.toggle('process-status--stale', !!snapshot.stale);
    }

    function showResult(text, failed) {
        resultEl.hidden = false;
        resultEl.textContent = text;
        resultEl.classList.toggle('process-result--failed', !!failed);
    }

    // ---- polling ------------------------------------------------------------------

    async function load() {
        if (inFlight) return;
        inFlight = true;
        try {
            const resp = await fetch(
                `/api/machines/${encodeURIComponent(MACHINE)}/processes`);
            if (!resp.ok) {
                // A 403 here means the machine left this operator's scope while they were
                // reading; there is nothing to retry and nothing to show.
                if (resp.status === 403) stopPolling();
                return;
            }
            const body = await resp.json();
            if (Number(body.poll_interval) > 0) pollSeconds = Number(body.poll_interval);
            snapshot = body;
            render();
            renderStatus();
        } catch (e) {
            /* transient: the next tick tries again, and the age readout already shows drift */
        } finally {
            inFlight = false;
        }
    }

    function active() {
        return card.open && document.visibilityState === 'visible';
    }

    function syncPolling() {
        if (active() && pollTimer === null) {
            checkAgentVersion();
            load();
            pollTimer = setInterval(load, pollSeconds * 1000);
        } else if (!active() && pollTimer !== null) {
            stopPolling();
        }
    }

    function stopPolling() {
        if (pollTimer !== null) clearInterval(pollTimer);
        pollTimer = null;
    }

    // ---- actions ------------------------------------------------------------------

    function closeDialog() {
        if (dialog.open) dialog.close();
        pending = null;
    }

    function confirmEnd(name, pids) {
        pending = { kind: 'kill', name, pids };
        dialogTitle.textContent = t('machine.processes.confirm.end_title', { name });
        dialogText.textContent = pids.length === 1
            ? t('machine.processes.confirm.end_one',
                { name, pid: pids[0], machine: MACHINE })
            : t('machine.processes.confirm.end_many',
                { count: pids.length, name, machine: MACHINE });
        dialogTreeRow.hidden = false;
        dialogTree.checked = false;
        dialogOk.textContent = t('machine.processes.end');
        dialog.showModal();
    }

    function confirmRestart(name, pid) {
        pending = { kind: 'restart', name, pid };
        dialogTitle.textContent = t('machine.processes.confirm.restart_title', { name });
        dialogText.textContent = t('machine.processes.confirm.restart_text',
                                   { name, pid, machine: MACHINE });
        dialogTreeRow.hidden = true;
        dialogOk.textContent = t('machine.processes.restart');
        dialog.showModal();
    }

    async function submit() {
        if (!pending) return;
        const action = pending;
        const tree = dialogTree.checked;
        closeDialog();
        showResult(t('machine.processes.queued', { name: action.name }), false);

        const url = `/api/machines/${encodeURIComponent(MACHINE)}/processes/` +
                    (action.kind === 'kill' ? 'kill' : 'restart');
        const payload = action.kind === 'kill'
            ? { name: action.name, pids: action.pids, tree }
            : { name: action.name, pid: action.pid };

        try {
            const resp = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const body = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                showResult(t('machine.processes.failed',
                             { error: body.error || `HTTP ${resp.status}` }), true);
                return;
            }
            watchCommand(body.command_id, action.name);
        } catch (e) {
            showResult(t('machine.processes.failed', { error: e.message }), true);
        }
    }

    // Follow the queued command to its end. The agent picks it up on its own poll, so this
    // is seconds rather than instant -- and it can legitimately never happen, which is why
    // the expired status is reported as its own outcome rather than as a timeout here.
    function watchCommand(commandId, name) {
        if (!commandId) return;
        let ticks = 0;
        const timer = setInterval(async () => {
            ticks += 1;
            // ~2 minutes. Past that the command has not expired (its TTL is longer) but
            // nothing useful is happening, and a poll that runs forever outlives the page.
            if (ticks > 60) {
                clearInterval(timer);
                showResult(t('machine.processes.gave_up', { name }), true);
                return;
            }
            try {
                const resp = await fetch(`/api/fleet/commands/${encodeURIComponent(commandId)}`);
                if (!resp.ok) return;
                const command = await resp.json();
                if (command.status === 'pending' || command.status === 'claimed') return;
                clearInterval(timer);
                if (command.status === 'expired') {
                    showResult(t('machine.processes.expired', { name }), true);
                    return;
                }
                const output = (command.result && command.result.output) || '';
                const ok = command.status === 'done';
                showResult(ok ? t('machine.processes.done', { name, detail: output })
                              : t('machine.processes.failed',
                                  { error: output || command.status }), !ok);
                // The machine has just changed underneath the list; ask for a fresh one
                // rather than leaving a killed process on screen until the next tick.
                load();
            } catch (e) {
                /* keep polling: a dropped request is not an outcome */
            }
        }, 2000);
    }

    // ---- wiring -------------------------------------------------------------------

    card.addEventListener('toggle', syncPolling);
    document.addEventListener('visibilitychange', syncPolling);
    // Filtering and grouping are views of the snapshot we already hold, so neither waits on
    // the network.
    filterEl.addEventListener('input', render);
    groupEl.addEventListener('change', render);
    dialogOk.addEventListener('click', submit);
    dialogCancel.addEventListener('click', closeDialog);
    // Esc closes the dialog itself (showModal does that); this clears what it was about.
    dialog.addEventListener('close', () => { pending = null; });
})();
