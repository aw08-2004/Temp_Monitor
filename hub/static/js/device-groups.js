// Device groups page (hub 1.114.0): the list of saved target filters, and their editor.
//
// A group's definition is the same include/exclude spec as a rule's target, so the editor is
// deliberately the same shape as the rules editor's target section (rules.js selectorRow):
// someone who has written one rule can write a group without learning a second grammar. It is
// not SHARED code with rules.js, because rules.js is a page script with its own globals and no
// module system to import from; the two are small enough that a shared helper would cost more
// in coupling than the duplication does.
//
// Built with createElement/textContent throughout: group names are operator text, and machine
// names in a definition are whatever a host reported to the unauthenticated /api/report.
(function () {
    'use strict';

    const root = document.getElementById('device-groups-root');
    if (!root) return;

    const CAN_MANAGE = root.dataset.canManage === '1';
    const body = document.getElementById('device-groups-body');
    const empty = document.getElementById('device-groups-empty');
    const dialog = document.getElementById('device-group-dialog');

    let editingId = null;
    let draft = null;
    let previewTimer = null;

    async function api(path, options) {
        const resp = await fetch(path, options);
        let data = null;
        try { data = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
        return data;
    }

    function json(method, payload) {
        // Content-Type: application/json is load-bearing, not cosmetic -- it is what makes a
        // cross-origin POST preflight and fail. See fleet_web.py's module docstring.
        return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    // Literal keys, not t(`rules.target.${kind}`): tests/test_i18n.py only sees literals, and a
    // computed key for a kind nobody translated would render itself.
    function kindLabel(kind) {
        switch (kind) {
            case 'all': return t('rules.target.all');
            case 'machines': return t('rules.target.machines');
            case 'ad_ou': return t('rules.target.ad_ou');
            case 'field': return t('rules.target.field');
            default: return kind;
        }
    }

    function describeSelector(selector) {
        switch (selector.kind) {
            case 'machines': return `${kindLabel('machines')}: ${(selector.machines || []).join(', ')}`;
            case 'ad_ou': return `${kindLabel('ad_ou')}: ${selector.ou || ''}`;
            case 'field': return `${kindLabel('field')}: ${selector.field} = ${selector.value}`;
            default: return kindLabel(selector.kind);
        }
    }

    function describe(target) {
        const include = (target.include || []).map(describeSelector).join('; ');
        const exclude = (target.exclude || []).map(describeSelector).join('; ');
        return exclude ? `${include} — ${t('device_groups.except')} ${exclude}` : include;
    }

    // ---------------------------------------------------------------- the list
    function renderList(groups) {
        body.replaceChildren();
        empty.style.display = groups.length ? 'none' : 'block';
        for (const group of groups) {
            const tr = el('tr');

            const nameTd = el('td');
            nameTd.appendChild(el('div', null, group.name));
            if (group.description) nameTd.appendChild(el('div', 'stat-card__meta', group.description));

            const defTd = el('td', 'stat-card__meta', describe(group.target));
            const countTd = el('td', null,
                group.count === null ? '--' : tPlural('device_groups.members', group.count));
            const usedTd = el('td', 'stat-card__meta',
                (group.used_by || []).map((r) => r.name).join(', ') || '--');

            const actions = el('td', 'data-table__actions');
            if (CAN_MANAGE) {
                const edit = el('button', 'btn btn--ghost', t('device_groups.edit'));
                edit.type = 'button';
                edit.addEventListener('click', () => openEditor(group));
                const remove = el('button', 'btn btn--ghost', t('device_groups.delete'));
                remove.type = 'button';
                remove.style.color = 'var(--danger)';
                remove.addEventListener('click', () => removeGroup(group));
                actions.append(edit, remove);
            }
            tr.append(nameTd, defTd, countTd, usedTd, actions);
            body.appendChild(tr);
        }
    }

    async function load() {
        try {
            const data = await api('/api/device-groups');
            renderList(data.groups || []);
        } catch (e) {
            const tr = el('tr');
            const td = el('td', 'stat-card__meta', `${t('device_groups.load_failed')} ${e.message}`);
            td.colSpan = 5;
            tr.appendChild(td);
            body.replaceChildren(tr);
        }
    }

    async function removeGroup(group) {
        if (!window.confirm(t('device_groups.confirm_delete', { name: group.name }))) return;
        try {
            await api(`/api/device-groups/${group.id}`, { method: 'DELETE' });
            load();
        } catch (e) {
            // A 409 names the rules still aimed at the group; that sentence is the whole answer.
            toast(e.message, { kind: 'error' });
        }
    }

    // ---------------------------------------------------------------- the editor
    if (!CAN_MANAGE || !dialog) {
        load();
        return;
    }

    const errorEl = document.getElementById('device-group-error');
    const countEl = document.getElementById('device-group-count');

    function fillKinds() {
        for (const [id, side] of [['device-group-include-kind', 'include'], ['device-group-exclude-kind', 'exclude']]) {
            const select = document.getElementById(id);
            select.replaceChildren();
            for (const kind of ['all', 'machines', 'ad_ou', 'field']) {
                // "Every PC" as an exclusion would exclude everything -- same call rules.js makes.
                if (side === 'exclude' && kind === 'all') continue;
                const option = el('option', null, kindLabel(kind));
                option.value = kind;
                select.appendChild(option);
            }
        }
    }

    function input(value, placeholder, onChange, width) {
        const node = el('input', 'input');
        node.type = 'text';
        node.value = value;
        node.placeholder = placeholder;
        if (width) node.style.minWidth = width;
        node.addEventListener('change', () => { onChange(node.value); schedulePreview(); });
        return node;
    }

    function selectorRow(side, selector, index) {
        const row = el('div', 'toolbar');
        row.style.marginBottom = 'var(--space-2)';
        row.appendChild(el('span', 'stat-card__meta', kindLabel(selector.kind)));

        if (selector.kind === 'machines') {
            row.appendChild(input((selector.machines || []).join(', '), 'PC-1, PC-2', (v) => {
                selector.machines = v.split(',').map((s) => s.trim()).filter(Boolean);
            }, '280px'));
        } else if (selector.kind === 'ad_ou') {
            row.appendChild(input(selector.ou || '', 'OU=Sales,DC=corp', (v) => { selector.ou = v.trim(); }, '280px'));
            const label = el('label', 'stat-card__meta');
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.checked = selector.include_children !== false;
            box.addEventListener('change', () => { selector.include_children = box.checked; schedulePreview(); });
            label.append(box, document.createTextNode(' ' + t('device_groups.children')));
            row.appendChild(label);
        } else if (selector.kind === 'field') {
            row.appendChild(input(selector.field || '', 'location', (v) => { selector.field = v.trim(); }));
            row.appendChild(input(selector.value === undefined ? '' : String(selector.value), '', (v) => { selector.value = v; }));
        }

        const remove = el('button', 'btn btn--ghost', '×');
        remove.type = 'button';
        remove.addEventListener('click', () => {
            draft.target[side].splice(index, 1);
            renderSelectors();
            schedulePreview();
        });
        row.appendChild(remove);
        return row;
    }

    function renderSelectors() {
        for (const side of ['include', 'exclude']) {
            const host = document.getElementById(`device-group-${side}`);
            host.replaceChildren(...draft.target[side].map((s, i) => selectorRow(side, s, i)));
        }
    }

    function schedulePreview() {
        clearTimeout(previewTimer);
        previewTimer = setTimeout(preview, 250);
    }

    async function preview() {
        if (!draft) return;
        try {
            const data = await api('/api/device-groups/preview', json('POST', { target: draft.target }));
            countEl.textContent = tPlural('device_groups.members', data.count);
            countEl.title = data.machines.slice(0, 50).join(', ');
        } catch (e) {
            countEl.textContent = e.message;
            countEl.title = '';
        }
    }

    function openEditor(group) {
        editingId = group ? group.id : null;
        draft = {
            name: group ? group.name : '',
            description: group ? group.description : '',
            target: group
                ? JSON.parse(JSON.stringify(group.target))
                : { include: [{ kind: 'all' }], exclude: [] },
        };
        draft.target.include = draft.target.include || [];
        draft.target.exclude = draft.target.exclude || [];
        document.getElementById('device-group-title').textContent = group
            ? t('device_groups.editor_edit', { name: group.name })
            : t('device_groups.editor_new');
        document.getElementById('device-group-name').value = draft.name;
        document.getElementById('device-group-description').value = draft.description;
        errorEl.textContent = '';
        countEl.textContent = '';
        renderSelectors();
        preview();
        dialog.showModal();
    }

    for (const side of ['include', 'exclude']) {
        document.getElementById(`device-group-${side}-add`).addEventListener('click', () => {
            const kind = document.getElementById(`device-group-${side}-kind`).value;
            const selector = { kind };
            if (kind === 'machines') selector.machines = [];
            if (kind === 'ad_ou') { selector.ou = ''; selector.include_children = true; }
            if (kind === 'field') { selector.field = ''; selector.value = ''; }
            draft.target[side].push(selector);
            renderSelectors();
            schedulePreview();
        });
    }

    document.getElementById('device-group-cancel').addEventListener('click', () => dialog.close());
    document.getElementById('device-group-new').addEventListener('click', () => openEditor(null));
    document.getElementById('device-group-save').addEventListener('click', async () => {
        errorEl.textContent = '';
        const payload = {
            name: document.getElementById('device-group-name').value,
            description: document.getElementById('device-group-description').value,
            target: draft.target,
        };
        try {
            if (editingId === null) {
                await api('/api/device-groups', json('POST', payload));
            } else {
                await api(`/api/device-groups/${editingId}`, json('PUT', payload));
            }
            dialog.close();
            load();
        } catch (e) {
            errorEl.textContent = e.message;
        }
    });

    fillKinds();
    load();
})();
