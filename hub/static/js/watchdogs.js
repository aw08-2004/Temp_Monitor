// Watchdogs page (roadmap #20): which service must stay running on which PCs, and what the
// fleet has actually been doing about it.
//
// Two house rules, the same ones rules.js and packages.js follow and for the same reasons:
//
//  * Everything is built with textContent / createElement, never innerHTML. Hostnames,
//    service names and the detail line an agent sends are all arbitrary strings that came
//    from operators or from machines.
//  * The vocabularies -- the selector kinds, the bounds on the flap limits, the list of
//    services this hub refuses -- come from GET /api/watchdogs, not from a copy here. A
//    hardcoded bound silently disagrees with the server's the day somebody widens it, and
//    that reads to an operator as "the page is broken".
//
// **Most of this file is the state table, not the editor, and that is the point.** A watchdog
// that is working does nothing observable, so the only way to tell a healthy fleet from a
// feature that never reached a single machine is to show what the machines are reporting.
// `reporting` counts that; a watchdog with machines targeted and none reporting is the
// failure this page exists to make visible.
(function () {
    'use strict';

    var listBody = document.getElementById('watchdogs-body');
    var listEmpty = document.getElementById('watchdogs-empty');
    var listCount = document.getElementById('watchdogs-count');
    var editor = document.getElementById('watchdog-editor');
    var readOnly = document.getElementById('watchdogs-read-only');
    var statePanel = document.getElementById('watchdog-state');
    var eventsBody = document.getElementById('watchdog-events-body');
    var eventsEmpty = document.getElementById('watchdog-events-empty');

    var canManage = false;
    var limits = null;
    var deviceGroups = [];
    var editingId = null;
    // The editor's working copy, kept as data rather than read back out of the DOM so that
    // adding a selector never loses a half-typed service name.
    var draft = null;
    var openStateFor = null;

    // The selector kinds this page offers. Taken from the server's target vocabulary rather
    // than hardcoded, except for the ORDER, which is a UI decision.
    var SELECTOR_KINDS = ['all', 'machines', 'group', 'ad_ou', 'field'];

    async function api(path, options) {
        var resp = await fetch(path, options);
        var body = null;
        try { body = await resp.json(); } catch (e) { /* an empty body is fine */ }
        if (!resp.ok) throw new Error((body && body.error) || ('HTTP ' + resp.status));
        return body;
    }

    function json(method, payload) {
        // Content-Type: application/json is load-bearing, not cosmetic -- it is what makes a
        // cross-origin POST preflight and fail. See fleet_web.py's module docstring.
        return {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        };
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function opt(value, label) {
        var o = document.createElement('option');
        o.value = value;
        o.textContent = label;
        return o;
    }

    function epoch(value) {
        return value ? new Date(value * 1000).toLocaleString() : '--';
    }

    // The status words come from the catalog, so a status this build has never heard of shows
    // the raw word rather than an empty cell. An agent newer than the console is a real state
    // -- the deploy order is hub first, then agent, but a beta-channel machine can be ahead.
    function statusLabel(status) {
        var key = `watchdogs.status_name.${status}`;
        var label = t(key);
        return label === key ? status : label;
    }

    // ---------------------------------------------------------------- the list

    function renderList(rows) {
        listBody.textContent = '';
        listEmpty.hidden = rows.length > 0;
        listCount.textContent = rows.length ? t('watchdogs.count', { count: rows.length }) : '';
        rows.forEach(function (row) {
            var tr = document.createElement('tr');
            if (!row.enabled) tr.style.opacity = '0.55';

            var name = el('td');
            name.appendChild(el('div', null, row.name));
            if (row.description) name.appendChild(el('div', 'stat-card__meta', row.description));
            tr.appendChild(name);

            tr.appendChild(el('td', null, row.service));
            tr.appendChild(el('td', 'stat-card__meta', t('watchdogs.flap_summary', {
                count: row.max_restarts,
                minutes: Math.round(row.window_seconds / 60)
            })));

            // Reporting vs targeted is the honest health number, so it is a cell of its own
            // rather than a footnote: nobody reporting means the document never landed.
            tr.appendChild(el('td', 'stat-card__meta',
                              t('watchdogs.reporting_count', { count: row.machines })));

            var trouble = el('td', 'stat-card__meta');
            if (row.trouble) {
                trouble.textContent = t('watchdogs.trouble_count', { count: row.trouble });
                trouble.style.color = 'var(--color-danger, #f85149)';
            } else if (row.missing) {
                trouble.textContent = t('watchdogs.missing_count', { count: row.missing });
                trouble.style.color = 'var(--color-warning, #d29922)';
            } else {
                trouble.textContent = '--';
            }
            tr.appendChild(trouble);

            var actions = el('td');
            var state = el('button', 'btn btn--ghost', t('watchdogs.show_state'));
            state.type = 'button';
            state.addEventListener('click', function () { showState(row); });
            actions.appendChild(state);
            if (canManage) {
                var edit = el('button', 'btn btn--ghost', t('watchdogs.edit'));
                edit.type = 'button';
                edit.addEventListener('click', function () { openEditor(row); });
                actions.appendChild(edit);

                var toggle = el('button', 'btn btn--ghost',
                                row.enabled ? t('watchdogs.disable') : t('watchdogs.enable'));
                toggle.type = 'button';
                toggle.addEventListener('click', async function () {
                    try {
                        await api('/api/watchdogs/' + row.id + '/enabled',
                                  json('PUT', { enabled: !row.enabled }));
                        await load();
                    } catch (e) { window.alert(e.message); }
                });
                actions.appendChild(toggle);

                var remove = el('button', 'btn btn--ghost', t('watchdogs.delete'));
                remove.type = 'button';
                remove.addEventListener('click', async function () {
                    if (!window.confirm(t('watchdogs.delete_confirm', { name: row.name }))) return;
                    try {
                        await api('/api/watchdogs/' + row.id, { method: 'DELETE' });
                        if (openStateFor === row.id) { statePanel.hidden = true; openStateFor = null; }
                        await load();
                    } catch (e) { window.alert(e.message); }
                });
                actions.appendChild(remove);
            }
            tr.appendChild(actions);
            listBody.appendChild(tr);
        });
    }

    // ---------------------------------------------------------------- state

    async function showState(row) {
        openStateFor = row.id;
        statePanel.hidden = false;
        document.getElementById('watchdog-state-title').textContent =
            t('watchdogs.state_title', { name: row.name, service: row.service });
        var body = document.getElementById('watchdog-state-body');
        body.textContent = '';
        var data;
        try {
            data = await api('/api/watchdogs/' + row.id);
        } catch (e) {
            document.getElementById('watchdog-state-empty').hidden = false;
            document.getElementById('watchdog-state-empty').textContent = e.message;
            return;
        }
        var states = data.state || [];
        document.getElementById('watchdog-state-empty').hidden = states.length > 0;
        document.getElementById('watchdog-state-empty').textContent = t('watchdogs.state_empty');
        states.forEach(function (state) {
            var tr = document.createElement('tr');
            tr.appendChild(el('td', null, state.machine));
            var status = el('td', null, statusLabel(state.status));
            if (state.status === 'given_up' || state.status === 'failed') {
                status.style.color = 'var(--color-danger, #f85149)';
            } else if (state.status === 'missing') {
                status.style.color = 'var(--color-warning, #d29922)';
            }
            tr.appendChild(status);
            tr.appendChild(el('td', 'stat-card__meta', String(state.restarts || 0)));
            tr.appendChild(el('td', 'stat-card__meta', epoch(state.reported_at)));
            tr.appendChild(el('td', 'stat-card__meta', state.detail || ''));
            body.appendChild(tr);
        });
        statePanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // ---------------------------------------------------------------- history

    function renderEvents(events) {
        eventsBody.textContent = '';
        eventsEmpty.hidden = events.length > 0;
        events.forEach(function (event) {
            var tr = document.createElement('tr');
            tr.appendChild(el('td', 'stat-card__meta', epoch(event.at)));
            tr.appendChild(el('td', null, event.machine));
            tr.appendChild(el('td', null, event.name || ''));
            tr.appendChild(el('td', null, statusLabel(event.status)));
            tr.appendChild(el('td', 'stat-card__meta', event.detail || ''));
            eventsBody.appendChild(tr);
        });
    }

    // ---------------------------------------------------------------- targets

    function renderTargets() {
        ['include', 'exclude'].forEach(function (side) {
            var host = document.getElementById(
                side === 'include' ? 'watchdog-target-include' : 'watchdog-target-exclude');
            host.textContent = '';
            draft.target[side] = draft.target[side] || [];
            draft.target[side].forEach(function (selector, index) {
                host.appendChild(selectorRow(side, selector, index));
            });
        });
    }

    function selectorRow(side, selector, index) {
        var row = el('div', 'toolbar');
        row.style.marginBottom = 'var(--space-2)';
        row.appendChild(el('span', 'stat-card__meta', t(`rules.target.${selector.kind}`)));

        if (selector.kind === 'machines') {
            var machines = el('input', 'input');
            machines.type = 'text';
            machines.value = (selector.machines || []).join(', ');
            machines.placeholder = 'PC-1, PC-2';
            machines.style.minWidth = '320px';
            machines.addEventListener('change', function () {
                selector.machines = machines.value.split(',').map(function (s) {
                    return s.trim();
                }).filter(Boolean);
                refreshTargetCount();
            });
            row.appendChild(machines);
        } else if (selector.kind === 'ad_ou') {
            var ou = el('input', 'input');
            ou.type = 'text';
            ou.value = selector.ou || '';
            ou.placeholder = 'OU=Sales,DC=corp';
            ou.style.minWidth = '320px';
            ou.addEventListener('change', function () {
                selector.ou = ou.value.trim();
                refreshTargetCount();
            });
            row.appendChild(ou);
            var label = el('label', 'stat-card__meta');
            var box = document.createElement('input');
            box.type = 'checkbox';
            box.checked = selector.include_children !== false;
            box.addEventListener('change', function () {
                selector.include_children = box.checked;
                refreshTargetCount();
            });
            label.appendChild(box);
            label.appendChild(document.createTextNode(' ' + t('rules.include_children')));
            row.appendChild(label);
        } else if (selector.kind === 'group') {
            if (!deviceGroups.length) {
                row.appendChild(el('span', 'stat-card__meta', t('device_groups.none_defined')));
            } else {
                var select = el('select', 'input');
                deviceGroups.forEach(function (group) {
                    select.appendChild(opt(String(group.id), group.name));
                });
                if (selector.group_id == null) selector.group_id = deviceGroups[0].id;
                select.value = String(selector.group_id);
                select.addEventListener('change', function () {
                    selector.group_id = Number(select.value);
                    refreshTargetCount();
                });
                row.appendChild(select);
            }
        } else if (selector.kind === 'field') {
            var name = el('input', 'input');
            name.type = 'text';
            name.value = selector.field || '';
            name.placeholder = 'location';
            name.addEventListener('change', function () {
                selector.field = name.value.trim();
                refreshTargetCount();
            });
            row.appendChild(name);
            var value = el('input', 'input');
            value.type = 'text';
            value.value = selector.value === undefined ? '' : String(selector.value);
            value.addEventListener('change', function () {
                selector.value = value.value;
                refreshTargetCount();
            });
            row.appendChild(value);
        }

        var remove = el('button', 'btn btn--ghost', '×');
        remove.type = 'button';
        remove.addEventListener('click', function () {
            draft.target[side].splice(index, 1);
            renderTargets();
            refreshTargetCount();
        });
        row.appendChild(remove);
        return row;
    }

    // Counted by the SERVER, against the caller's own scope, rather than by filtering a list
    // here: a scoped operator must see the number their watchdog will actually reach, and the
    // browser has no way to know which machines those are.
    async function refreshTargetCount() {
        var label = document.getElementById('watchdog-target-count');
        try {
            var data = await api('/api/rules/targets', json('POST', { target: draft.target }));
            label.textContent = t('watchdogs.targets', { count: data.count });
            if (data.machines && data.machines.length) {
                label.title = data.machines.slice(0, 50).join(', ');
            }
        } catch (e) {
            label.textContent = e.message;
        }
    }

    // ---------------------------------------------------------------- the editor

    function openEditor(row) {
        editingId = row ? row.id : null;
        draft = {
            target: (row && row.target) || { include: [{ kind: 'all' }], exclude: [] }
        };
        document.getElementById('watchdog-name').value = (row && row.name) || '';
        document.getElementById('watchdog-service').value = (row && row.service) || '';
        document.getElementById('watchdog-description').value = (row && row.description) || '';
        document.getElementById('watchdog-enabled').checked = row ? !!row.enabled : true;
        document.getElementById('watchdog-grace').value = row ? row.grace_seconds : 60;
        document.getElementById('watchdog-max-restarts').value = row ? row.max_restarts : 3;
        document.getElementById('watchdog-window').value = row ? row.window_seconds : 3600;
        document.getElementById('watchdog-save-error').textContent = '';
        editor.hidden = false;
        renderTargets();
        refreshTargetCount();
        editor.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    function closeEditor() {
        editor.hidden = true;
        editingId = null;
        draft = null;
    }

    async function save() {
        var payload = {
            name: document.getElementById('watchdog-name').value,
            service: document.getElementById('watchdog-service').value,
            description: document.getElementById('watchdog-description').value,
            enabled: document.getElementById('watchdog-enabled').checked,
            target: draft.target,
            grace_seconds: Number(document.getElementById('watchdog-grace').value),
            max_restarts: Number(document.getElementById('watchdog-max-restarts').value),
            window_seconds: Number(document.getElementById('watchdog-window').value)
        };
        var errorEl = document.getElementById('watchdog-save-error');
        errorEl.textContent = '';
        try {
            if (editingId) {
                await api('/api/watchdogs/' + editingId, json('PUT', payload));
            } else {
                await api('/api/watchdogs', json('POST', payload));
            }
        } catch (e) {
            // Verbatim: the server's validation text is written out of this operator's own
            // input ("'RpcSs' is one of the services this hub will not watch"), and replacing
            // it with "invalid" is how an editor becomes unusable. See watchdogs_web.py.
            errorEl.textContent = e.message;
            return;
        }
        closeEditor();
        await load();
    }

    // ---------------------------------------------------------------- load

    async function load() {
        var data;
        try {
            data = await api('/api/watchdogs');
        } catch (e) {
            listEmpty.hidden = false;
            listEmpty.textContent = e.message;
            return;
        }
        canManage = !!data.can_manage;
        limits = data.limits || {};
        document.getElementById('watchdog-new').hidden = !canManage;
        // Only shown to somebody who is short exactly one of the two capabilities. Somebody
        // with neither is not being told about a door they cannot see anyway.
        readOnly.hidden = canManage || !data.can_manage_rules;
        if (limits.grace_seconds) {
            document.getElementById('watchdog-grace').min = limits.grace_seconds[0];
            document.getElementById('watchdog-grace').max = limits.grace_seconds[1];
        }
        if (limits.max_restarts) {
            document.getElementById('watchdog-max-restarts').min = limits.max_restarts[0];
            document.getElementById('watchdog-max-restarts').max = limits.max_restarts[1];
        }
        if (limits.window_seconds) {
            document.getElementById('watchdog-window').min = limits.window_seconds[0];
            document.getElementById('watchdog-window').max = limits.window_seconds[1];
        }
        renderList(data.watchdogs || []);
        if (openStateFor) {
            var still = (data.watchdogs || []).filter(function (w) { return w.id === openStateFor; });
            if (still.length) showState(still[0]); else statePanel.hidden = true;
        }
        try {
            var history = await api('/api/watchdogs/events');
            renderEvents(history.events || []);
        } catch (e) {
            eventsEmpty.hidden = false;
            eventsEmpty.textContent = e.message;
        }
    }

    async function loadGroups() {
        // Never fatal: a hub with no device groups, or one whose groups endpoint fails, still
        // has four other selector kinds and a perfectly usable page.
        try {
            var data = await api('/api/device-groups');
            deviceGroups = data.groups || [];
        } catch (e) {
            deviceGroups = [];
        }
    }

    ['watchdog-target-kind', 'watchdog-exclude-kind'].forEach(function (id) {
        var select = document.getElementById(id);
        SELECTOR_KINDS.forEach(function (kind) {
            select.appendChild(opt(kind, t(`rules.target.${kind}`)));
        });
    });

    ['watchdog-target-add', 'watchdog-exclude-add'].forEach(function (id) {
        document.getElementById(id).addEventListener('click', function () {
            if (!draft) return;
            var side = id === 'watchdog-target-add' ? 'include' : 'exclude';
            var kind = document.getElementById(
                side === 'include' ? 'watchdog-target-kind' : 'watchdog-exclude-kind').value;
            draft.target[side] = draft.target[side] || [];
            draft.target[side].push({ kind: kind });
            renderTargets();
            refreshTargetCount();
        });
    });

    document.getElementById('watchdog-new').addEventListener('click', function () { openEditor(null); });
    document.getElementById('watchdog-cancel').addEventListener('click', closeEditor);
    document.getElementById('watchdog-save').addEventListener('click', save);

    loadGroups().then(load);
    // The state a machine reports arrives on its heartbeat, so this page goes stale on its own
    // within seconds. Polled rather than pushed, like inventory.js: this is not a page anybody
    // sits on, and a socket for it would be a channel to keep alive for nothing.
    window.setInterval(load, 30000);
}());
