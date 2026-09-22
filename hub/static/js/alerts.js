// Alerts: operator-facing conditions that want attention, grouped into BUNDLES (roadmap
// #17). A machine whose disk filled, whose backup service then failed, and whose CPU pinned
// while it retried raises three alerts about one problem; the hub groups those three and
// this file renders the group as one block with its member cards inside it.
//
// **Two fetches, deliberately.** /api/alerts/bundles owns the grouping, the causal claim and
// any recommendation; /api/alerts owns the per-alert payload, including the machine status
// enrichment only duplicate_serial cards use. Joining them here on alert id keeps that
// enrichment in the one endpoint that already does it, rather than growing a second copy in
// correlate_web.py that would drift. If the bundles call fails, every alert renders flat --
// the grouping is a lens over the list, so losing it costs the lens, not the list.
//
// Four alert kinds:
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

// ---------------------------------------------------------------------------- bundles

// Server vocabularies, rendered through literal t() calls in a lookup map rather than a key
// built by `+`. The catalog scan in tests/test_i18n.py only sees literal keys, so a key
// assembled from a prefix and a server-supplied code is a typo no test can catch -- it just
// prints its own key on the card. Same idiom wake.js uses for its diagnosis codes, and the
// same reason CLAUDE.md gives for preferring a literal key over a computed one.
const CAUSE_LABELS = {
    disk_starved_process: () => t('alerts.bundle.cause.disk_starved_process'),
    disk_starved_agent: () => t('alerts.bundle.cause.disk_starved_agent'),
    memory_paging: () => t('alerts.bundle.cause.memory_paging'),
    memory_starved_process: () => t('alerts.bundle.cause.memory_starved_process'),
    thermal_throttling: () => t('alerts.bundle.cause.thermal_throttling'),
    offline_unmatched: () => t('alerts.bundle.cause.offline_unmatched'),
};

const FACET_LABELS = {
    disk: () => t('alerts.bundle.facet.disk'),
    memory: () => t('alerts.bundle.facet.memory'),
    cpu: () => t('alerts.bundle.facet.cpu'),
    thermal: () => t('alerts.bundle.facet.thermal'),
    network: () => t('alerts.bundle.facet.network'),
    process: () => t('alerts.bundle.facet.process'),
    liveness: () => t('alerts.bundle.facet.liveness'),
    session: () => t('alerts.bundle.facet.session'),
    directory: () => t('alerts.bundle.facet.directory'),
    identity: () => t('alerts.bundle.facet.identity'),
    event: () => t('alerts.bundle.facet.event'),
    other: () => t('alerts.bundle.facet.other'),
};

// An unrecognised code shows itself. A newer hub adding a facet renders as `disk_io` rather
// than as a missing catalog key, which is ugly and true instead of broken and confident.
function labelFor(map, value) {
    const fn = map[value];
    return fn ? fn() : value;
}

function bundleUrl(bundle, suffix) {
    return '/api/alerts/bundles/' + encodeURIComponent(bundle.machine)
        + '/' + encodeURIComponent(bundle.anchor) + (suffix || '');
}

