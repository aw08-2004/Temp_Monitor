// Machine page, Network tab: what else is on the subnet this PC is sitting on (roadmap #18).
//
// **The subnet is picked from a list, never typed.** That is not a UI preference, it is the
// feature's safety property made visible: the options are the subnets this machine has
// already reported being on, so there is no way to aim a sweep at somebody else's network
// from here. The hub refuses an off-segment range anyway (discovery.request_scan), and this
// is what stops an operator discovering that by being refused.
//
// **A host with no match is 'unmanaged', and the console never calls it rogue.** Most of
// what lands in that column is a printer, an access point or somebody's phone on the guest
// VLAN. A console that labels the accounting department's label printer a rogue device
// teaches people to scroll past the column, which is the one outcome that makes this
// feature worthless.
//
// **Nothing here offers to DO anything about a discovered host**, and the absence is
// deliberate rather than unfinished -- see discovery.py. The classification is a MAC match,
// and a MAC is a claim a device makes about itself.
//
// **There is no agent-version gate here**, unlike the Files and Processes cards. An agent
// too old for this executor fails the command with "unknown command type: network_sweep",
// which lands in the scan row and is rendered below in the machine's own words. A version
// constant would have to name a release that has not been cut yet, and this way the operator
// is told what is actually wrong rather than shown nothing at all.
//
// Same two rules as the rest of the console: built with textContent/createElement, never
// innerHTML -- a hostname is arbitrary text that arrived from an unmanaged device on the
// wire, which makes it the least trustworthy string in the product -- and every word comes
// from the catalog.

