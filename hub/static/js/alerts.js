// Alerts: operator-facing conditions that want attention. Four kinds:
//   * rule -- raised by an operator-written rule's `alert` action. The text comes from the
//     rule, so the card just states it; one card per EPISODE, like the others.
//   * duplicate_serial -- two machines sharing a serial while both online. The hub refuses
//     to auto-merge live machines, so the operator picks a survivor here and the rest are
//     merged into it (POST /api/machines/merge).
//   * ad_unmatched -- a machine this hub manages that Active Directory has no computer
//     object for.
//   * high_temperature -- RETIRED. The hub no longer raises these (temperature is a rule
//     now), but rows an operator has not dismissed are still in the table, so the renderer
//     stays.
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

// An explicit table rather than a chain of ifs with a fall-through default. The fall-through
// is what broke this: `rule` and `ad_unmatched` were both added server-side after this file
// was written, and both landed in renderDuplicateSerial -- which titles the card from
// `alert.serial_number` (always null on those kinds, so "unknown serial") and offers a Merge
// button with nothing to merge. An unknown kind now gets renderGeneric, so the next kind
// added degrades to a plain, true card instead of a confidently wrong one.
const RENDERERS = {
    rule: renderRule,
    duplicate_serial: renderDuplicateSerial,
    ad_unmatched: renderAdUnmatched,
    high_temperature: renderHighTemp,
};

function renderAlert(alert) {
    return (RENDERERS[alert.kind] || renderGeneric)(alert);
}

// Every card shares this shape: a bold title, a meta paragraph, then an action row that
// always ends in Dismiss. Factored out so a new kind is a title and a sentence rather than
// forty lines of DOM, and so the four cards cannot drift apart visually.
function alertCard(title, meta, machine) {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.marginBottom = 'var(--space-5)';

    const titleEl = document.createElement('div');
    titleEl.style.fontWeight = '600';
    titleEl.style.marginBottom = 'var(--space-2)';
    titleEl.textContent = title;
    card.appendChild(titleEl);

    if (meta) {
        const metaEl = document.createElement('p');
        metaEl.className = 'stat-card__meta';
        metaEl.style.marginBottom = 'var(--space-4)';
        metaEl.textContent = meta;
        card.appendChild(metaEl);
    }
    return card;
}

// The action row for a card with nothing to decide: optionally a link to the machine, then
// Dismiss. Appended by the caller so a card can put its own controls in first.
function alertActions(alert, card) {
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
    return actions;
}

// A rule alert: one episode of one rule matching one machine. The body text is the rule
// author's, rendered from their template server-side, so it goes on the card verbatim --
// this renderer's job is to say WHICH rule and for HOW LONG, not to editorialise.
function renderRule(alert) {
    const detail = alert.detail || {};
    const ongoing = !alert.episode_ended_at;
    const machineName = alert.machine || t('alerts.unknown_machine');
    const ruleName = detail.rule_name || t('alerts.rule.unnamed');

    const card = alertCard(
        ongoing
            ? t('alerts.rule.title', { rule: ruleName, machine: machineName })
            : t('alerts.rule.title_ended', { rule: ruleName, machine: machineName }),
        detail.text || '');

    // Whole sentences from the catalog, never clauses joined with `+` -- see the note on
    // the high-temperature bodies below for why.
    const when = document.createElement('p');
    when.className = 'stat-card__meta';
    when.style.marginBottom = 'var(--space-4)';
    const count = Number(detail.count) || 1;
    if (ongoing) {
        when.textContent = count > 1
            ? tPlural('alerts.rule.ongoing_count', count,
                      { since: formatEpoch(alert.created_at), count })
            : t('alerts.rule.ongoing', { since: formatEpoch(alert.created_at) });
    } else {
        when.textContent = t('alerts.rule.ended', {
            from: formatEpoch(alert.created_at),
            until: formatEpoch(alert.episode_ended_at),
        });
    }
    card.appendChild(when);

    card.appendChild(alertActions(alert, card));
    return card;
}

// A machine this hub manages that Active Directory has no computer object for. Nothing to
// decide here either: the fix is in AD, not in this console.
function renderAdUnmatched(alert) {
    const machineName = alert.machine || t('alerts.unknown_machine');
    const card = alertCard(t('alerts.ad_unmatched.title', { machine: machineName }),
                           t('alerts.ad_unmatched.body', { machine: machineName }));
    card.appendChild(alertActions(alert, card));
    return card;
}

// A kind this build does not know about -- an older console against a newer hub. Says
// exactly that and offers Dismiss, rather than guessing at a layout and misreporting it.
function renderGeneric(alert) {
    const card = alertCard(t('alerts.unknown_kind', { kind: alert.kind || '?' }),
                           alert.machine || '');
    card.appendChild(alertActions(alert, card));
    return card;
}

// A temperature alert: one episode of a machine's windowed AVERAGE crossing the threshold.
// RETIRED -- nothing raises this kind any more (temperature is an ordinary rule now), but
// alerts an operator has not yet dismissed are still in the table and must still render.
// There is nothing to decide (unlike a merge), so the card just states the condition, links
// to the machine, and offers Dismiss.
function renderHighTemp(alert) {
    const ongoing = !alert.episode_ended_at;
    const machineName = alert.machine || t('alerts.unknown_machine');
    const detail = alert.detail || {};

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
    let body;
    if (ongoing) {
        const params = { window: windowLabel, avg, threshold, since: formatEpoch(alert.created_at) };
        body = (peak && peak !== avg)
            ? t('alerts.high_temp.ongoing_peak', Object.assign({ peak }, params))
            : t('alerts.high_temp.ongoing', params);
    } else {
        const params = {
            threshold,
            from: formatEpoch(alert.created_at),
            until: formatEpoch(alert.episode_ended_at),
        };
        body = peak
            ? t('alerts.high_temp.ended_peak', Object.assign({ peak, window: windowLabel }, params))
            : t('alerts.high_temp.ended', params);
    }

    const card = alertCard(
        ongoing
            ? t('alerts.high_temp.title', { machine: machineName })
            : t('alerts.high_temp.title_ended', { machine: machineName }),
        body);
    card.appendChild(alertActions(alert, card));
    return card;
}

function renderDuplicateSerial(alert) {
    // The one kind that does NOT use alertActions: it has a decision to offer (which record
    // survives), so it builds its own action row with Merge in front of Dismiss.
    const card = alertCard(
        t('alerts.duplicate.title', {
            serial: alert.serial_number || t('alerts.duplicate.unknown_serial'),
        }),
        t('alerts.duplicate.intro'));

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
