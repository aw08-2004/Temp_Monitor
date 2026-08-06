// The Dashboard is a live temperature + online-status view and deliberately does NOT flag
// high temperatures. A hot machine is evaluated server-side from a rolling AVERAGE (a momentary
// spike is not an alert) and surfaced in the Alerts tab, so a red card here -- based on a
// single instantaneous reading -- would both contradict the average and duplicate the
// alert. See app.evaluate_high_temp_once and the Alerts tab.
const socket = connectSocketWithStatus();
const machineCards = document.getElementById('machine-cards');
const emptyStateEl = document.getElementById('empty-state');

// machine -> whether it has a fleet enrollment, from the last /api/machines poll.
//
// Held here because the two things that update a card disagree about what they know: the
// 30-second poll carries the whole row, while the live `new_temp` socket event carries a
// temperature and nothing else -- and it fires far more often. Without this, every socket
// update would repaint the pill as a plain "Online" and the enrollment note would flicker
// out between polls. A machine not in the map yet (its first temp arrived before the first
// poll) is left unqualified rather than guessed at.
const enrollment = new Map();

function formatMachineInfo(info) {
    if (!info) return '';
    const parts = [];
    if (info.model) parts.push(info.model);
    if (info.serial_number) parts.push(t('common.serial_prefix', { value: info.serial_number }));
    if (info.asset_tag) parts.push(t('common.asset_prefix', { value: info.asset_tag }));
    return parts.join(' • ');
}

function goToMachine(machine) {
    window.location.href = '/machine/' + encodeURIComponent(machine);
}

function updateMachineCard(machine, temp, uptimeSeconds, info) {
    let card = document.getElementById('card-' + machine);
    if (!card) {
        card = document.createElement('div');
        card.id = 'card-' + machine;
        card.className = 'card stat-card stat-card--interactive';
        // Built with createElement/textContent, never an innerHTML template. The machine
        // name arrives from /api/report, which is unauthenticated by design -- anything
        // that can reach the hub picks its own name. Interpolating it into markup made
        // that an unauthenticated stored-XSS into an operator session, and an operator
        // session is fleet-wide code execution as SYSTEM (see fleet.py). Setting .id as a
        // property is likewise safe for arbitrary names; getElementById takes a literal
        // id, not a selector, so no escaping is needed on the lookups below.
        const nameEl = document.createElement('div');
        nameEl.className = 'machine-card__name';
        nameEl.textContent = machine;

        const infoEl = document.createElement('div');
        infoEl.className = 'machine-card__info';
        infoEl.id = 'info-' + machine;
        infoEl.style.display = 'none';

        const tempEl = document.createElement('div');
        tempEl.className = 'stat-card__value';
        tempEl.id = 'temp-' + machine;
        tempEl.textContent = '-- °C';

        const uptimeEl = document.createElement('div');
        uptimeEl.className = 'stat-card__meta';
        uptimeEl.id = 'uptime-' + machine;
        uptimeEl.textContent = t('common.uptime_unknown');

        const pillEl = document.createElement('span');
        pillEl.className = 'status-pill status-pill--muted';
        pillEl.id = 'status-' + machine;
        pillEl.style.marginTop = '10px';
        const dotEl = document.createElement('span');
        dotEl.className = 'status-pill__dot';
        pillEl.append(dotEl, '--');

        card.append(nameEl, infoEl, tempEl, uptimeEl, pillEl);
        card.addEventListener('click', () => goToMachine(machine));
        machineCards.appendChild(card);
        emptyStateEl.style.display = 'none';
    }

    if (info) {
        const infoEl = document.getElementById('info-' + machine);
        const text = formatMachineInfo(info);
        infoEl.textContent = text;
        infoEl.style.display = text ? '' : 'none';
    }

    if (uptimeSeconds !== undefined && uptimeSeconds !== null) {
        document.getElementById('uptime-' + machine).textContent = t('common.uptime', { value: formatUptime(uptimeSeconds) });
    }

    const statusEl = document.getElementById('status-' + machine);
    if (temp === undefined || temp === null) return;

    document.getElementById('temp-' + machine).innerText = Number(temp).toFixed(1) + ' °C';
    // A card on the live Dashboard is, by construction, a machine currently reporting.
    // High temperature is not flagged here (see the top of this file). Reporting is not the
    // same as reachable, though: a machine with no enrollment posts telemetry and answers
    // nothing, so the pill says so.
    setMachineStatusPill(statusEl, {
        status: 'online',
        enrolled: enrollment.has(machine) ? enrollment.get(machine) : undefined,
    });
}

async function refreshMachineInfo() {
    try {
        const resp = await fetch('/api/machines');
        if (!resp.ok) return;
        const rows = await resp.json();
        // The live Dashboard shows only currently-online machines. Offline (and
        // deleted) machines live in the Asset Inventory, not here.
        const online = rows.filter((row) => row.status === 'online');
        const onlineNames = new Set(online.map((row) => row.machine));
        // Reconcile: drop any card whose machine is no longer online.
        for (const card of Array.from(machineCards.children)) {
            const name = card.id.replace(/^card-/, '');
            if (!onlineNames.has(name)) card.remove();
        }
        emptyStateEl.style.display = online.length ? 'none' : 'block';
        for (const row of online) {
            // Recorded before the card is drawn -- updateMachineCard reads it back out.
            if (typeof row.enrolled === 'boolean') enrollment.set(row.machine, row.enrolled);
            updateMachineCard(row.machine, row.temp, row.uptime_seconds, row);
        }
        // Machines that left the live view keep no entry: they are gone from the map for the
        // same reason their card is gone from the page.
        for (const name of [...enrollment.keys()]) {
            if (!onlineNames.has(name)) enrollment.delete(name);
        }
    } catch (e) { /* non-critical, dashboard still works without it */ }
}

refreshMachineInfo();
// Re-poll so a machine that goes quiet disappears from the live view without a reload.
setInterval(refreshMachineInfo, 30000);

socket.on('new_temp', (msg) => {
    updateMachineCard(msg.machine, msg.temp, msg.uptime_seconds, undefined);
});
