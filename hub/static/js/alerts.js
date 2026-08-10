// Alerts: operator-facing conditions that want attention. Two kinds:
//   * duplicate_serial -- two machines sharing a serial while both online. The hub refuses
//     to auto-merge live machines, so the operator picks a survivor here and the rest are
//     merged into it (POST /api/machines/merge).
//   * high_temperature -- a machine whose AVERAGE temperature over the configured window
//     is at or above the threshold. Raised server-side; the operator must Dismiss it (it
//     no longer auto-resolves, to ensure the operator sees it). One card per EPISODE, so a
//     machine that runs hot repeatedly stacks up several cards rather than having the older
//     ones overwritten -- `episode_ended_at` says whether the machine is still hot right
//     now or this card is a past episode.
// Reads /api/alerts, acts via /api/machines/merge and /api/alerts/<id>/dismiss. Mirrors
// inventory.js: build DOM with textContent (never innerHTML from data), poll to stay fresh.

const alertsList = document.getElementById('alerts-list');
const alertsEmpty = document.getElementById('alerts-empty');

function setAlertsEmpty(isEmpty) {
    alertsEmpty.style.display = isEmpty ? 'block' : 'none';
}

function formatLastSeen(updatedAt) {
    return updatedAt || '--';
}

// alerts.created_at/updated_at are epoch SECONDS (unlike machine_info's timestamp strings),
// so temperature alerts format them into a readable local time rather than showing a raw int.
function formatEpoch(epoch) {
    return epoch ? new Date(epoch * 1000).toLocaleString() : '--';
}

async function mergeAlert(survivor, victims, cardEl, btnEl) {
    // One whole sentence per plural form rather than a phrase spliced into the middle of
    // another: "merge X into it" and "merge N machines into it" inflect differently in the
    // languages this ships in, and a translator cannot fix a sentence built by `+`.
    if (!window.confirm(tPlural('alerts.duplicate.confirm', victims.length,
        { survivor, victim: victims[0] }))) {
        return;
    }
    btnEl.disabled = true;
    btnEl.textContent = t('alerts.duplicate.merging');
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
        btnEl.textContent = t('alerts.duplicate.merge');
        window.alert(t('alerts.duplicate.merge_failed', { error: e.message }));
    }
}

