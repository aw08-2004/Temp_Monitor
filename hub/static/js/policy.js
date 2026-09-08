// App policy (roadmap #23 phase D): which apps a managed device may run.
//
// **Saving is gated on previewing, and that is the point of the page.** An app policy is the
// first thing in this product that can make a device less useful to the person holding it, and
// the gap between "block TikTok" and "block fourteen apps on ninety phones, including the
// camera" is exactly where somebody notices they picked the wrong list. So the Save button is
// disabled until a preview has been rendered for the current draft, and any edit disables it
// again. That is friction on purpose.
//
// **The preview is per machine, not a fleet total.** The total is the number that reassures;
// the per-device list is the one that stops somebody.
//
// **Protected packages are shown as protected rather than hidden.** An operator looking for the
// launcher has to find it and be told why it cannot be blocked -- a picker that simply does not
// contain it reads as a broken search.
(function () {
    'use strict';

    const listEl = document.getElementById('policy-list');
    if (!listEl) return;

    const editor = document.getElementById('policy-editor');
    const newButton = document.getElementById('policy-new');
    const nameEl = document.getElementById('policy-name');
    const modeEl = document.getElementById('policy-mode');
    const modeHelpEl = document.getElementById('policy-mode-help');
    const enabledEl = document.getElementById('policy-enabled');
    const fleetEl = document.getElementById('policy-fleet');
    const machinesWrap = document.getElementById('policy-machines-wrap');
    const machinesEl = document.getElementById('policy-machines');
    const machineFilterEl = document.getElementById('policy-machine-filter');
    const packagesEl = document.getElementById('policy-packages');
    const packageFilterEl = document.getElementById('policy-package-filter');
    const packageSystemEl = document.getElementById('policy-package-system');
    const packageCountEl = document.getElementById('policy-package-count');
    const previewButton = document.getElementById('policy-preview');
    const previewOut = document.getElementById('policy-preview-out');
    const saveButton = document.getElementById('policy-save');
    const cancelButton = document.getElementById('policy-cancel');
    const errorEl = document.getElementById('policy-error');

    let policies = [];
    let packages = [];
    let machines = [];
    let protectedPrefixes = [];
    let canManage = false;
    // The policy being edited, or null for a new one.
    let editing = null;
    const chosenPackages = new Set();
    const chosenMachines = new Set();

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function isProtected(name) {
        const lower = (name || '').toLowerCase();
        return protectedPrefixes.some((prefix) => lower.startsWith(prefix));
    }

    // ---------------------------------------------------------------- the list
    function renderList() {
        listEl.replaceChildren();
        if (policies.length === 0) {
            listEl.appendChild(el('p', 'stat-card__meta', t('policy.none')));
            return;
        }
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('policy.column.name'), t('policy.column.mode'), t('policy.column.packages'),
         t('policy.column.targets'), ''].forEach((label) =>
            headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        policies.forEach((policy) => {
            const row = el('tr');
            const name = el('td');
            name.appendChild(el('span', null, policy.name));
            if (!policy.enabled) {
                name.appendChild(document.createTextNode(' '));
                name.appendChild(el('span', 'stat-card__meta', t('policy.off')));
            }
            row.appendChild(name);
            // A switch of literal keys, not a computed one: tests/test_i18n.py scans literals.
            row.appendChild(el('td', null, policy.mode === 'allow'
                ? t('policy.mode.allow') : t('policy.mode.block')));
            row.appendChild(el('td', null, policy.packages.length));

            const targets = el('td', null, policy.fleet_wide
                ? t('policy.targets.fleet')
                : t('policy.targets.machines', { count: policy.machines.length }));
            // An operator scoped to three phones sees a policy covering ninety. Saying so
            // beats showing them three and letting them believe that is all of it.
            if (policy.machines_hidden) {
                targets.appendChild(document.createTextNode(' '));
                targets.appendChild(el('span', 'stat-card__meta',
                    t('policy.targets.hidden', { count: policy.machines_hidden })));
            }
            row.appendChild(targets);

            const actions = el('td');
            if (canManage) {
                const edit = el('button', 'btn btn--ghost', t('policy.edit'));
                edit.type = 'button';
                edit.addEventListener('click', () => openEditor(policy));
                actions.appendChild(edit);
                const remove = el('button', 'btn btn--ghost', t('policy.delete'));
                remove.type = 'button';
                remove.addEventListener('click', () => destroy(policy));
                actions.appendChild(remove);
            }
            row.appendChild(actions);
            body.appendChild(row);
        });
        table.appendChild(body);
        listEl.appendChild(table);
    }

    // ---------------------------------------------------------------- pickers
    function pickerRow(value, label, checked, onToggle, note) {
        const row = el('label', 'policy-picker__row');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.className = 'checkbox';
        box.checked = checked;
        box.disabled = Boolean(note);
        box.addEventListener('change', () => { onToggle(box.checked); draftChanged(); });
        row.appendChild(box);
        row.appendChild(el('span', 'policy-picker__label', label));
        row.appendChild(el('span', 'policy-picker__meta', value));
        if (note) row.appendChild(el('span', 'policy-picker__meta', note));
        return row;
    }

    function renderPackages() {
        const needle = (packageFilterEl.value || '').trim().toLowerCase();
        const rows = packages.filter((p) => {
            if (!packageSystemEl.checked && p.is_system) return false;
            if (!needle) return true;
            return (p.label || '').toLowerCase().includes(needle)
                || (p.package || '').toLowerCase().includes(needle);
        });
        packageCountEl.textContent = t('policy.package_count', {
            chosen: chosenPackages.size, shown: rows.length,
        });
        packagesEl.replaceChildren();
        if (rows.length === 0) {
            packagesEl.appendChild(el('p', 'stat-card__meta',
                packages.length ? t('policy.no_package_match') : t('policy.no_packages')));
            return;
        }
        rows.forEach((p) => {
            packagesEl.appendChild(pickerRow(
                p.package,
                `${p.label} (${t('policy.on_machines', { count: p.machines })})`,
                chosenPackages.has(p.package),
                (on) => { if (on) chosenPackages.add(p.package); else chosenPackages.delete(p.package); },
                // Shown and disabled rather than filtered out: somebody looking for the
                // launcher has to find it and be told why, or the picker reads as broken.
                isProtected(p.package) ? t('policy.protected') : ''));
        });
    }

    function renderMachines() {
        machinesWrap.hidden = fleetEl.checked;
        const needle = (machineFilterEl.value || '').trim().toLowerCase();
        const rows = machines.filter((m) => !needle || m.toLowerCase().includes(needle));
        machinesEl.replaceChildren();
        rows.forEach((machine) => {
            machinesEl.appendChild(pickerRow(
                machine, machine, chosenMachines.has(machine),
                (on) => { if (on) chosenMachines.add(machine); else chosenMachines.delete(machine); }));
        });
    }

    // ---------------------------------------------------------------- the editor
    function modeHelp() {
        modeHelpEl.textContent = modeEl.value === 'allow'
            ? t('policy.mode.allow_help') : t('policy.mode.block_help');
    }

    function openEditor(policy) {
        editing = policy || null;
        nameEl.value = policy ? policy.name : '';
        modeEl.value = policy ? policy.mode : 'block';
        enabledEl.checked = policy ? policy.enabled : true;
        fleetEl.checked = policy ? policy.fleet_wide : false;
        chosenPackages.clear();
        chosenMachines.clear();
        (policy ? policy.packages : []).forEach((p) => chosenPackages.add(p));
        (policy ? policy.machines : []).forEach((m) => chosenMachines.add(m));
        errorEl.textContent = '';
        previewOut.replaceChildren();
        modeHelp();
        renderPackages();
        renderMachines();
        draftChanged();
        editor.hidden = false;
        nameEl.focus();
    }

    /** Any edit invalidates the preview, and with it the right to save. */
    function draftChanged() {
        saveButton.disabled = true;
        previewOut.replaceChildren();
    }

    function draft() {
        return {
            name: nameEl.value,
            mode: modeEl.value,
            enabled: enabledEl.checked,
            fleet_wide: fleetEl.checked,
            packages: [...chosenPackages],
            machines: [...chosenMachines],
        };
    }

    // ---------------------------------------------------------------- preview
    function renderPreview(data) {
        previewOut.replaceChildren();

        if (data.dropped_protected && data.dropped_protected.length) {
            // Named, never silently dropped: an operator who ticked the launcher has to be
            // told, or they will believe the policy covers something it does not.
            previewOut.appendChild(el('p', 'banner banner--warn',
                t('policy.preview.dropped', { packages: data.dropped_protected.join(', ') })));
        }

        const affected = data.machines.filter((m) => m.would_suspend.length > 0);
        previewOut.appendChild(el('p', 'stat-card__meta', t('policy.preview.summary', {
            machines: affected.length, total: data.machines.length,
        })));
        if (data.machines_hidden) {
            previewOut.appendChild(el('p', 'stat-card__meta',
                t('policy.preview.hidden', { count: data.machines_hidden })));
        }
        if (affected.length === 0) {
            previewOut.appendChild(el('p', 'stat-card__meta', t('policy.preview.nothing')));
            return;
        }

        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('policy.preview.machine'), t('policy.preview.would_suspend')].forEach((label) =>
            headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);
        const body = el('tbody');
        affected.forEach((entry) => {
            const row = el('tr');
            row.appendChild(el('td', null, entry.machine));
            // Labels, not package names: a list of reverse-DNS is not something anybody can
            // check against what they meant.
            const names = entry.would_suspend.map((p) => entry.labels[p] || p);
            row.appendChild(el('td', null,
                `${names.length} - ${names.slice(0, 12).join(', ')}`
                + (names.length > 12 ? ' ...' : '')));
            body.appendChild(row);
        });
        table.appendChild(body);
        previewOut.appendChild(table);
    }

    async function preview() {
        errorEl.textContent = '';
        let response;
        try {
            response = await fetch('/api/policy/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(draft()),
            });
        } catch (e) {
            errorEl.textContent = t('policy.unreachable');
            return;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            // Rendered verbatim: policy.py names the specific refusal (a blocklist with no
            // packages, a policy with no target), and a generic message would throw away the
            // only part that helps.
            errorEl.textContent = data.error || t('policy.failed');
            return;
        }
        renderPreview(data);
        saveButton.disabled = false;
    }

    // ---------------------------------------------------------------- writes
    async function save() {
        errorEl.textContent = '';
        const url = editing ? `/api/policy/apps/${encodeURIComponent(editing.id)}`
                            : '/api/policy/apps';
        let response;
        try {
            response = await fetch(url, {
                method: editing ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(draft()),
            });
        } catch (e) {
            errorEl.textContent = t('policy.unreachable');
            return;
        }
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            errorEl.textContent = data.error || t('policy.failed');
            return;
        }
        editor.hidden = true;
        editing = null;
        await load();
    }

    async function destroy(policy) {
        if (!window.confirm(t('policy.confirm_delete', { name: policy.name }))) return;
        try {
            await fetch(`/api/policy/apps/${encodeURIComponent(policy.id)}`,
                        { method: 'DELETE' });
        } catch (e) {
            return;
        }
        await load();
    }

    // ---------------------------------------------------------------- load
    async function load() {
        const [policyResp, packageResp, machineResp] = await Promise.all([
            fetch('/api/policy/apps').catch(() => null),
            fetch('/api/apps/packages').catch(() => null),
            fetch('/api/machines').catch(() => null),
        ]);
        if (!policyResp || !policyResp.ok) return;

        const data = await policyResp.json();
        policies = data.policies || [];
        canManage = Boolean(data.can_manage);
        protectedPrefixes = (data.protected_prefixes || []).map((p) => p.toLowerCase());
        newButton.hidden = !canManage;

        if (packageResp && packageResp.ok) {
            packages = (await packageResp.json()).packages || [];
        }
        if (machineResp && machineResp.ok) {
            machines = ((await machineResp.json()) || []).map((m) => m.machine).sort();
        }
        renderList();
    }

    newButton.addEventListener('click', () => openEditor(null));
    cancelButton.addEventListener('click', () => { editor.hidden = true; editing = null; });
    previewButton.addEventListener('click', preview);
    saveButton.addEventListener('click', save);
    [nameEl, modeEl, enabledEl].forEach((node) =>
        node.addEventListener('change', draftChanged));
    nameEl.addEventListener('input', draftChanged);
    modeEl.addEventListener('change', modeHelp);
    fleetEl.addEventListener('change', () => { renderMachines(); draftChanged(); });
    packageFilterEl.addEventListener('input', renderPackages);
    packageSystemEl.addEventListener('change', renderPackages);
    machineFilterEl.addEventListener('input', renderMachines);

    load();
})();
