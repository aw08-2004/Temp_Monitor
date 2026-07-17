// Asset Inventory: the full roster of every machine the hub has ever seen, with
// online/offline status and a per-row hard-delete. Reads the same /api/machines the
// Dashboard uses (which now carries a `status` field), but shows offline machines too.

const inventoryBody = document.getElementById('inventory-body');
const inventoryEmpty = document.getElementById('inventory-empty');

function formatLastSeen(updatedAt) {
    if (!updatedAt) return '--';
    // updated_at is a server-local "YYYY-MM-DD HH:MM:SS" string; show it as-is.
    return updatedAt;
}

function formatTemp(temp) {
    return (temp === null || temp === undefined) ? '--' : `${Number(temp).toFixed(1)} °C`;
}

async function deleteMachine(machine, rowEl, btnEl) {
    if (!window.confirm(`Permanently delete "${machine}"?\n\nThis removes its identity, all temperature history, and its fleet enrollment. This cannot be undone.`)) {
        return;
    }
    btnEl.disabled = true;
    btnEl.textContent = 'Deleting…';
    try {
        const resp = await fetch('/api/machines/' + encodeURIComponent(machine), { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        rowEl.remove();
        if (!inventoryBody.querySelector('tr')) {
            inventoryEmpty.style.display = 'block';
        }
    } catch (e) {
        btnEl.disabled = false;
        btnEl.textContent = 'Delete';
        window.alert(`Could not delete "${machine}": ${e.message}`);
    }
}

function renderRow(row) {
    const tr = document.createElement('tr');

    const nameTd = document.createElement('td');
    const link = document.createElement('a');
    link.href = '/machine/' + encodeURIComponent(row.machine);
    link.textContent = row.machine;
    nameTd.appendChild(link);

    const statusTd = document.createElement('td');
    const pill = document.createElement('span');
    pill.className = 'status-pill';
    const online = row.status === 'online';
    setStatusPill(pill, online ? 'ok' : 'muted', online ? 'Online' : 'Offline');
    statusTd.appendChild(pill);

    const modelTd = document.createElement('td');
    modelTd.textContent = row.model || '--';
    const serialTd = document.createElement('td');
    serialTd.textContent = row.serial_number || '--';
    const assetTd = document.createElement('td');
    assetTd.textContent = row.asset_tag || '--';
    const tempTd = document.createElement('td');
    tempTd.textContent = formatTemp(row.temp);
    const seenTd = document.createElement('td');
    seenTd.textContent = formatLastSeen(row.updated_at);

    const actionTd = document.createElement('td');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--ghost';
    btn.style.color = 'var(--danger, #e5484d)';
    btn.textContent = 'Delete';
    btn.addEventListener('click', () => deleteMachine(row.machine, tr, btn));
    actionTd.appendChild(btn);

    tr.append(nameTd, statusTd, modelTd, serialTd, assetTd, tempTd, seenTd, actionTd);
    return tr;
}

async function loadInventory() {
    try {
        const resp = await fetch('/api/machines');
        if (!resp.ok) return;
        const rows = await resp.json();
        inventoryBody.innerHTML = '';
        inventoryEmpty.style.display = rows.length ? 'none' : 'block';
        // Online first, then alphabetical -- the machines you can act on live now sort up top.
        rows.sort((a, b) => {
            const rank = (r) => (r.status === 'online' ? 0 : 1);
            return rank(a) - rank(b) || String(a.machine).localeCompare(String(b.machine));
        });
        for (const row of rows) {
            inventoryBody.appendChild(renderRow(row));
        }
    } catch (e) {
        inventoryBody.innerHTML = '<tr><td colspan="8" class="stat-card__meta">Failed to load inventory.</td></tr>';
    }
}

loadInventory();
// Keep status fresh without a manual reload.
setInterval(loadInventory, 30000);
