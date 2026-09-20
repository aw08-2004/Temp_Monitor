// Events page: what the fleet's Windows event logs said, and what is being collected
// (roadmap #16).
//
// Same two rules as patches.js and packages.js, for the same reasons, and the first one
// matters more here than anywhere else in the console:
//
//  * Everything is built with textContent / createElement, never innerHTML. An event
//    message is a string Windows -- or any installed provider, or an attacker who managed
//    to get a string into a log -- wrote. It is the most hostile text this product
//    renders, and it is rendered verbatim on purpose, because a truncated or escaped-
//    looking message is the one an operator cannot act on.
//  * The vocabularies (levels, the common channels) come from GET /api/events, not a copy
//    here. A hardcoded list silently stops offering a new level, which reads to an
//    operator as "the feature is broken".
//
// Two panes: what came back, and what is subscribed. The second is shown to viewers as
// well as managers, because an empty first pane is ambiguous until you can see whether
// anything is being collected at all -- the subscription controls are what `can_manage`
// gates, not the list.
//
// This page does NOT poll. Events arrive on a ten-second heartbeat and are rolled up over
// five minutes, so a live-updating list would redraw constantly to show the same rows; the
// refresh button is explicit and the counters name the window they cover.

(function () {
    'use strict';

    const t = window.t;

    const summaryBox = document.getElementById('events-summary');
    const actions = document.getElementById('events-actions');
    const listBox = document.getElementById('events-list');
    const subsPane = document.getElementById('subs-pane');
    const searchInput = document.getElementById('events-search');
    const levelSelect = document.getElementById('events-level');
    const logSelect = document.getElementById('events-log');
    const subModal = document.getElementById('sub-modal');

    let vocab = { levels: [], common_logs: [], max_event_ids: 60, rollup_window_seconds: 300 };
    let canManage = false;
    let subscriptions = [];
    let editingId = null;
    let searchTimer = null;

    // Which tone each level is drawn in. The classes are components.css's existing status
    // pills rather than anything new -- this page adds no CSS, because hub self-update ships
    // hub/ as plain files and a deployed hub never runs the Tailwind build. Mirrors
    // events.LEVELS; presentation only, so drift costs a grey badge rather than an answer.
    const TONE = {
        critical: 'status-pill--danger',
        error: 'status-pill--danger',
        warning: 'status-pill--warn',
        information: 'status-pill--muted',
        verbose: 'status-pill--muted',
    };

    async function api(path, options) {
        const resp = await fetch(path, options);
        let body = null;
        try { body = await resp.json(); } catch (e) { /* an empty body is fine */ }
        if (!resp.ok) throw new Error((body && body.error) || t('events.request_failed'));
        return body;
    }

    function send(method, path, payload) {
        return api(path, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {}),
        });
    }

    function labelFor(name) {
        const found = (vocab.levels || []).find((entry) => entry.name === name);
        return found ? found.label : name;
    }

    // ------------------------------------------------------------------ query string

    function query() {
        const params = new URLSearchParams();
        if (searchInput.value.trim()) params.set('q', searchInput.value.trim());
        if (levelSelect.value) params.set('level', levelSelect.value);
        if (logSelect.value) params.set('log', logSelect.value);
        const text = params.toString();
        return text ? `?${text}` : '';
    }

    // ------------------------------------------------------------------ summary

    function renderSummary(summary) {
        // Hours rather than seconds: the knob is in seconds because that is what a setting
        // is, and "24 h" is what somebody reads.
        const hours = Math.round((summary.window_seconds || 0) / 3600);
        const cards = [
            [t('events.summary.occurrences'), summary.occurrences || 0],
            [t('events.summary.distinct'), summary.rows || 0],
            [t('events.summary.machines'), summary.machines || 0],
            [t('events.summary.errors'),
             ((summary.by_level || {}).error || 0) + ((summary.by_level || {}).critical || 0)],
        ];
        summaryBox.replaceChildren();
        cards.forEach(([label, value]) => {
            const card = el('div', 'stat-card');
            card.append(el('div', 'stat-card__label', label),
                        el('div', 'stat-card__value', String(value)));
            summaryBox.appendChild(card);
        });
        const note = el('p', 'stat-card__meta', t('events.summary.window', { hours }));
        note.style.gridColumn = '1 / -1';
        summaryBox.appendChild(note);
    }

    // ------------------------------------------------------------------ the event list

    function renderEvents(rows) {
        document.getElementById('count-records').textContent = String(rows.length);
        if (!rows.length) {
            // Two different empty states, because they call for different actions. Nothing
            // subscribed is a thing to go and fix; nothing matched is good news.
            listBox.replaceChildren(el('p', 'stat-card__meta',
                subscriptions.some((s) => s.enabled) ? t('events.none_matched')
                                                     : t('events.none_subscribed')));
            return;
        }
        const table = el('table', 'table');
        const head = el('tr');
        [t('events.col.when'), t('events.col.machine'), t('events.col.level'),
         t('events.col.source'), t('events.col.event'), t('events.col.message')]
            .forEach((label) => head.appendChild(el('th', null, label)));
        table.appendChild(el('thead')).appendChild(head);

        const body = el('tbody');
        rows.forEach((row) => {
            const tr = el('tr');

            // The LAST occurrence, with the first named underneath when they differ. A
            // rolled-up row spans a window, and showing only one end of it would make a
            // burst that started an hour ago look like one that just happened.
            const when = el('td');
            when.append(el('div', null, fmtTime(row.last_seen)));
            if (row.count > 1 && row.first_seen !== row.last_seen) {
                when.append(el('div', 'stat-card__meta',
                               t('events.since', { when: fmtTime(row.first_seen) })));
            }
            tr.appendChild(when);

            tr.appendChild(el('td', null, row.machine));

            const level = el('td');
            level.appendChild(el('span',
                `status-pill ${TONE[row.level] || 'status-pill--muted'}`,
                labelFor(row.level)));
            tr.appendChild(level);

            const source = el('td');
            source.append(el('div', null, row.log));
            if (row.provider) source.append(el('div', 'stat-card__meta', row.provider));
            tr.appendChild(source);

            // The count rides with the event id rather than getting a column of its own:
            // "4625 x412" is how somebody reads it, and a separate column of mostly-1s is
            // four characters of noise on every other row.
            const ident = el('td');
            ident.append(el('div', null, String(row.event_id)));
            if (row.count > 1) {
                ident.append(el('div', 'stat-card__meta',
                                t('events.times', { count: row.count })));
            }
            tr.appendChild(ident);

            tr.appendChild(el('td', null, row.message || '—'));
            body.appendChild(tr);
        });
        table.appendChild(body);
        listBox.replaceChildren(table);
    }

    // ------------------------------------------------------------------ subscriptions

    function describe(sub) {
        const ids = sub.event_ids.length ? sub.event_ids.join(', ') : t('events.sub.any_id');
        const levels = sub.levels.length ? sub.levels.map(labelFor).join(', ')
                                         : t('events.sub.any_level');
        return t('events.sub.summary', { ids, levels });
    }

    function renderSubscriptions() {
        document.getElementById('count-subs').textContent = String(subscriptions.length);
        if (!subscriptions.length) {
            subsPane.replaceChildren(el('p', 'stat-card__meta',
                canManage ? t('events.sub.none_manage') : t('events.sub.none')));
            return;
        }
        const table = el('table', 'table');
        const head = el('tr');
        const columns = [t('events.sub.col_name'), t('events.sub.col_log'),
                         t('events.sub.col_filter'), t('events.sub.col_state')];
        if (canManage) columns.push('');
        columns.forEach((label) => head.appendChild(el('th', null, label)));
        table.appendChild(el('thead')).appendChild(head);

        const body = el('tbody');
        subscriptions.forEach((sub) => {
            const tr = el('tr');
            const name = el('td');
            name.append(el('div', null, sub.name));
            if (sub.provider) name.append(el('div', 'stat-card__meta', sub.provider));
            tr.append(name, el('td', null, sub.log), el('td', null, describe(sub)));

            const state = el('td');
            state.appendChild(el('span',
                sub.enabled ? 'status-pill status-pill--ok' : 'status-pill status-pill--muted',
                sub.enabled ? t('events.sub.enabled') : t('events.sub.disabled')));
            tr.appendChild(state);

            if (canManage) {
                const cell = el('td');
                const bar = el('div', 'toolbar');
                const toggle = el('button', 'btn',
                                  sub.enabled ? t('events.sub.disable') : t('events.sub.enable'));
                toggle.type = 'button';
                toggle.addEventListener('click', () => setEnabled(sub, !sub.enabled));
                const edit = el('button', 'btn', t('common.edit'));
                edit.type = 'button';
                edit.addEventListener('click', () => openEditor(sub));
                const remove = el('button', 'btn', t('common.delete'));
                remove.type = 'button';
                remove.addEventListener('click', () => removeSubscription(sub));
                bar.append(toggle, edit, remove);
                cell.appendChild(bar);
                tr.appendChild(cell);
            }
            body.appendChild(tr);
        });
        table.appendChild(body);
        subsPane.replaceChildren(table);
    }

    async function setEnabled(sub, enabled) {
        try {
            await send('PATCH', `/api/events/subscriptions/${encodeURIComponent(sub.id)}`,
                       { enabled });
            await load();
        } catch (e) {
            toast(e.message, { kind: 'error' });
        }
    }

    async function removeSubscription(sub) {
        // The confirmation says what is kept as well as what goes: deleting a subscription
        // leaves the events it collected in place (see events.delete_subscription), and an
        // operator who thinks they are erasing a record would be stopped by the wrong fear.
        if (!window.confirm(t('events.sub.confirm_delete', { name: sub.name }))) return;
        try {
            await api(`/api/events/subscriptions/${encodeURIComponent(sub.id)}`,
                      { method: 'DELETE' });
            await load();
        } catch (e) {
            toast(e.message, { kind: 'error' });
        }
    }

    // ------------------------------------------------------------------ the editor

    function levelBoxes(selected) {
        const host = document.getElementById('sub-levels');
        host.replaceChildren();
        (vocab.levels || []).forEach((entry) => {
            const label = el('label', 'setting__label');
            label.style.marginRight = 'var(--space-4)';
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.value = entry.name;
            box.checked = (selected || []).includes(entry.name);
            label.append(box, document.createTextNode(` ${entry.label}`));
            host.appendChild(label);
        });
    }

    function openEditor(sub) {
        editingId = sub ? sub.id : null;
        document.getElementById('sub-modal-title').textContent =
            sub ? t('events.sub.edit_title') : t('events.sub.new_title');
        document.getElementById('sub-name').value = sub ? sub.name : '';
        document.getElementById('sub-log').value = sub ? sub.log : '';
        document.getElementById('sub-ids').value = sub ? sub.event_ids.join(', ') : '';
        document.getElementById('sub-provider').value = sub ? sub.provider : '';
        levelBoxes(sub ? sub.levels : []);

        const options = document.getElementById('sub-log-options');
        options.replaceChildren();
        (vocab.common_logs || []).forEach((name) => {
            const option = document.createElement('option');
            option.value = name;
            options.appendChild(option);
        });
        subModal.showModal();
    }

    function parseIds(text) {
        // Split on anything that is not a digit, so "4625, 4740" and "4625 4740" and a
        // pasted column all work. An operator typing event ids is copying them out of Event
        // Viewer, and insisting on one separator would be the console being fussy about the
        // one step somebody is doing by hand.
        return String(text || '').split(/[^0-9]+/).filter(Boolean).map(Number);
    }

    async function saveSubscription() {
        const levels = Array.from(document.querySelectorAll('#sub-levels input:checked'))
            .map((box) => box.value);
        const payload = {
            name: document.getElementById('sub-name').value.trim(),
            log: document.getElementById('sub-log').value.trim(),
            event_ids: parseIds(document.getElementById('sub-ids').value),
            levels,
            provider: document.getElementById('sub-provider').value.trim(),
        };
        try {
            if (editingId) {
                await send('PATCH',
                           `/api/events/subscriptions/${encodeURIComponent(editingId)}`,
                           payload);
            } else {
                await send('POST', '/api/events/subscriptions', payload);
            }
            subModal.close();
            await load();
        } catch (e) {
            toast(e.message, { kind: 'error' });
        }
    }

    // ------------------------------------------------------------------ filters

    function renderFilters(current) {
        if (!levelSelect.options.length) {
            const any = document.createElement('option');
            any.value = '';
            any.textContent = t('events.any_level');
            levelSelect.appendChild(any);
            (vocab.levels || []).forEach((entry) => {
                const option = document.createElement('option');
                option.value = entry.name;
                option.textContent = entry.label;
                levelSelect.appendChild(option);
            });
        }
        // The channel filter is built from the channels actually SUBSCRIBED, not from the
        // four Windows has: a filter offering Setup on a hub that has never collected a
        // Setup event is a control that can only ever produce an empty list.
        const logs = Array.from(new Set(subscriptions.map((s) => s.log))).sort();
        const keep = logSelect.value;
        logSelect.replaceChildren();
        const anyLog = document.createElement('option');
        anyLog.value = '';
        anyLog.textContent = t('events.any_log');
        logSelect.appendChild(anyLog);
        logs.forEach((name) => {
            const option = document.createElement('option');
            option.value = name;
            option.textContent = name;
            logSelect.appendChild(option);
        });
        if (logs.includes(keep)) logSelect.value = keep;
        if (current) levelSelect.value = current;
    }

    function renderActions() {
        actions.replaceChildren();
        const refresh = el('button', 'btn', t('events.refresh'));
        refresh.type = 'button';
        refresh.addEventListener('click', () => load());
        actions.appendChild(refresh);
        if (canManage) {
            const add = el('button', 'btn btn--primary', t('events.sub.add'));
            add.type = 'button';
            add.addEventListener('click', () => openEditor(null));
            actions.appendChild(add);
        }
    }

    // ------------------------------------------------------------------ load

    async function load() {
        let data;
        try {
            data = await api(`/api/events${query()}`);
        } catch (e) {
            listBox.replaceChildren(el('p', 'stat-card__meta', e.message));
            return;
        }
        vocab = data.vocabulary || vocab;
        canManage = !!data.can_manage;
        subscriptions = data.subscriptions || [];
        renderActions();
        renderFilters(levelSelect.value);
        renderSummary(data.summary || {});
        renderEvents(data.events || []);
        renderSubscriptions();
    }

    document.getElementById('sub-save').addEventListener('click', saveSubscription);
    document.getElementById('sub-cancel').addEventListener('click', () => subModal.close());
    levelSelect.addEventListener('change', load);
    logSelect.addEventListener('change', load);
    searchInput.addEventListener('input', () => {
        // Debounced: the search is a LIKE over the message column across the fleet, and
        // firing it per keystroke would put a scan on the hub for every letter of a
        // hostname.
        clearTimeout(searchTimer);
        searchTimer = setTimeout(load, 300);
    });

    load();
})();
