// Fleet command panel on the machine detail page.
//
// Issues commands via POST /api/fleet/commands (Google-session gated, same as the
// rest of the dashboard) and polls their lifecycle back out of
// GET /api/fleet/commands?machine=<name>.
//
// Wrapped in an IIFE: machine.js is a classic script sharing the global lexical
// scope, so top-level `const config`/`socket` here would collide with its bindings.
(function () {
    'use strict';

    const machineConfig = document.getElementById('machine-config');
    if (!machineConfig) return;
    const MACHINE_NAME = machineConfig.dataset.machine;

    const agentStatusEl = document.getElementById('fleet-agent-status');
    const agentHintEl = document.getElementById('fleet-agent-hint');
    const commandSelect = document.getElementById('fleet-command');
    const paramsEl = document.getElementById('fleet-params');
    const feedbackEl = document.getElementById('fleet-feedback');
    const sendBtn = document.getElementById('fleet-send');
    const refreshBtn = document.getElementById('fleet-refresh');
    const historyBody = document.getElementById('fleet-history');
    const historyEmptyEl = document.getElementById('fleet-history-empty');

    // Mirrors fleet.py's LOW_RISK_COMMANDS / HIGH_RISK_COMMANDS. Low-risk types get a
    // typed form; high-risk ones get a raw params textarea + signature field, because
    // the offline signature covers the exact params object and must round-trip
    // unchanged (see fleet.canonical_command_bytes).
    const COMMAND_SPECS = [
        {
            type: 'gpupdate',
            label: 'gpupdate — force a Group Policy refresh',
            risk: 'low',
            fields: []
        },
        {
            type: 'restart',
            label: 'restart — reboot the machine',
            risk: 'low',
            confirm: (params) => `Reboot ${MACHINE_NAME} in ${params.delay_seconds ?? 60}s?`,
            fields: [{ name: 'delay_seconds', label: 'Delay (seconds)', type: 'number', value: 60 }]
        },
        {
            type: 'shutdown',
            label: 'shutdown — power off the machine',
            risk: 'low',
            confirm: (params) => `Shut down ${MACHINE_NAME} in ${params.delay_seconds ?? 60}s?`,
            fields: [{ name: 'delay_seconds', label: 'Delay (seconds)', type: 'number', value: 60 }]
        },
        {
            type: 'rename',
            label: 'rename — change the computer name',
            risk: 'low',
            confirm: (params) => `Rename ${MACHINE_NAME} to "${params.new_name}"? Takes effect on its next reboot.`,
            fields: [{ name: 'new_name', label: 'New computer name', type: 'text', required: true }]
        },
        {
            type: 'install_app',
            label: 'install_app — install via winget or MSI',
            risk: 'low',
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
            label: 'run_script — run a PowerShell script',
            risk: 'high',
            defaultParams: { script: '' }
        },
        {
            type: 'install_driver',
            label: 'install_driver — not implemented on the agent yet',
            risk: 'high',
            defaultParams: {}
        },
        {
            type: 'update_bios',
            label: 'update_bios — not implemented on the agent yet',
            risk: 'high',
            defaultParams: {}
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

    async function getJson(url) {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`${url} returned ${response.status}`);
        return response.json();
    }

    // ---------------- Params form ----------------
    function makeField(spec, field) {
        const wrap = document.createElement('label');
        wrap.className = 'fleet-field';

        const label = document.createElement('span');
        label.className = 'fleet-field__label';
        label.textContent = field.required ? `${field.label} *` : field.label;
        wrap.appendChild(label);

        const input = document.createElement('input');
        input.className = 'input';
        input.type = field.type;
        input.dataset.param = field.name;
        if (field.value !== undefined) input.value = field.value;
        wrap.appendChild(input);

        if (field.hint) {
            const hint = document.createElement('span');
            hint.className = 'fleet-field__hint';
            hint.textContent = field.hint;
            wrap.appendChild(hint);
        }
        return wrap;
    }

    function makeHighRiskFields(spec) {
        const fragment = document.createDocumentFragment();

        const paramsWrap = document.createElement('label');
        paramsWrap.className = 'fleet-field fleet-field--wide';
        const paramsLabel = document.createElement('span');
        paramsLabel.className = 'fleet-field__label';
        paramsLabel.textContent = 'Params (JSON) *';
        paramsWrap.appendChild(paramsLabel);
        const paramsInput = document.createElement('textarea');
        paramsInput.className = 'input';
        paramsInput.id = 'fleet-params-json';
        paramsInput.value = JSON.stringify(spec.defaultParams ?? {}, null, 2);
        paramsWrap.appendChild(paramsInput);
        const paramsHint = document.createElement('span');
        paramsHint.className = 'fleet-field__hint';
        paramsHint.textContent = 'Must match the params you signed, exactly.';
        paramsWrap.appendChild(paramsHint);
        fragment.appendChild(paramsWrap);

        const sigWrap = document.createElement('label');
        sigWrap.className = 'fleet-field fleet-field--wide';
        const sigLabel = document.createElement('span');
        sigLabel.className = 'fleet-field__label';
        sigLabel.textContent = 'Offline signature (hex) *';
        sigWrap.appendChild(sigLabel);
        const sigInput = document.createElement('input');
        sigInput.className = 'input';
        sigInput.type = 'text';
        sigInput.id = 'fleet-signature';
        sigInput.placeholder = 'Paste the signature printed by sign_release.py';
        sigWrap.appendChild(sigInput);
        fragment.appendChild(sigWrap);

        const signHint = document.createElement('div');
        signHint.className = 'fleet-sign-hint';
        signHint.id = 'fleet-sign-hint';
        fragment.appendChild(signHint);

        return { fragment, paramsInput, signHint };
    }

    function updateSignHint(spec, paramsInput, signHint) {
        let compact;
        try {
            compact = JSON.stringify(JSON.parse(paramsInput.value || '{}'));
        } catch (e) {
            signHint.textContent = 'Sign this command offline once the params below are valid JSON.';
            return;
        }
        signHint.textContent =
            `python sign_release.py --sign-command --type ${spec.type} ` +
            `--machine ${MACHINE_NAME} --params '${compact}'`;
    }

    function renderParams() {
        const spec = currentSpec();
        paramsEl.textContent = '';
        clearFeedback();
        if (!spec) return;

        if (spec.risk === 'high') {
            const { fragment, paramsInput, signHint } = makeHighRiskFields(spec);
            paramsEl.appendChild(fragment);
            updateSignHint(spec, paramsInput, signHint);
            paramsInput.addEventListener('input', () => updateSignHint(spec, paramsInput, signHint));
            return;
        }

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
        let signature = null;

        try {
            if (spec.risk === 'high') {
                const rawParams = document.getElementById('fleet-params-json').value.trim() || '{}';
                try {
                    params = JSON.parse(rawParams);
                } catch (e) {
                    throw new Error(`Params must be valid JSON: ${e.message}`);
                }
                if (params === null || typeof params !== 'object' || Array.isArray(params)) {
                    throw new Error('Params must be a JSON object.');
                }
                signature = document.getElementById('fleet-signature').value.trim();
                if (!signature) {
                    throw new Error(`${spec.type} is high-risk and requires an offline signature.`);
                }
            } else {
                params = collectParams(spec);
            }
        } catch (e) {
            showFeedback('error', e.message);
            return;
        }

        if (spec.confirm && !window.confirm(spec.confirm(params))) return;

        const body = { machine: MACHINE_NAME, type: spec.type, params };
        if (signature) body.signature = signature;

        sendBtn.disabled = true;
        try {
            const response = await fetch('/api/fleet/commands', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                showFeedback('error', data.error || `Hub returned ${response.status}.`);
                return;
            }
            showFeedback('ok', `Queued ${spec.type} (${data.command_id}). The agent picks it up within ~10s.`);
            hasActiveCommand = true;
            await refreshHistory();
            schedulePoll();
        } catch (e) {
            showFeedback('error', `Request failed: ${e.message}`);
        } finally {
            sendBtn.disabled = false;
        }
    }

    // ---------------- History ----------------
    function formatTime(epochSeconds) {
        const value = Number(epochSeconds);
        if (!Number.isFinite(value)) return '--';
        return new Date(value * 1000).toLocaleString();
    }

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
        pre.className = 'fleet-output';
        // Show the last known text while refetching, so polling doesn't flicker
        // the output back to "Loading…" under the reader.
        pre.textContent = detailCache.get(commandId) ?? 'Loading…';
        cell.appendChild(pre);
        detail.appendChild(cell);
        row.after(detail);

        let text;
        try {
            const command = await getJson(`/api/fleet/commands/${encodeURIComponent(commandId)}`);
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
            if (row.requires_signature) {
                const badge = document.createElement('span');
                badge.className = 'badge fleet-badge-risk';
                badge.textContent = 'signed';
                typeCell.appendChild(badge);
            }
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
            const rows = await getJson(`/api/fleet/commands?machine=${encodeURIComponent(MACHINE_NAME)}`);
            hasActiveCommand = rows.some((row) => ACTIVE_STATUSES.has(row.status));
            renderHistory(rows);
        } catch (e) {
            historyEmptyEl.hidden = false;
            historyEmptyEl.textContent = `Could not load command history: ${e.message}`;
        }
    }

    async function refreshAgentStatus() {
        try {
            const rows = await getJson('/api/fleet/status');
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
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') tick();
    });

    renderParams();
    refreshHistory();
    refreshAgentStatus();
    schedulePoll();
})();
