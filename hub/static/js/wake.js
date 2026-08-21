// Machine page, Network tab: this PC's adapters, and waking it (roadmap #10).
//
// **Nothing here ever claims a machine was woken because a packet went out.** WoL is
// fire-and-forget: nothing acknowledges a magic packet, no error comes back for a MAC that
// does not exist, and a machine that was already awake looks identical to one that just
// woke. So `sent` is rendered as an attempt in progress and only the machine's own check-in
// turns it green -- which is the hub's decision, made against the moment the packet left,
// and mirrored here rather than re-derived.
//
// **"No relay available" is rendered as an ANSWER, not an error.** Every machine on a subnet
// being asleep is the expected state at 3am, and "no awake machine on 10.4.7.0/24 to relay
// through" is a diagnosis somebody can act on. Styling it red would send an operator hunting
// for a fault that is not there.
//
// **The diagnosis is the other half of the feature.** Most of a Wake-on-LAN rollout is
// preconditions -- the firmware setting, the NIC's own wake flag, Fast Startup, Wi-Fi -- so a
// machine that can never be woken says so from its inventory instead of being offered a
// button that silently does nothing. The remedy sits next to it, because a console that can
// only name the four reasons sends somebody to forty desks.
//
// Same two rules as the rest of the console: built with textContent/createElement, never
// innerHTML -- adapter names and driver descriptions are arbitrary text arriving from a
// remote machine -- and every string comes from the catalog.

