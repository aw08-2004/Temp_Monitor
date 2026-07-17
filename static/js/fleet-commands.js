// Fleet command panel on the machine detail page.
//
// Issues commands via POST /api/fleet/commands (Google-session gated, same as the
// rest of the dashboard) and polls their lifecycle back out of
// GET /api/fleet/commands?machine=<name>. That session gate is the only
// authorization: commands are not signed, so anything issued here runs as SYSTEM on
// the target. The hub audits every one against the operator's email.
//
// Wrapped in an IIFE: machine.js is a classic script sharing the global lexical
// scope, so top-level `const config`/`socket` here would collide with its bindings.
(function () {
    'use strict';

    if (!window.FleetApi || !FleetApi.machine) return;
    const MACHINE_NAME = FleetApi.machine;

    const agentStatusEl = document.getElementById('fleet-agent-status');
    const agentHintEl = document.getElementById('fleet-agent-hint');
    const commandSelect = document.getElementById('fleet-command');
    const paramsEl = document.getElementById('fleet-params');
    const feedbackEl = document.getElementById('fleet-feedback');
    const sendBtn = document.getElementById('fleet-send');
    const refreshBtn = document.getElementById('fleet-refresh');
    const historyBody = document.getElementById('fleet-history');
    const historyEmptyEl = document.getElementById('fleet-history-empty');

    // Mirrors fleet.py's ALL_COMMANDS. Every type is issued on the session alone --
    // commands are no longer signed, so run_script gets an ordinary typed form like
    // the rest. (It previously needed a raw JSON textarea + a pasted signature hex,
    // because the offline signature covered the exact params bytes and had to
    // round-trip unchanged.)
    const COMMAND_SPECS = [
        {
            type: 'gpupdate',
            label: 'gpupdate — force a Group Policy refresh',
            fields: []
        },
        {
            type: 'restart',
            label: 'restart — reboot the machine',
            confirm: (params) => `Reboot ${MACHINE_NAME} in ${params.delay_seconds ?? 60}s?`,
            fields: [{ name: 'delay_seconds', label: 'Delay (seconds)', type: 'number', value: 60 }]
        },
        {
            type: 'shutdown',
            label: 'shutdown — power off the machine',
            confirm: (params) => `Shut down ${MACHINE_NAME} in ${params.delay_seconds ?? 60}s?`,
            fields: [{ name: 'delay_seconds', label: 'Delay (seconds)', type: 'number', value: 60 }]
        },
        {
            type: 'rename',
            label: 'rename — change the computer name',
            confirm: (params) => `Rename ${MACHINE_NAME} to "${params.new_name}"? Takes effect on its next reboot.`,
            fields: [{ name: 'new_name', label: 'New computer name', type: 'text', required: true }]
        },
        {
            type: 'install_app',
            label: 'install_app — install via winget or MSI',
            fields: [
                { name: 'id', label: 'winget package ID', type: 'text', hint: 'e.g. Google.Chrome' },
                { name: 'msi_path', label: 'or MSI path', type: 'text', hint: 'Must be reachable by the SYSTEM account' }
            ],
            validate: (params) => (!params.id && !params.msi_path)
                ? 'Provide either a winget package ID or an MSI path.'
                : null
        },
        {
            type: 'run_script',
            label: 'run_script — run a PowerShell or cmd script',
            confirm: () => `Run this script on ${MACHINE_NAME} as SYSTEM?`,
            fields: [
                {
                    name: 'script', label: 'Script', type: 'textarea', required: true,
                    hint: 'Runs as SYSTEM, with a 600s timeout. Output appears once it finishes.'
                },
                {
                    name: 'shell', label: 'Shell', type: 'select', value: 'powershell',
                    options: [
                        { value: 'powershell', label: 'PowerShell' },
                        { value: 'cmd', label: 'cmd' }
                    ]
                }
            ]
        },
        {
            type: 'install_driver',
            label: 'install_driver — not implemented on the agent yet',
            fields: []
        },
        {
            type: 'update_bios',
            label: 'update_bios — not implemented on the agent yet',
            fields: []
        }
    ];

    const ACTIVE_STATUSES = new Set(['pending', 'claimed']);
    const STATUS_PILLS = {
        pending: ['warn', 'Pending'],
        claimed: ['warn', 'Running'],
        done: ['ok', 'Done'],
        failed: ['danger', 'Failed'],
        expired: ['muted', 'Expired']
    };

    let hasActiveCommand = false;
    let pollTimer = null;
    let openDetailId = null;
    const detailCache = new Map();

    function specFor(type) {
        return COMMAND_SPECS.find((spec) => spec.type === type);
    }

    function currentSpec() {
        return specFor(commandSelect.value);
    }

    function showFeedback(kind, message) {
        feedbackEl.hidden = false;
        feedbackEl.className = `fleet-feedback fleet-feedback--${kind}`;
        feedbackEl.textContent = message;
    }

    function clearFeedback() {
        feedbackEl.hidden = true;
        feedbackEl.textContent = '';
    }

    // ---------------- Params form ----------------
    function makeControl(field) {
        if (field.type === 'textarea') {
            const el = document.createElement('textarea');
            el.className = 'input';
            el.rows = 6;
            el.spellcheck = false;
            if (field.value !== undefined) el.value = field.value;
            return el;
        }
        if (field.type === 'select') {
            const el = document.createElement('select');
            el.className = 'select';
            for (const option of field.options) {
                const opt = document.createElement('option');
                opt.value = option.value;
                opt.textContent = option.label;
                el.appendChild(opt);
            }
            if (field.value !== undefined) el.value = field.value;
            return el;
        }
        const el = document.createElement('input');
        el.className = 'input';
        el.type = field.type;
        if (field.value !== undefined) el.value = field.value;
        return el;
    }

    function makeField(spec, field) {
        const wrap = document.createElement('label');
        wrap.className = field.type === 'textarea' ? 'fleet-field fleet-field--wide' : 'fleet-field';

        const label = document.createElement('span');
        label.className = 'fleet-field__label';
        label.textContent = field.required ? `${field.label} *` : field.label;
        wrap.appendChild(label);

        const control = makeControl(field);
        control.dataset.param = field.name;
        wrap.appendChild(control);

        if (field.hint) {
            const hint = document.createElement('span');
            hint.className = 'fleet-field__hint';
            hint.textContent = field.hint;
            wrap.appendChild(hint);
        }
        return wrap;
    }

    function renderParams() {
        const spec = currentSpec();
        paramsEl.textContent = '';
        clearFeedback();
        if (!spec) return;

        for (const field of spec.fields) {
            paramsEl.appendChild(makeField(spec, field));
        }
    }

    // Reads the typed form back into a params object. Empty optional fields are
    // omitted entirely so the agent applies its own default rather than receiving
    // an explicit empty value.
    function collectParams(spec) {
        const params = {};
        for (const field of spec.fields) {
            const input = paramsEl.querySelector(`[data-param="${field.name}"]`);
            const raw = (input?.value ?? '').trim();
            if (!raw) {
                if (field.required) throw new Error(`${field.label} is required.`);
                continue;
            }
            if (field.type === 'number') {
                const value = Number(raw);
                if (!Number.isFinite(value)) throw new Error(`${field.label} must be a number.`);
                params[field.name] = value;
            } else {
                params[field.name] = raw;
            }
        }
        const problem = spec.validate?.(params);
        if (problem) throw new Error(problem);
        return params;
    }

    // ---------------- Sending ----------------
    async function send() {
        const spec = currentSpec();
        if (!spec) return;
        clearFeedback();

        let params;
        try {
            params = collectParams(spec);
        } catch (e) {
            showFeedback('error', e.message);
            return;
        }

        if (spec.confirm && !window.confirm(spec.confirm(params))) return;

        sendBtn.disabled = true;
        try {
            const commandId = await FleetApi.issueCommand(spec.type, params);
            showFeedback('ok', `Queued ${spec.type} (${commandId}). The agent picks it up within ~10s.`);
        } catch (e) {
            showFeedback('error', e.message);
        } finally {
            sendBtn.disabled = false;
        }
    }

    // ---------------- History ----------------
    const formatTime = FleetApi.formatTime;

    function removeDetailRows() {
        historyBody.querySelectorAll('[data-detail-for]').forEach((el) => el.remove());
    }

    // Renders (or re-renders) the expanded detail under `row`. Called again after
    // every poll so an open detail survives the table being rebuilt -- and so a
    // pending command's output fills in live while you watch it.
    async function renderDetail(row) {
        const commandId = row.dataset.commandId;
        removeDetailRows();

        const detail = document.createElement('tr');
        detail.dataset.detailFor = commandId;
        const cell = document.createElement('td');
        cell.colSpan = 4;
        const pre = document.createElement('pre');
        pre.className = 'console-surface fleet-output';
        // Show the last known text while refetching, so polling doesn't flicker
        // the output back to "Loading…" under the reader.
        pre.textContent = detailCache.get(commandId) ?? 'Loading…';
        cell.appendChild(pre);
        detail.appendChild(cell);
        row.after(detail);

        let text;
        try {
            const command = await FleetApi.getCommand(commandId);
            const lines = [`params: ${JSON.stringify(command.params)}`];
            if (command.result) {
                lines.push(`success: ${command.result.success ? 'yes' : 'no'}`);
                lines.push(`completed: ${formatTime(command.result.completed_at)}`);
                lines.push('');
                lines.push(command.result.output ?? '(no output)');
            } else {
                lines.push('');
                lines.push('No result yet — the agent has not reported back.');
            }
            text = lines.join('\n');
            detailCache.set(commandId, text);
        } catch (e) {
            text = `Could not load result: ${e.message}`;
        }
        // The row may have been closed or rebuilt while the fetch was in flight.
        if (openDetailId !== commandId || !pre.isConnected) return;
        // textContent, not innerHTML: command output is untrusted agent-side text.
        pre.textContent = text;
    }

    function toggleDetail(row) {
        const commandId = row.dataset.commandId;
        if (openDetailId === commandId) {
            openDetailId = null;
            removeDetailRows();
            return;
        }
        openDetailId = commandId;
        renderDetail(row);
    }

    function renderHistory(rows) {
        historyBody.textContent = '';
        if (!rows.length) {
            historyEmptyEl.hidden = false;
            return;
        }
        historyEmptyEl.hidden = true;

        for (const row of rows) {
            const tr = document.createElement('tr');
            tr.className = 'fleet-row';
            tr.dataset.commandId = row.id;
            tr.title = 'Click to show params and result output';

            const timeCell = document.createElement('td');
            timeCell.textContent = formatTime(row.created_at);
            tr.appendChild(timeCell);

            const typeCell = document.createElement('td');
            const typeText = document.createElement('span');
            typeText.className = 'fleet-row__type';
            typeText.textContent = row.type;
            typeCell.appendChild(typeText);
            tr.appendChild(typeCell);

            const byCell = document.createElement('td');
            byCell.textContent = row.issued_by || '--';
            tr.appendChild(byCell);

            const statusCell = document.createElement('td');
            const pill = document.createElement('span');
            pill.className = 'status-pill';
            const [state, label] = STATUS_PILLS[row.status] ?? ['muted', row.status];
            setStatusPill(pill, state, label);
            statusCell.appendChild(pill);
            tr.appendChild(statusCell);

            tr.addEventListener('click', () => toggleDetail(tr));
            historyBody.appendChild(tr);
        }

        // The table was just rebuilt from scratch; restore whatever detail was open.
        if (openDetailId) {
            const openRow = historyBody.querySelector(`[data-command-id="${openDetailId}"]`);
            if (openRow) renderDetail(openRow);
            else openDetailId = null;
        }
    }

    async function refreshHistory() {
        try {
            const rows = await FleetApi.listCommands(MACHINE_NAME);
            hasActiveCommand = rows.some((row) => ACTIVE_STATUSES.has(row.status));
            renderHistory(rows);
        } catch (e) {
            historyEmptyEl.hidden = false;
            historyEmptyEl.textContent = `Could not load command history: ${e.message}`;
        }
    }

    async function refreshAgentStatus() {
        try {
            const rows = await FleetApi.agentStatus();
            const entry = rows.find((row) => row.machine === MACHINE_NAME);
            if (!entry) {
                setStatusPill(agentStatusEl, 'muted', 'No agent enrolled');
                agentHintEl.textContent =
                    'No enrolled agent for this machine. Commands can still be queued, but nothing will ' +
                    'execute them until the C# agent is installed with an enrollment secret. The Python ' +
                    'companion is telemetry-only and never runs commands.';
                return;
            }
            if (entry.status === 'online') {
                setStatusPill(agentStatusEl, 'ok', 'Agent online');
                agentHintEl.textContent = '';
            } else {
                setStatusPill(agentStatusEl, 'warn', 'Agent offline');
                agentHintEl.textContent =
                    `Last seen ${formatTime(entry.last_seen)}. Commands stay queued until it checks back in, ` +
                    'or expire at their TTL.';
            }
        } catch (e) {
            setStatusPill(agentStatusEl, 'muted', 'Status unavailable');
            agentHintEl.textContent = `Could not read fleet status: ${e.message}`;
        }
    }

    // Poll faster while something is in flight, slowly otherwise, and not at all
    // in a background tab.
    function schedulePoll() {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(tick, hasActiveCommand ? 4000 : 20000);
    }

    async function tick() {
        if (document.visibilityState === 'visible') {
            await Promise.all([refreshHistory(), refreshAgentStatus()]);
        }
        schedulePoll();
    }

    // ---------------- Init ----------------
    for (const spec of COMMAND_SPECS) {
        const option = document.createElement('option');
        option.value = spec.type;
        option.textContent = spec.label;
        commandSelect.appendChild(option);
    }

    commandSelect.addEventListener('change', renderParams);
    sendBtn.addEventListener('click', send);
    refreshBtn.addEventListener('click', () => {
        refreshHistory();
        refreshAgentStatus();
    });
    // Fires for this panel's own sends AND for anything the Terminal tab runs, so a
    // script typed there shows up here without either module knowing about the other.
    FleetApi.onCommandIssued(() => {
        hasActiveCommand = true;
        refreshHistory();
        schedulePoll();
    });
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') tick();
    });

    renderParams();
    refreshHistory();
    refreshAgentStatus();
    schedulePoll();
})();