async function dismissAlert(alertId, cardEl, btnEl) {
    btnEl.disabled = true;
    try {
        // The JSON content type is not decoration: app.login_required refuses a POST
        // without it, which is what stops a cross-site form from dismissing alerts.
        const resp = await fetch('/api/alerts/' + encodeURIComponent(alertId) + '/dismiss', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        // Drop the card the moment the server confirms, rather than waiting for the
        // refetch below: the operator clicked it, so the card should go now. loadAlerts()
        // still runs and is what the rendering ultimately settles on.
        cardEl.remove();
        const left = alertsList.querySelectorAll('.card').length;
        setAlertsEmpty(left === 0);
        setAlertBadge(left);
        loadAlerts();
    } catch (e) {
        btnEl.disabled = false;
        window.alert(t('alerts.dismiss_failed', { error: e.message }));
    }
}

function renderAlert(alert) {
    if (alert.kind === 'high_temperature') return renderHighTemp(alert);
    return renderDuplicateSerial(alert);
}

// A temperature alert: one episode of a machine's windowed AVERAGE crossing the threshold.
// There is nothing to decide (unlike a merge), so the card just states the condition, links
// to the machine, and offers Dismiss. It remains open until the operator dismisses it, and
// a later hot spell on the same machine arrives as its own card.
function renderHighTemp(alert) {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.marginBottom = 'var(--space-5)';

    const ongoing = !alert.episode_ended_at;

    const title = document.createElement('div');
    title.style.fontWeight = '600';
    title.style.marginBottom = 'var(--space-2)';
    const machineName = alert.machine || t('alerts.unknown_machine');
    title.textContent = ongoing
        ? t('alerts.high_temp.title', { machine: machineName })
        : t('alerts.high_temp.title_ended', { machine: machineName });
    card.appendChild(title);

    const detail = alert.detail || {};
    const meta = document.createElement('p');
    meta.className = 'stat-card__meta';
    meta.style.marginBottom = 'var(--space-4)';
    const windowMins = detail.window_seconds ? Math.round(detail.window_seconds / 60) : null;
    const avg = typeof detail.avg_temp === 'number' ? detail.avg_temp.toFixed(1) : '?';
    const peak = typeof detail.peak_temp === 'number' ? detail.peak_temp.toFixed(1) : null;
    const threshold = detail.threshold != null ? detail.threshold : '?';
    // Four whole sentences in the catalog rather than one assembled from clauses. The
    // English original read fine concatenated; translated, the clause order and the
    // punctuation between them differ per language, so a sentence built by `+` here is one
    // no translator can repair. Only the window is a fragment, and it has its own key.
    const windowLabel = windowMins
        ? t('alerts.high_temp.window_minutes', { minutes: windowMins })
        : t('alerts.high_temp.window_unknown');
    // Past episodes lead with the peak: the last average before it cooled is the least
    // interesting number on a card about something that already happened.
    if (ongoing) {
        const params = { window: windowLabel, avg, threshold, since: formatEpoch(alert.created_at) };
        meta.textContent = (peak && peak !== avg)
            ? t('alerts.high_temp.ongoing_peak', Object.assign({ peak }, params))
            : t('alerts.high_temp.ongoing', params);
    } else {
        const params = {
            threshold,
            from: formatEpoch(alert.created_at),
            until: formatEpoch(alert.episode_ended_at),
        };
        meta.textContent = peak
            ? t('alerts.high_temp.ended_peak', Object.assign({ peak, window: windowLabel }, params))
            : t('alerts.high_temp.ended', params);
    }
    card.appendChild(meta);

    const actions = document.createElement('div');
    actions.style.marginTop = 'var(--space-4)';
    actions.style.display = 'flex';
    actions.style.gap = 'var(--space-3)';

    if (alert.machine) {
        const view = document.createElement('a');
        view.className = 'btn btn--primary';
        view.textContent = t('alerts.view_machine');
        view.href = '/machine/' + encodeURIComponent(alert.machine);
        actions.appendChild(view);
    }

    const dismissBtn = document.createElement('button');
    dismissBtn.type = 'button';
    dismissBtn.className = 'btn btn--ghost';
    dismissBtn.textContent = t('alerts.dismiss');
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
    title.textContent = t('alerts.duplicate.title', {
        serial: alert.serial_number || t('alerts.duplicate.unknown_serial'),
    });
    card.appendChild(title);

    const meta = document.createElement('p');
    meta.className = 'stat-card__meta';
    meta.style.marginBottom = 'var(--space-4)';
    meta.textContent = t('alerts.duplicate.intro');
    card.appendChild(meta);

    const machines = alert.machines || [];
    // Default survivor: the first still-online machine, else the first row.
    const defaultOnline = machines.find((m) => m.status === 'online') || machines[0];
    const radioName = 'survivor-' + alert.id;

    const table = document.createElement('table');
    table.className = 'data-table';
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const label of [
        t('alerts.duplicate.col.keep'),
        t('alerts.duplicate.col.machine'),
        t('alerts.duplicate.col.status'),
        t('alerts.duplicate.col.model'),
        t('alerts.duplicate.col.last_seen'),
    ]) {
        const th = document.createElement('th');
        th.textContent = label;
        headRow.appendChild(th);
    }
    thead.appendChild(headRow);
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
        setStatusPill(pill, online ? 'ok' : 'muted', online ? t('common.status.online') : t('common.status.offline'));
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
    mergeBtn.textContent = t('alerts.duplicate.merge');
    mergeBtn.addEventListener('click', () => {
        const chosen = card.querySelector(`input[name="${radioName}"]:checked`);
        if (!chosen) { window.alert(t('alerts.duplicate.pick_first')); return; }
        const survivor = chosen.value;
        const victims = machines.map((m) => m.machine).filter((name) => name !== survivor);
        mergeAlert(survivor, victims, card, mergeBtn);
    });

    const dismissBtn = document.createElement('button');
    dismissBtn.type = 'button';
    dismissBtn.className = 'btn btn--ghost';
    dismissBtn.textContent = t('alerts.dismiss');
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
        setAlertsEmpty(alerts.length === 0);
        setAlertBadge(alerts.length);
        for (const alert of alerts) {
            alertsList.appendChild(renderAlert(alert));
        }
    } catch (e) {
        // DOM rather than an innerHTML string, now that the message comes from the catalog.
        const failed = document.createElement('p');
        failed.className = 'stat-card__meta';
        failed.textContent = t('alerts.load_failed');
        alertsList.replaceChildren(failed);
    }
}

loadAlerts();
setInterval(loadAlerts, 30000);
