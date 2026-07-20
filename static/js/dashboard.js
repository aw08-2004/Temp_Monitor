// Thresholds come from the server (they're operator-settable in Settings), via data
// attributes on #dashboard-config -- same idiom as machine.js. These used to be
// hardcoded 40/85 here, which meant the Dashboard and the machine page could disagree
// about whether the same reading was overheating once the setting moved.
const dashboardConfig = document.getElementById('dashboard-config');
const OVERHEAT_THRESHOLD = Number(dashboardConfig.dataset.overheatThreshold);
const LOW_LOAD_THRESHOLD = Number(dashboardConfig.dataset.lowLoadThreshold);

requestNotificationPermission();

const socket = connectSocketWithStatus();
const machineCards = document.getElementById('machine-cards');
const emptyStateEl = document.getElementById('empty-state');

function formatMachineInfo(info) {
    if (!info) return '';
    const parts = [];
    if (info.model) parts.push(info.model);
    if (info.serial_number) parts.push(`SN: ${info.serial_number}`);
    if (info.asset_tag) parts.push(`Asset: ${info.asset_tag}`);
    return parts.join(' • ');
}

function goToMachine(machine) {
    window.location.href = '/machine/' + encodeURIComponent(machine);
}

function updateMachineCard(machine, temp, threshold, uptimeSeconds, info, diagnostics) {
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
        uptimeEl.textContent = 'Uptime: --';

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
        document.getElementById('uptime-' + machine).textContent = `Uptime: ${formatUptime(uptimeSeconds)}`;
    }

    const statusEl = document.getElementById('status-' + machine);
    if (temp === undefined || temp === null) return;

    document.getElementById('temp-' + machine).innerText = Number(temp).toFixed(1) + ' °C';

    const cpuLoadPct = diagnostics && typeof diagnostics.cpu_load_pct === 'number' ? diagnostics.cpu_load_pct : null;
    const status = classifyOverheatStatus(temp, threshold, cpuLoadPct, LOW_LOAD_THRESHOLD);

    if (status === 'normal') {
        card.classList.remove('stat-card--overheat');
        setStatusPill(statusEl, 'ok', 'Normal');
        return;
    }

    const wasOverheating = card.classList.contains('stat-card--overheat');
    card.classList.add('stat-card--overheat');
    if (status === 'overheat-expected') {
        setStatusPill(statusEl, 'warn', '🔥 Overheating (high load)');
    } else {
        setStatusPill(statusEl, 'danger', '🔥 Overheating (low load — investigate)');
    }
    if (!wasOverheating) {
        notifyOverheat(machine, temp);
    }
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
            updateMachineCard(row.machine, row.temp, OVERHEAT_THRESHOLD, row.uptime_seconds, row, row.diagnostics);
        }
    } catch (e) { /* non-critical, dashboard still works without it */ }
}

refreshMachineInfo();
// Re-poll so a machine that goes quiet disappears from the live view without a reload.
setInterval(refreshMachineInfo, 30000);

socket.on('new_temp', (msg) => {
    updateMachineCard(msg.machine, msg.temp, msg.threshold, msg.uptime_seconds, undefined, msg.diagnostics);
});