(function () {
    'use strict';

    const PANEL_ID = 'tool-network';
    const pane = document.getElementById(PANEL_ID);
    if (!pane) return;

    const t = window.t;
    const currentMachine = () => window.MachineContext.current();

    const statusPill = document.getElementById('discovery-status');
    const statusText = document.getElementById('discovery-status-text');
    const stateBox = document.getElementById('discovery-state');
    const body = document.getElementById('discovery-body');
    const subnetPicker = document.getElementById('discovery-subnet');
    const sweepBtn = document.getElementById('discovery-sweep');
    if (!statusPill) return;

    let data = null;
    let pollTimer = null;
    let pollsLeft = 0;
    let requestGeneration = 0;
    let refreshInFlight = false;
    const POLL_INTERVAL_MS = 4000;
    // A /24 at 32 probes in flight is under a minute; a /22 is nearer four. This covers the
    // slow case with room to spare, and gives up rather than polling all afternoon.
    const MAX_POLLS = 120;

    const CLASS_LABELS = {
        relay: () => t('machine.discovery.class.relay'),
        managed: () => t('machine.discovery.class.managed'),
        unmanaged: () => t('machine.discovery.class.unmanaged'),
    };
    const STATUS_LABELS = {
        scanning: () => t('machine.discovery.state.scanning'),
        done: () => t('machine.discovery.state.done'),
        failed: () => t('machine.discovery.state.failed'),
    };

    function labelFor(map, value) {
        const fn = map[value];
        // An unrecognised value shows itself: a newer hub reporting a class this console has
        // no word for reads better as its own name than as a missing catalog key.
        return fn ? fn() : value;
    }

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

    function isOpen(scan) {
        return !!scan && scan.status === 'scanning';
    }

    ToolPanels.register('discovery', {
        panelId: PANEL_ID,
        load,
        teardown: reset,
        requires: (machine) => !!machine,
    });

    function reset() {
        stopPolling();
        data = null;
        pollsLeft = 0;
    }

    async function load() {
        stopPolling();
        const generation = requestGeneration;
        const machine = currentMachine();
        setStatus('muted', t('common.loading'));
        try {
            const loaded = await loadDiscovery(machine);
            if (!isCurrent(generation, machine)) return;
            data = loaded;
        } catch (e) {
            if (!isCurrent(generation, machine)) return;
            data = null;
            setStatus('danger', t('machine.discovery.load_failed'));
            body.replaceChildren(el('p', 'setting__error', e.message));
            return;
        }
        // Opening the tab onto a sweep somebody else started starts the watch too: it is the
        // same fact whoever asked for it.
        if (isOpen(data.scan) && pollTimer === null) startPolling();
        render();
    }

    function isCurrent(generation, machine) {
        return generation === requestGeneration && machine === currentMachine();
    }

    async function loadDiscovery(machine) {
        const payload = await api(`/api/discovery/machines/${encodeURIComponent(machine)}`);
        const scanId = payload && payload.scan && payload.scan.id;
        if (!scanId) return payload;
        // The machine endpoint deliberately carries a compact current-scan row. Always
        // replace it with the scan resource before rendering so hosts and counts agree.
        const scan = await api(`/api/discovery/scans/${encodeURIComponent(scanId)}`);
        return { ...payload, scan };
    }

    async function refresh(generation, machine) {
        if (refreshInFlight) return false;
        refreshInFlight = true;
        try {
            const loaded = await loadDiscovery(machine);
            if (!isCurrent(generation, machine)) return false;
            data = loaded;
            render();
            return true;
        } catch (e) { /* a failed poll is not worth tearing the card down over */ }
        finally { refreshInFlight = false; }
        return false;
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
        const generation = requestGeneration;
        const machine = currentMachine();
        pollTimer = setInterval(async () => {
            if (!isCurrent(generation, machine)) return;
            if (pollsLeft-- <= 0) { stopPolling(); return; }
            const refreshed = await refresh(generation, machine);
            if (refreshed && isCurrent(generation, machine) && !isOpen(data && data.scan)) {
                stopPolling();
            }
        }, POLL_INTERVAL_MS);
    }

    function stopPolling() {
        // A timer callback may already be awaiting fetch(). Bump the generation before it
        // resumes so a closed panel or a different machine never receives that old result.
        requestGeneration += 1;
        if (pollTimer !== null) clearInterval(pollTimer);
        pollTimer = null;
    }

    // ---------------------------------------------------------------- render
    function render() {
        if (!data) return;
        renderStatus();
        renderSubnets();
        renderState();
        renderHosts();
        syncButtons();
    }

    function renderStatus() {
        const scan = data.scan;
        if (isOpen(scan)) { setStatus('muted', t('machine.discovery.status.sweeping')); return; }
        if (!(data.subnets || []).length) {
            setStatus('muted', t('machine.discovery.status.no_subnets'));
            return;
        }
        if (!scan) { setStatus('muted', t('machine.discovery.status.never')); return; }
        if (scan.status === 'failed') {
            setStatus('warn', t('machine.discovery.status.failed'));
            return;
        }
        const unmanaged = (scan.counts && scan.counts.unmanaged) || 0;
        // 'warn' rather than 'danger' for a non-zero count, on the same argument the card's
        // docstring makes: something unrecognised on the wire is a thing to look at, not a
        // fault, and most of the time it is a printer.
        setStatus(unmanaged ? 'warn' : 'ok', unmanaged
            ? t('machine.discovery.status.unmanaged', { count: unmanaged })
            : t('machine.discovery.status.clean'));
    }

    function renderSubnets() {
        if (!subnetPicker) return;
        const subnets = data.subnets || [];
        const previous = subnetPicker.value;
        subnetPicker.replaceChildren();
        subnets.forEach((subnet) => {
            const option = el('option', null, subnet);
            option.value = subnet;
            subnetPicker.appendChild(option);
        });
        // Keep the operator's choice across a poll, so the picker does not snap back to the
        // first subnet every four seconds while a sweep runs.
        if (subnets.indexOf(previous) !== -1) subnetPicker.value = previous;
        else if (data.scan && subnets.indexOf(data.scan.subnet) !== -1) {
            subnetPicker.value = data.scan.subnet;
        }
    }

    function syncButtons() {
        if (!sweepBtn) return;
        const subnets = data.subnets || [];
        sweepBtn.disabled = isOpen(data.scan) || !data.online || !subnets.length;
        sweepBtn.title = data.online
            ? t('machine.discovery.sweep_title')
            : t('machine.discovery.sweep_offline');
    }

    function renderState() {
        stateBox.replaceChildren();
        const scan = data.scan;
        if (!scan) return;

        if (isOpen(scan)) {
            const card = el('div', 'notice notice--muted');
            card.appendChild(el('div', 'section-title', labelFor(STATUS_LABELS, scan.status)));
            card.appendChild(el('p', 'stat-card__meta',
                                t('machine.discovery.sweeping_on', { subnet: scan.subnet })));
            stateBox.appendChild(card);
            return;
        }
        if (scan.status === 'failed') {
            const card = el('div', 'notice notice--warn');
            card.appendChild(el('div', 'section-title', labelFor(STATUS_LABELS, scan.status)));
            // The agent's own sentence, verbatim. For an agent too old to have the executor
            // that reads "unknown command type: network_sweep", which is the most useful
            // thing the console could possibly say and not a string it has to own.
            if (scan.error) card.appendChild(el('p', 'stat-card__meta', scan.error));
            stateBox.appendChild(card);
        }
    }

    function renderHosts() {
        body.replaceChildren();
        const scan = data.scan;
        if (!scan || scan.status !== 'done') {
            if (!(data.subnets || []).length) {
                body.appendChild(el('p', 'stat-card__meta',
                                    t('machine.discovery.no_subnets_help')));
            }
            return;
        }

        const hosts = scan.hosts || [];
        const meta = el('p', 'stat-card__meta', t('machine.discovery.swept', {
            subnet: scan.subnet,
            probed: scan.probed || 0,
            when: fmtTime(scan.finished_at || scan.requested_at),
        }));
        body.appendChild(meta);
        if (scan.truncated) {
            body.appendChild(el('p', 'stat-card__meta', t('machine.discovery.truncated')));
        }
        if (!hosts.length) {
            // A real answer on a small VLAN, and phrased as one: a sweep that found nothing
            // and a sweep that failed are different facts, and only the second is a problem.
            body.appendChild(el('p', 'stat-card__meta', t('machine.discovery.nothing_found')));
            return;
        }

        const table = el('table', 'data-table');
        const head = el('tr');
        [t('machine.discovery.col.address'), t('machine.discovery.col.mac'),
         t('machine.discovery.col.name'), t('machine.discovery.col.known')]
            .forEach((label) => head.appendChild(el('th', null, label)));
        table.appendChild(el('thead')).appendChild(head);

        const tbody = el('tbody');
        hosts.forEach((host) => {
            const row = el('tr');
            row.appendChild(el('td', null, host.ip));
            row.appendChild(el('td', null, host.mac || '—'));
            row.appendChild(el('td', null, host.hostname || '—'));
            const knownCell = el('td');
            knownCell.appendChild(el('div', null, labelFor(CLASS_LABELS, host.classification)));
            // The machine name under the verdict, because "managed" on its own invites the
            // next question and the hub already knows the answer.
            if (host.machine) {
                knownCell.appendChild(el('div', 'stat-card__meta', host.machine));
            }
            row.appendChild(knownCell);
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        body.appendChild(table);
    }

    // ---------------------------------------------------------------- actions
    if (sweepBtn) {
        sweepBtn.addEventListener('click', async () => {
            sweepBtn.disabled = true;
            const subnet = subnetPicker ? subnetPicker.value : '';
            const machine = currentMachine();
            stopPolling();
            const generation = requestGeneration;
            try {
                const payload = await post(
                    `/api/discovery/machines/${encodeURIComponent(machine)}/scan`,
                    { subnet });
                const scanId = payload && payload.scan && payload.scan.id;
                const scan = scanId
                    ? await api(`/api/discovery/scans/${encodeURIComponent(scanId)}`)
                    : null;
                if (!isCurrent(generation, machine)) return;
                data = scan ? { ...payload, scan } : payload;
                render();
                if (isOpen(data.scan)) startPolling();
            } catch (e) {
                if (!isCurrent(generation, machine)) return;
                stateBox.replaceChildren(el('p', 'setting__error', e.message));
                sweepBtn.disabled = false;
            }
        });
    }

})();
