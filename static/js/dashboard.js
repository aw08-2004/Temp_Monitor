// /api/machines doesn't send thresholds today (matches the existing hardcoded 85
// below), so this mirrors that pattern rather than introducing a new inconsistency.
const LOW_LOAD_THRESHOLD = 40;

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
        card.innerHTML = `
            <div class="machine-card__name">${machine}</div>
            <div class="machine-card__info" id="info-${machine}" style="display:none;"></div>
            <div class="stat-card__value" id="temp-${machine}">-- °C</div>
            <div class="stat-card__meta" id="uptime-${machine}">Uptime: --</div>
            <span class="status-pill status-pill--muted" id="status-${machine}" style="margin-top: 10px;"><span class="status-pill__dot"></span>--</span>
        `;
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
        emptyStateEl.style.display = rows.length ? 'none' : 'block';
        for (const row of rows) {
            updateMachineCard(row.machine, row.temp, 85, row.uptime_seconds, row, row.diagnostics);
        }
    } catch (e) { /* non-critical, dashboard still works without it */ }
}

refreshMachineInfo();

socket.on('new_temp', (msg) => {
    updateMachineCard(msg.machine, msg.temp, msg.threshold, msg.uptime_seconds, undefined, msg.diagnostics);
});
