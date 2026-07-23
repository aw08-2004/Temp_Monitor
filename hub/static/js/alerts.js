// Alerts: operator-facing conditions that want attention. Two kinds:
//   * duplicate_serial -- two machines sharing a serial while both online. The hub refuses
//     to auto-merge live machines, so the operator picks a survivor here and the rest are
//     merged into it (POST /api/machines/merge).
//   * overheat -- a machine whose AVERAGE temperature over the configured window is at or
//     above the overheat threshold. Raised/resolved server-side; the operator can Dismiss.
// Reads /api/alerts, acts via /api/machines/merge and /api/alerts/<id>/dismiss. Mirrors
// inventory.js: build DOM with textContent (never innerHTML from data), poll to stay fresh.

const alertsList = document.getElementById('alerts-list');
const alertsEmpty = document.getElementById('alerts-empty');

function formatLastSeen(updatedAt) {
    return updatedAt || '--';
}

// alerts.created_at/updated_at are epoch SECONDS (unlike machine_info's timestamp strings),
// so overheat alerts format them into a readable local time rather than showing a raw int.
function formatEpoch(epoch) {
    return epoch ? new Date(epoch * 1000).toLocaleString() : '--';
}

async function mergeAlert(survivor, victims, cardEl, btnEl) {
    if (!window.confirm(
        `Keep "${survivor}" and merge ${victims.length === 1 ? `"${victims[0]}"` : `${victims.length} machines`} into it?\n\n` +
        `The merged machines' temperature history moves onto "${survivor}"; their duplicate records and fleet enrollments are removed. This cannot be undone.`)) {
        return;
    }
    btnEl.disabled = true;
    btnEl.textContent = 'Merging…';
    try {
        const resp = await fetch('/api/machines/merge', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ survivor, victims }),
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${resp.status}`);
        }
        loadAlerts();
    } catch (e) {
        btnEl.disabled = false;
        btnEl.textContent = 'Merge';
        window.alert(`Could not merge: ${e.message}`);
    }
}

async function dismissAlert(alertId, cardEl, btnEl) {
    btnEl.disabled = true;
    try {
        const resp = await fetch('/api/alerts/' + encodeURIComponent(alertId) + '/dismiss', { method: 'POST' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        loadAlerts();
    } catch (e) {
        btnEl.disabled = false;
        window.alert(`Could not dismiss alert: ${e.message}`);
    }
}

function renderAlert(alert) {
    if (alert.kind === 'overheat') return renderOverheat(alert);
    return renderDuplicateSerial(alert);
}

// A temperature alert: one machine whose windowed AVERAGE crossed the threshold. There is
// nothing to decide (unlike a merge), so the card just states the condition, links to the
// machine, and offers Dismiss -- it also auto-resolves server-side once the machine cools.
function renderOverheat(alert) {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.marginBottom = 'var(--space-5)';

    const title = document.createElement('div');
    title.style.fontWeight = '600';
    title.style.marginBottom = 'var(--space-2)';
    title.textContent = `🔥 Overheating: ${alert.machine || '(unknown machine)'}`;
    card.appendChild(title);

    const detail = alert.detail || {};
    const meta = document.createElement('p');
    meta.className = 'stat-card__meta';
    meta.style.marginBottom = 'var(--space-4)';
    const windowMins = detail.window_seconds ? Math.round(detail.window_seconds / 60) : null;
    const avg = typeof detail.avg_temp === 'number' ? detail.avg_temp.toFixed(1) : '?';
    const threshold = detail.threshold != null ? detail.threshold : '?';
    meta.textContent =
        `${windowMins ? windowMins + '-min' : 'Windowed'} average ${avg} °C `
        + `is at or above the ${threshold} °C threshold. Since ${formatEpoch(alert.created_at)}.`;
    card.appendChild(meta);

    const actions = document.createElement('div');
    actions.style.marginTop = 'var(--space-4)';
    actions.style.display = 'flex';
    actions.style.gap = 'var(--space-3)';

    if (alert.machine) {
        const view = document.createElement('a');
        view.className = 'btn btn--primary';
        view.textContent = 'View machine';
        view.href = '/machine/' + encodeURIComponent(alert.machine);
        actions.appendChild(view);
    }

    const dismissBtn = document.createElement('button');
    dismissBtn.type = 'button';
    dismissBtn.className = 'btn btn--ghost';
    dismissBtn.textContent = 'Dismiss';
    dismissBtn.addEventListener('click', () => dismissAlert(alert.id, card, dismissBtn));
    actions.appendChild(dismissBtn);

    card.appendChild(actions);
    return card;
}

function renderDuplicateSerial(alert) {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.marginBottom = 'var(--space-5)';

    const title = document.createElement('div');
    title.style.fontWeight = '600';
    title.style.marginBottom = 'var(--space-2)';
    title.textContent = `⚠ Duplicate serial number: ${alert.serial_number || '(unknown)'}`;
    card.appendChild(title);

    const meta = document.createElement('p');
    meta.className = 'stat-card__meta';
    meta.style.marginBottom = 'var(--space-4)';
    meta.textContent = 'These machines report the same serial and are both online. Choose the record to keep:';
    card.appendChild(meta);

    const machines = alert.machines || [];
    // Default survivor: the first still-online machine, else the first row.
    const defaultOnline = machines.find((m) => m.status === 'online') || machines[0];
    const radioName = 'survivor-' + alert.id;

    const table = document.createElement('table');
    table.className = 'data-table';
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>Keep</th><th>Machine</th><th>Status</th><th>Model</th><th>Last seen</th></tr>';
    table.appendChild(thead);
    const tbody = document.createElement('tbody');

    for (const m of machines) {
        const tr = document.createElement('tr');

        const keepTd = document.createElement('td');
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = radioName;
        radio.value = m.machine;
        if (defaultOnline && m.machine === defaultOnline.machine) radio.checked = true;
        keepTd.appendChild(radio);

        const nameTd = document.createElement('td');
        nameTd.textContent = m.machine;

        const statusTd = document.createElement('td');
        const pill = document.createElement('span');
        pill.className = 'status-pill';
        const online = m.status === 'online';
        setStatusPill(pill, online ? 'ok' : 'muted', online ? 'Online' : 'Offline');
        statusTd.appendChild(pill);

        const modelTd = document.createElement('td');
        modelTd.textContent = m.model || '--';
        const seenTd = document.createElement('td');
        seenTd.textContent = formatLastSeen(m.updated_at);

        tr.append(keepTd, nameTd, statusTd, modelTd, seenTd);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    card.appendChild(table);

    const actions = document.createElement('div');
    actions.style.marginTop = 'var(--space-4)';
    actions.style.display = 'flex';
    actions.style.gap = 'var(--space-3)';

    const mergeBtn = document.createElement('button');
    mergeBtn.type = 'button';
    mergeBtn.className = 'btn btn--primary';
    mergeBtn.textContent = 'Merge';
    mergeBtn.addEventListener('click', () => {
        const chosen = card.querySelector(`input[name="${radioName}"]:checked`);
        if (!chosen) { window.alert('Pick a machine to keep first.'); return; }
        const survivor = chosen.value;
        const victims = machines.map((m) => m.machine).filter((name) => name !== survivor);
        mergeAlert(survivor, victims, card, mergeBtn);
    });

    const dismissBtn = document.createElement('button');
    dismissBtn.type = 'button';
    dismissBtn.className = 'btn btn--ghost';
    dismissBtn.textContent = 'Dismiss';
    dismissBtn.addEventListener('click', () => dismissAlert(alert.id, card, dismissBtn));

    actions.append(mergeBtn, dismissBtn);
    card.appendChild(actions);
    return card;
}

async function loadAlerts() {
    try {
        const resp = await fetch('/api/alerts');
        if (!resp.ok) return;
        const alerts = await resp.json();
        alertsList.innerHTML = '';
        alertsEmpty.style.display = alerts.length ? 'none' : 'block';
        for (const alert of alerts) {
            alertsList.appendChild(renderAlert(alert));
        }
    } catch (e) {
        alertsList.innerHTML = '<p class="stat-card__meta">Failed to load alerts.</p>';
    }
}

loadAlerts();
setInterval(loadAlerts, 30000);
