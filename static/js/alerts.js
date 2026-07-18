// Alerts: operator-facing conflicts. Today that's duplicate machines sharing a serial
// number while both are online -- the hub refuses to auto-merge live machines, so the
// operator picks a survivor here and the rest are merged into it. Reads /api/alerts,
// acts via POST /api/machines/merge and /api/alerts/<id>/dismiss. Mirrors inventory.js:
// build DOM with textContent (never innerHTML from data), poll to stay fresh.

const alertsList = document.getElementById('alerts-list');
const alertsEmpty = document.getElementById('alerts-empty');

function formatLastSeen(updatedAt) {
    return updatedAt || '--';
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