(function () {
    'use strict';

    const PANEL_ID = 'tool-network';
    const pane = document.getElementById(PANEL_ID);
    if (!pane) return;

    const t = window.t;
    // The machine this panel is currently showing. A call, not a constant: on the Tools
    // page the operator picks a different PC without the page reloading, and every URL
    // below has to be built against the machine chosen now rather than the one that
    // happened to be current when this file was parsed.
    const currentMachine = () => window.MachineContext.current();

    const statusPill = document.getElementById('network-status');
    const statusText = document.getElementById('network-status-text');
    const wakeState = document.getElementById('network-wake-state');
    const diagnosisBox = document.getElementById('network-diagnosis');
    const body = document.getElementById('network-body');
    const history = document.getElementById('network-history');
    const wakeBtn = document.getElementById('network-wake');
    const prepareBtn = document.getElementById('network-prepare');

    let data = null;
    // Set while a wake is in flight so the poll stops on its own. A wake resolves in seconds
    // when a peer is awake and can sit pending for its whole TTL when none is -- so this
    // polls rather than assuming, and gives up rather than polling all weekend.
    let pollTimer = null;
    let pollsLeft = 0;
    const POLL_INTERVAL_MS = 5000;
    // Long enough to cover a cold boot plus the hub's own confirm window, and no longer.
    const MAX_POLLS = 90;

    async function api(path, options) {
        const resp = await fetch(path, options);
        let payload = null;
        try { payload = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
        return payload;
    }

    function post(path, bodyObj) {
        return api(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(bodyObj || {}),
        });
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

    // Wire values shown to an operator need display names, and every map is spelled out with
    // one literal t() per value so the key scan in tests/test_i18n.py can see them.
    const STATUS_LABELS = {
        pending: () => t('machine.network.state.pending'),
        relaying: () => t('machine.network.state.relaying'),
        sent: () => t('machine.network.state.sent'),
        awake: () => t('machine.network.state.awake'),
        already_awake: () => t('machine.network.state.already_awake'),
        no_relay: () => t('machine.network.state.no_relay'),
        no_answer: () => t('machine.network.state.no_answer'),
        unwakeable: () => t('machine.network.state.unwakeable'),
        cancelled: () => t('machine.network.state.cancelled'),
    };
    // `no_relay` is deliberately 'muted', not 'danger': it is the expected state of a subnet
    // at 3am, and colouring it as a failure is what sends an operator looking for a fault.
    const STATUS_TONES = {
        pending: 'muted', relaying: 'muted', sent: 'muted', awake: 'ok',
        already_awake: 'ok', no_relay: 'muted', no_answer: 'warn',
        unwakeable: 'warn', cancelled: 'muted',
    };
    const DIAGNOSIS_LABELS = {
        no_report: () => t('machine.network.problem.no_report'),
        no_wired_nic: () => t('machine.network.problem.no_wired_nic'),
        wireless_only: () => t('machine.network.problem.wireless_only'),
        no_address: () => t('machine.network.problem.no_address'),
        wake_disabled: () => t('machine.network.problem.wake_disabled'),
        fast_startup: () => t('machine.network.problem.fast_startup'),
    };
    const KIND_LABELS = {
        wired: () => t('machine.network.kind.wired'),
        wireless: () => t('machine.network.kind.wireless'),
        other: () => t('machine.network.kind.other'),
    };
    const DELIVERY_LABELS = {
        relay: () => t('machine.network.delivery.relay'),
        hub: () => t('machine.network.delivery.hub'),
    };

    function labelFor(map, value) {
        const fn = map[value];
        // An unrecognised value shows itself: a newer hub reporting a state this console has
        // no word for reads better as its own name than as a missing catalog key.
        return fn ? fn() : value;
    }

    function isOpen(request) {
        return !!request && (request.status === 'pending' || request.status === 'relaying'
                             || request.status === 'sent');
    }

    // Loaded lazily on first reveal and re-loaded on a machine switch, like the Backup and
    // Firmware tabs: most machine pages are opened to look at a temperature graph.
    // ToolPanels owns that sequencing; see tool-panels.js.
    ToolPanels.register('network', {
        panelId: PANEL_ID,
        load,
        teardown: reset,
        requires: (machine) => !!machine
    });

    // stopPolling() is the load-bearing half: a wake in flight polls once a second, and a
    // poll loop left running against the machine you just navigated away from would keep
    // writing its result into the panel now showing a different PC.
    function reset() {
        stopPolling();
        data = null;
        pollsLeft = 0;
    }

    async function load() {
        setStatus('muted', t('common.loading'));
        try {
            data = await api(`/api/wake/machines/${encodeURIComponent(currentMachine())}`);
        } catch (e) {
            data = null;
            setStatus('danger', t('machine.network.load_failed'));
            body.replaceChildren(el('p', 'setting__error', e.message));
            return;
        }
        // Opening the tab onto a wake somebody else asked for starts the watch too: it is
        // the same fact whoever requested it, and a "sent" that only updates on a manual
        // reload is how an operator concludes the feature is broken.
        if (isOpen(data.request) && pollTimer === null) startPolling();
        render();
    }

    async function refresh() {
        try {
            data = await api(`/api/wake/machines/${encodeURIComponent(currentMachine())}`);
            render();
        } catch (e) { /* a failed poll is not worth tearing the tab down over */ }
    }

    function setStatus(tone, text) {
        statusPill.className = `status-pill status-pill--${tone}`;
        statusText.textContent = text;
        statusPill.replaceChildren(el('span', 'status-pill__dot'), statusText);
    }

    // ---------------------------------------------------------------- polling
    function startPolling() {
        stopPolling();
        pollsLeft = MAX_POLLS;
        pollTimer = setInterval(async () => {
            if (pollsLeft-- <= 0) { stopPolling(); return; }
            await refresh();
            if (!isOpen(data && data.request)) stopPolling();
        }, POLL_INTERVAL_MS);
    }

    function stopPolling() {
        if (pollTimer !== null) clearInterval(pollTimer);
        pollTimer = null;
    }

    // ---------------------------------------------------------------- render
    function render() {
        if (!data) return;
        renderStatus();
        renderWakeState();
        renderDiagnosis();
        renderAdapters();
        renderHistory();
        syncButtons();
    }

    function renderStatus() {
        if (data.online) { setStatus('ok', t('machine.network.status.online')); return; }
        if (data.reported_at === null) {
            setStatus('muted', t('machine.network.status.never_reported'));
            return;
        }
        if (!data.wakeable) { setStatus('warn', t('machine.network.status.unwakeable')); return; }
        setStatus('muted', t('machine.network.status.asleep'));
    }

    function syncButtons() {
        if (wakeBtn) {
            // Disabled while one is in flight and when there is nothing to send to. A button
            // that stays live and answers "already requested" teaches an operator that the
            // console does not know what it is doing.
            wakeBtn.disabled = isOpen(data.request) || data.online || !data.wakeable;
            wakeBtn.textContent = data.online
                ? t('machine.network.wake_online')
                : t('machine.network.wake');
        }
        if (prepareBtn) {
            // The remedy runs ON the machine, so it needs the machine awake -- which is the
            // ordinary case: preconditions get fixed during the day and pay off at 3am.
            prepareBtn.disabled = !data.online;
            prepareBtn.title = data.online
                ? t('machine.network.prepare_title')
                : t('machine.network.prepare_offline');
        }
    }

    function renderWakeState() {
        wakeState.replaceChildren();
        const request = data.request || (data.history || [])[0];
        if (!request) return;

        const card = el('div', `notice notice--${STATUS_TONES[request.status] || 'muted'}`);
        card.appendChild(el('div', 'section-title', labelFor(STATUS_LABELS, request.status)));

        const lines = [];
        if (request.status === 'pending') {
            lines.push(request.subnet
                ? t('machine.network.pending_on', { subnet: request.subnet })
                : t('machine.network.pending_any'));
        } else if (request.status === 'relaying' && request.relay) {
            lines.push(t('machine.network.relaying_via', { relay: request.relay }));
        } else if (request.status === 'sent') {
            lines.push(request.delivery === 'hub'
                ? t('machine.network.sent_by_hub')
                : t('machine.network.sent_via', { relay: request.relay || '—' }));
            // Said plainly, because it is the honest limit of the mechanism: the packet is
            // gone and nothing will ever acknowledge it. What resolves this row is the
            // machine itself checking in.
            lines.push(t('machine.network.sent_note'));
        } else if (request.error) {
            lines.push(request.error);
        }
        if (request.delivery && request.status !== 'pending' && request.status !== 'relaying') {
            lines.push(t('machine.network.delivered_by',
                         { method: labelFor(DELIVERY_LABELS, request.delivery) }));
        }
        lines.forEach((line) => card.appendChild(el('p', 'stat-card__meta', line)));

        if (request.status === 'pending' && wakeBtn) {
            const cancel = el('button', 'btn btn--ghost', t('machine.network.cancel'));
            cancel.type = 'button';
            cancel.addEventListener('click', () => cancelWake(request.id, cancel));
            card.appendChild(cancel);
        }
        wakeState.appendChild(card);
    }

    function renderDiagnosis() {
        diagnosisBox.replaceChildren();
        const problems = data.diagnosis || [];
        if (!problems.length) return;

        const card = el('div', 'notice notice--warn');
        card.appendChild(el('div', 'section-title', t('machine.network.problems_title')));
        const list = el('ul', 'stat-card__meta');
        problems.forEach((code) => {
            list.appendChild(el('li', null, labelFor(DIAGNOSIS_LABELS, code)));
        });
        card.appendChild(list);
        diagnosisBox.appendChild(card);
    }

    function renderAdapters() {
        body.replaceChildren();
        const nics = data.nics || [];
        if (!nics.length) {
            // Distinct from "has no wired adapter": one is an agent too old for this feature
            // (or one that has not checked in yet) and the other is a fact about hardware.
            body.appendChild(el('p', 'stat-card__meta',
                                data.reported_at === null
                                    ? t('machine.network.never_reported_help')
                                    : t('machine.network.no_adapters')));
            return;
        }

        const table = el('table', 'data-table');
        const head = el('tr');
        [t('machine.network.col.adapter'), t('machine.network.col.kind'),
         t('machine.network.col.mac'), t('machine.network.col.address'),
         t('machine.network.col.link'), t('machine.network.col.wake')]
            .forEach((label) => head.appendChild(el('th', null, label)));
        table.appendChild(el('thead')).appendChild(head);

        const tbody = el('tbody');
        nics.forEach((nic) => {
            const row = el('tr');
            const nameCell = el('td');
            nameCell.appendChild(el('div', null, nic.name || '—'));
            if (nic.description) {
                nameCell.appendChild(el('div', 'stat-card__meta', nic.description));
            }
            row.appendChild(nameCell);
            row.appendChild(el('td', null, labelFor(KIND_LABELS, nic.kind)));
            row.appendChild(el('td', null, nic.mac));

            const addressCell = el('td');
            if (nic.ipv4) {
                addressCell.appendChild(el('div', null, nic.ipv4));
                // The subnet is shown because it is the thing relay selection joins on --
                // when a wake reports "no awake machine on 10.4.7.0/24", this is where an
                // operator confirms that is the segment they expected.
                addressCell.appendChild(el('div', 'stat-card__meta', nic.subnet));
            } else {
                addressCell.textContent = '—';
            }
            row.appendChild(addressCell);
            row.appendChild(el('td', null, nic.link_up
                ? t('machine.network.link_up')
                : t('machine.network.link_down')));
            row.appendChild(el('td', null, wakeCell(nic)));
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        body.appendChild(table);
    }

    // Three answers, never two: an adapter whose driver does not publish the setting is
    // UNKNOWN, and rendering that as "no" would have somebody fixing a working machine.
    function wakeCell(nic) {
        if (nic.kind !== 'wired') return t('machine.network.wake_na');
        if (nic.wake_enabled === null || nic.wake_enabled === undefined) {
            return t('machine.network.wake_unknown');
        }
        return nic.wake_enabled ? t('machine.network.wake_on') : t('machine.network.wake_off');
    }

    function renderHistory() {
        history.replaceChildren();
        const rows = data.history || [];
        if (!rows.length) {
            history.appendChild(el('p', 'stat-card__meta', t('machine.network.no_history')));
            return;
        }

        const table = el('table', 'data-table');
        const head = el('tr');
        [t('machine.network.col.when'), t('machine.network.col.outcome'),
         t('machine.network.col.relay'), t('machine.network.col.requested_by'),
         t('machine.network.col.detail')]
            .forEach((label) => head.appendChild(el('th', null, label)));
        table.appendChild(el('thead')).appendChild(head);

        const tbody = el('tbody');
        rows.forEach((request) => {
            const row = el('tr');
            row.appendChild(el('td', null, fmtTime(request.requested_at)));
            row.appendChild(el('td', null, labelFor(STATUS_LABELS, request.status)));
            row.appendChild(el('td', null, request.relay
                || (request.delivery === 'hub' ? t('machine.network.delivery.hub') : '—')));
            row.appendChild(el('td', null, request.requested_by || '—'));
            row.appendChild(el('td', null, request.error || request.subnet || '—'));
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        history.appendChild(table);
    }

    // ---------------------------------------------------------------- actions
    if (wakeBtn) {
        wakeBtn.addEventListener('click', async () => {
            wakeBtn.disabled = true;
            try {
                data = await post(`/api/wake/machines/${encodeURIComponent(currentMachine())}`, {});
                render();
                if (isOpen(data.request)) startPolling();
            } catch (e) {
                wakeState.replaceChildren(el('p', 'setting__error', e.message));
                wakeBtn.disabled = false;
            }
        });
    }

    if (prepareBtn) {
        prepareBtn.addEventListener('click', async () => {
            prepareBtn.disabled = true;
            const original = prepareBtn.textContent;
            prepareBtn.textContent = t('machine.network.preparing');
            try {
                await post(`/api/wake/machines/${encodeURIComponent(currentMachine())}/prepare`, {});
                // The agent re-reads and re-reports its own wake flags when it is done, so
                // the answer arrives on a heartbeat rather than in this response. Polling a
                // few times is what turns "queued" into a visibly updated diagnosis.
                startPolling();
            } catch (e) {
                diagnosisBox.replaceChildren(el('p', 'setting__error', e.message));
            } finally {
                prepareBtn.textContent = original;
                prepareBtn.disabled = false;
            }
        });
    }

    async function cancelWake(requestId, button) {
        button.disabled = true;
        try {
            data = await post(`/api/wake/requests/${encodeURIComponent(requestId)}/cancel`, {});
            stopPolling();
            render();
        } catch (e) {
            // The 409 case is the interesting one: the packet is already gone, and saying so
            // is more useful than a generic failure.
            wakeState.appendChild(el('p', 'setting__error', e.message));
            button.disabled = false;
        }
    }

})();