async function requestRecommendation(bundle, boxEl, btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = t('alerts.bundle.recommend_working');
    try {
        const resp = await fetch(bundleUrl(bundle, '/recommend'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
        // Replace the button with the answer in place. The operator asked about THIS bundle
        // and is looking at it; a full reload would scroll the page out from under them.
        boxEl.replaceChildren(renderRecommendation(bundle, body));
    } catch (e) {
        btnEl.disabled = false;
        btnEl.textContent = t('alerts.bundle.recommend');
        const failed = document.createElement('p');
        failed.className = 'stat-card__meta';
        failed.textContent = t('alerts.bundle.recommend_failed', { error: e.message });
        boxEl.appendChild(failed);
    }
}

async function draftSuggestedScript(bundle, btnEl, noteEl) {
    btnEl.disabled = true;
    try {
        const resp = await fetch(bundleUrl(bundle, '/script'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
        // Says "switched off" on purpose. The whole safety argument for a suggested script is
        // that a human reads it and turns it on, so the confirmation has to state that it is
        // off rather than read as "done".
        noteEl.textContent = t('alerts.bundle.script_drafted', { name: body.name });
    } catch (e) {
        btnEl.disabled = false;
        noteEl.textContent = t('alerts.bundle.script_failed', { error: e.message });
    }
}

// What else the machine was saying while the bundle was open: metrics outside its own
// trailing normal, and #16's event log rows inside the same window. Loaded on a click and
// not with the list, deliberately -- a baseline is a scan of a fortnight of readings per
// machine and the Alerts tab polls, so doing it per bundle per poll would make the alert
// badge the most expensive query in the hub.
async function loadBundleDetail(bundle, boxEl, btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = t('alerts.bundle.detail_working');
    try {
        const resp = await fetch(bundleUrl(bundle, ''));
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
        boxEl.replaceChildren(renderBundleDetail(body.facts || {}));
    } catch (e) {
        btnEl.disabled = false;
        btnEl.textContent = t('alerts.bundle.detail');
        const failed = document.createElement('p');
        failed.className = 'stat-card__meta';
        failed.textContent = t('alerts.bundle.detail_failed', { error: e.message });
        boxEl.appendChild(failed);
    }
}

// One decimal on a percentage or a temperature; a whole number on an RPM. toFixed(1) on
// everything would report a fan at "1234.0 rpm", which reads as a precision nobody has.
function formatMetric(value) {
    if (typeof value !== 'number') return '?';
    return Math.abs(value) >= 1000 ? String(Math.round(value)) : value.toFixed(1);
}

function renderBundleDetail(facts) {
    const box = document.createElement('div');
    box.style.marginTop = 'var(--space-3)';

    const anomalies = facts.anomalies || [];
    const events = facts.events || [];
    // Absence, not an all-clear. A machine with three days of history has no baseline and no
    // collected events, and this sentence says nothing stood out -- which is true -- rather
    // than that everything is fine, which is not something the hub knows.
    if (!anomalies.length && !events.length) {
        const none = document.createElement('p');
        none.className = 'stat-card__meta';
        none.textContent = t('alerts.bundle.detail_none');
        box.appendChild(none);
        return box;
    }

    if (anomalies.length) {
        box.appendChild(Object.assign(document.createElement('div'), {
            className: 'section-title',
            textContent: t('alerts.bundle.anomalies_title'),
        }));
        const list = document.createElement('ul');
        list.className = 'stat-card__meta';
        for (const anomaly of anomalies) {
            const item = document.createElement('li');
            item.textContent = t('alerts.bundle.anomaly_row', {
                metric: anomaly.metric,
                value: formatMetric(anomaly.value),
                median: formatMetric(anomaly.median),
            });
            list.appendChild(item);
        }
        box.appendChild(list);
    }

    if (events.length) {
        box.appendChild(Object.assign(document.createElement('div'), {
            className: 'section-title',
            textContent: t('alerts.bundle.events_title'),
        }));
        const list = document.createElement('ul');
        list.className = 'stat-card__meta';
        for (const event of events) {
            const item = document.createElement('li');
            item.textContent = t('alerts.bundle.event_row', {
                log: event.log,
                event_id: event.event_id,
                level: event.level,
                count: event.count,
                message: event.message || '',
            });
            list.appendChild(item);
        }
        box.appendChild(list);
    }
    return box;
}

function renderRecommendation(bundle, recommendation) {
    const box = document.createElement('div');
    box.className = 'notice';
    box.style.marginTop = 'var(--space-4)';

    box.appendChild(Object.assign(document.createElement('div'), {
        className: 'section-title',
        textContent: t('alerts.bundle.recommendation_title'),
    }));

    const explanation = document.createElement('p');
    explanation.className = 'stat-card__meta';
    explanation.textContent = recommendation.explanation || '';
    box.appendChild(explanation);

    const steps = recommendation.steps || [];
    if (steps.length) {
        const list = document.createElement('ol');
        list.className = 'stat-card__meta';
        for (const step of steps) {
            const item = document.createElement('li');
            item.textContent = step;
            list.appendChild(item);
        }
        box.appendChild(list);
    }

    // The provenance line is not decoration. This paragraph is the one place on the page
    // whose words came from a language model rather than from the hub, and an operator about
    // to quote it into a change ticket should be able to see that at a glance.
    const source = document.createElement('p');
    source.className = 'stat-card__meta';
    source.textContent = t('alerts.bundle.recommendation_source', {
        provider: recommendation.provider || '?',
        model: recommendation.model || '?',
    });
    box.appendChild(source);

    if (recommendation.script && bundle.can_draft_script) {
        const pre = document.createElement('pre');
        pre.className = 'stat-card__meta';
        pre.style.whiteSpace = 'pre-wrap';
        pre.textContent = recommendation.script.body || '';
        box.appendChild(pre);

        const note = document.createElement('p');
        note.className = 'stat-card__meta';

        if (recommendation.script_name) {
            note.textContent = t('alerts.bundle.script_drafted', { name: recommendation.script_name });
        } else {
            const draftBtn = document.createElement('button');
            draftBtn.type = 'button';
            draftBtn.className = 'btn btn--ghost';
            draftBtn.textContent = t('alerts.bundle.draft_script');
            draftBtn.addEventListener('click', () => draftSuggestedScript(bundle, draftBtn, note));
            box.appendChild(draftBtn);
        }
        box.appendChild(note);
    }
    return box;
}

// A bundle of two or more alerts: a heading that says how many and on which machine, the
// causal claim when the hub has one, and the member cards below. A bundle of ONE is not
// rendered with any of this -- see renderBundle.
function renderBundleHeader(bundle) {
    const header = document.createElement('div');
    header.className = 'card';
    header.style.marginBottom = 'var(--space-3)';

    const title = document.createElement('div');
    title.style.fontWeight = '600';
    title.style.marginBottom = 'var(--space-2)';
    title.textContent = tPlural('alerts.bundle.title', bundle.alert_ids.length, {
        count: bundle.alert_ids.length,
        machine: bundle.machine || t('alerts.unknown_machine'),
    });
    header.appendChild(title);

    const meta = document.createElement('p');
    meta.className = 'stat-card__meta';
    meta.style.marginBottom = 'var(--space-3)';
    // Two whole sentences from the catalog, never one assembled from clauses -- the same
    // rule the rule and high-temperature bodies above follow. Without a causal pair the line
    // says what the alerts have in common and stops; claiming a cause the hub cannot
    // establish is the one thing this feature must never do.
    if (bundle.cause) {
        meta.textContent = t('alerts.bundle.cause_line', {
            cause: labelFor(CAUSE_LABELS, bundle.cause.reason),
            since: formatEpoch(bundle.started_at),
        });
    } else {
        meta.textContent = t('alerts.bundle.together', {
            facets: (bundle.facets || []).map((f) => labelFor(FACET_LABELS, f)).join(', '),
            since: formatEpoch(bundle.started_at),
        });
    }
    header.appendChild(meta);

    if (bundle.machine) {
        const detailBox = document.createElement('div');
        const detailBtn = document.createElement('button');
        detailBtn.type = 'button';
        detailBtn.className = 'btn btn--ghost';
        detailBtn.textContent = t('alerts.bundle.detail');
        detailBtn.addEventListener('click', () => loadBundleDetail(bundle, detailBox, detailBtn));
        header.appendChild(detailBtn);
        header.appendChild(detailBox);
    }

    const box = document.createElement('div');
    if (bundle.recommendation) {
        box.appendChild(renderRecommendation(bundle, bundle.recommendation));
    } else if (bundle.can_recommend && bundle.machine) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn--ghost';
        btn.textContent = t('alerts.bundle.recommend');
        btn.addEventListener('click', () => requestRecommendation(bundle, box, btn));
        box.appendChild(btn);
    }
    header.appendChild(box);
    return header;
}

function renderBundle(bundle, byId) {
    const members = bundle.alert_ids.map((id) => byId[String(id)]).filter(Boolean);
    if (!members.length) return null;

    // A bundle of one is just an alert. Wrapping it in bundle chrome would add a heading
    // saying "1 alert on PC-04" above a card that already says so, which is how a grouping
    // feature makes the common case worse than it was before.
    if (members.length === 1) return renderAlert(members[0]);

    const group = document.createElement('div');
    group.style.marginBottom = 'var(--space-5)';
    group.appendChild(renderBundleHeader(bundle));

    const nested = document.createElement('div');
    nested.style.marginLeft = 'var(--space-5)';
    for (const alert of members) {
        nested.appendChild(renderAlert(alert));
    }
    group.appendChild(nested);
    return group;
}

async function loadAlerts() {
    try {
        const resp = await fetch('/api/alerts');
        if (!resp.ok) return;
        const alerts = await resp.json();
        alertsList.innerHTML = '';
        setAlertsEmpty(alerts.length === 0);
        // The badge counts ALERTS, not bundles, and deliberately keeps doing so: it has to
        // agree with /api/alerts/count, which the poller in the shell calls and which knows
        // nothing about grouping. A badge reading 4 beside a tab showing 7 cards is a bug
        // report waiting to happen.
        setAlertBadge(alerts.length);

        const byId = {};
        for (const alert of alerts) byId[String(alert.id)] = alert;

        const grouping = await fetchBundles();
        if (!grouping) {
            for (const alert of alerts) alertsList.appendChild(renderAlert(alert));
            return;
        }
        const rendered = new Set();
        for (const bundle of grouping.bundles || []) {
            bundle.can_recommend = grouping.can_recommend;
            bundle.can_draft_script = grouping.can_draft_script;
            const node = renderBundle(bundle, byId);
            if (!node) continue;
            for (const id of bundle.alert_ids) rendered.add(String(id));
            alertsList.appendChild(node);
        }
        // Anything the grouping did not account for still renders. The two endpoints read the
        // alert table a moment apart, so an alert raised between them belongs to no bundle
        // yet -- and an alert this tab silently dropped would be the worst possible bug in an
        // alerting feature.
        for (const alert of alerts) {
            if (!rendered.has(String(alert.id))) alertsList.appendChild(renderAlert(alert));
        }
    } catch (e) {
        // DOM rather than an innerHTML string, now that the message comes from the catalog.
        const failed = document.createElement('p');
        failed.className = 'stat-card__meta';
        failed.textContent = t('alerts.load_failed');
        alertsList.replaceChildren(failed);
    }
}

// Null on any failure, which loadAlerts renders as a flat list. Grouping is a lens over the
// alert list; losing the lens must not lose the alerts.
async function fetchBundles() {
    try {
        const resp = await fetch('/api/alerts/bundles');
        if (!resp.ok) return null;
        return await resp.json();
    } catch (e) {
        return null;
    }
}

loadAlerts();
setInterval(loadAlerts, 30000);
