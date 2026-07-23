// Permission Groups admin page.
//
// The whole page is one list + one <dialog> editor. Two rules it sticks to:
//
//  * Everything is built with textContent / createElement, never innerHTML. Group
//    names, member emails and machine hostnames are operator- and agent-supplied
//    strings that get re-rendered on every load; this page is where an XSS would be
//    worth the most, since its audience is by definition the people who can grant
//    capabilities.
//  * The capability vocabulary comes from the server (GET
//    /api/permissions/capabilities), not a copy here. A hardcoded list would silently
//    stop offering a new capability, which reads as "the feature doesn't work" rather
//    than "the UI is stale".

const groupsHost = document.getElementById('groups-host');
const modal = document.getElementById('group-modal');
const modalTitle = document.getElementById('group-modal-title');
const nameInput = document.getElementById('group-name');
const descriptionInput = document.getElementById('group-description');
const capabilityList = document.getElementById('capability-list');
const machinePicker = document.getElementById('machine-picker');
const machineChips = document.getElementById('machine-chips');
const machineInput = document.getElementById('machine-input');
const memberChips = document.getElementById('member-chips');
const memberInput = document.getElementById('member-input');
const errorEl = document.getElementById('group-error');
const groupStatusEl = document.getElementById('group-status');

let capabilities = [];        // [{name, label, description}]
let editingId = null;         // null while creating
let draftMachines = [];
let draftMembers = [];
let machineDirectory = [];    // full /api/machines rows, for the machine picker's search

async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
    if (!resp.ok) {
        throw new Error((body && body.error) || `HTTP ${resp.status}`);
    }
    return body;
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

// ---------------------------------------------------------------- the group list

function capabilityLabel(name) {
    const found = capabilities.find((c) => c.name === name);
    return found ? found.label : name;
}

function renderGroups(groups) {
    groupsHost.replaceChildren();
    if (!groups.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, 'No permission groups yet.'));
        empty.appendChild(el('p', 'stat-card__meta',
            'Until you create one, only the break-glass addresses above can sign in.'));
        groupsHost.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    ['Group', 'Capabilities', 'Machines', 'Members', ''].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    groups.forEach((group) => body.appendChild(renderGroupRow(group)));
    table.appendChild(body);
    card.appendChild(table);
    groupsHost.appendChild(card);
}

function renderGroupRow(group) {
    const tr = el('tr');

    const nameCell = el('td');
    nameCell.appendChild(el('div', null, group.name));
    if (group.description) {
        nameCell.appendChild(el('div', 'stat-card__meta', group.description));
    }
    tr.appendChild(nameCell);

    const capsCell = el('td');
    if (!group.capabilities.length) {
        capsCell.appendChild(el('span', 'stat-card__meta', 'None'));
    }
    group.capabilities.forEach((name) => {
        // The one capability worth calling out visually: it lets its holder grant
        // themselves everything else on this very page.
        const isAdmin = name === 'manage_permission_groups';
        capsCell.appendChild(
            el('span', isAdmin ? 'cap-badge cap-badge--admin' : 'cap-badge',
               capabilityLabel(name)));
    });
    tr.appendChild(capsCell);

    const machinesCell = el('td');
    if (group.scope_mode === 'all') {
        machinesCell.appendChild(el('span', 'cap-badge cap-badge--admin', 'Every machine'));
    } else if (!group.machines.length) {
        machinesCell.appendChild(el('span', 'stat-card__meta', 'None'));
    } else {
        machinesCell.appendChild(el('span', null, String(group.machines.length)));
        machinesCell.appendChild(el('div', 'stat-card__meta', group.machines.join(', ')));
    }
    tr.appendChild(machinesCell);

    const membersCell = el('td');
    if (!group.members.length) {
        membersCell.appendChild(el('span', 'stat-card__meta', 'None'));
    } else {
        membersCell.appendChild(el('div', 'stat-card__meta', group.members.join(', ')));
    }
    tr.appendChild(membersCell);

    const actions = el('td');
    const wrap = el('div', 'perm-row-actions');
    const edit = el('button', 'btn', 'Edit');
    edit.type = 'button';
    edit.addEventListener('click', () => openEditor(group));
    const remove = el('button', 'btn', 'Delete');
    remove.type = 'button';
    remove.addEventListener('click', () => deleteGroup(group, remove));
    wrap.append(edit, remove);
    actions.appendChild(wrap);
    tr.appendChild(actions);

    return tr;
}

async function deleteGroup(group, btn) {
    const warning = `Delete the permission group "${group.name}"?\n\n`
        + `${group.members.length} member(s) lose the access it granted. `
        + 'Anyone left with no groups at all can no longer sign in.';
    if (!window.confirm(warning)) return;
    btn.disabled = true;
    try {
        await api(`/api/permissions/groups/${encodeURIComponent(group.id)}`,
                  { method: 'DELETE' });
        await loadGroups();
    } catch (e) {
        btn.disabled = false;
        window.alert(`Could not delete "${group.name}": ${e.message}`);
    }
}

async function loadGroups() {
    try {
        renderGroups(await api('/api/permissions/groups'));
    } catch (e) {
        groupsHost.replaceChildren();
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, `Could not load permission groups: ${e.message}`));
        groupsHost.appendChild(empty);
    }
}

// ---------------------------------------------------------------- the editor

function renderChips(host, values, onRemove) {
    host.replaceChildren();
    values.forEach((value) => {
        const chip = el('span', 'chip');
        chip.appendChild(el('span', 'chip__name', value));
        const x = el('button', 'chip__remove', '×');
        x.type = 'button';
        x.setAttribute('aria-label', `Remove ${value}`);
        x.addEventListener('click', () => onRemove(value));
        chip.appendChild(x);
        host.appendChild(chip);
    });
}

function renderMachineChips() {
    renderChips(machineChips, draftMachines, (value) => {
        draftMachines = draftMachines.filter((m) => m !== value);
        renderMachineChips();
        autoSaveGroup();
    });
}

function renderMemberChips() {
    renderChips(memberChips, draftMembers, (value) => {
        draftMembers = draftMembers.filter((m) => m !== value);
        renderMemberChips();
        autoSaveGroup();
    });
}

function renderCapabilities(selected) {
    capabilityList.replaceChildren();
    capabilities.forEach((capability) => {
        const row = el('label', 'perm-capability');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.value = capability.name;
        box.checked = selected.includes(capability.name);
        const text = el('span');
        text.appendChild(el('span', 'perm-capability__label', capability.label));
        text.appendChild(el('span', 'perm-capability__help', capability.description));
        row.append(box, text);
        capabilityList.appendChild(row);
    });
}

function selectedCapabilities() {
    return Array.from(capabilityList.querySelectorAll('input:checked')).map((b) => b.value);
}

function scopeMode() {
    const checked = modal.querySelector('input[name="scope-mode"]:checked');
    return checked ? checked.value : 'list';
}

function syncScopeMode() {
    // "Every machine" makes the explicit list meaningless, so hide it rather than
    // leaving a list that silently has no effect.
    machinePicker.hidden = scopeMode() === 'all';
}

function openEditor(group) {
    editingId = group ? group.id : null;
    modalTitle.textContent = group ? `Edit ${group.name}` : 'New permission group';
    nameInput.value = group ? group.name : '';
    descriptionInput.value = (group && group.description) || '';
    draftMachines = group ? group.machines.slice() : [];
    draftMembers = group ? group.members.slice() : [];
    const mode = group ? group.scope_mode : 'list';
    modal.querySelectorAll('input[name="scope-mode"]').forEach((radio) => {
        radio.checked = radio.value === mode;
    });
    renderCapabilities(group ? group.capabilities : []);
    renderMachineChips();
    renderMemberChips();
    syncScopeMode();
    errorEl.textContent = '';
    // Reset the auto-save machinery for the new editing session.
    groupSaving = false;
    groupPending = false;
    if (groupDebounce) { clearTimeout(groupDebounce); groupDebounce = null; }
    setGroupStatus(group ? '' : 'Enter a name to create this group.', '');
    modal.showModal();
    nameInput.focus();
}

// ---------------------------------------------------------------- auto-save
//
// The editor saves as you go instead of on a Save button. Two guards make that safe on a
// page that grants sign-in access: (1) saves are serialised -- one request at a time, with
// a `pending` flag so an edit made mid-request is written straight after, never dropped;
// (2) every save sends the WHOLE group (name, capabilities, scope, machines, members), so
// there is no window where a half-applied group is live. A brand-new group cannot be
// created without a name, so nothing is written until one is typed; from the first
// successful create we hold its id and switch to updating it in place.

let groupSaving = false;
let groupPending = false;
let groupDebounce = null;

function setGroupStatus(text, cls) {
    if (!groupStatusEl) return;
    groupStatusEl.textContent = text;
    groupStatusEl.className = cls ? `autosave ${cls}` : 'autosave';
}

// Text fields debounce so a name is not POSTed letter by letter; discrete changes
// (a capability ticked, a chip added, the scope mode switched) save at once.
function autoSaveGroupDebounced() {
    if (groupDebounce) clearTimeout(groupDebounce);
    groupDebounce = setTimeout(autoSaveGroup, 500);
}

function autoSaveGroup() {
    if (groupDebounce) { clearTimeout(groupDebounce); groupDebounce = null; }
    if (groupSaving) { groupPending = true; return; }
    flushGroup();
}

async function flushGroup() {
    const name = nameInput.value.trim();
    // A group cannot exist without a name; don't create one until there is something to
    // call it. Editing an existing group with the name cleared is a real (rejected) edit,
    // so only short-circuit while still creating.
    if (!editingId && !name) {
        setGroupStatus('Enter a name to create this group.', '');
        return;
    }
    const payload = {
        name,
        description: descriptionInput.value.trim(),
        capabilities: selectedCapabilities(),
        scope_mode: scopeMode(),
        // Always send machines, even in "all" mode: the server ignores the list for an
        // "all" group, and keeping it means switching back to "list" doesn't silently
        // discard what was there.
        machines: draftMachines,
        members: draftMembers,
    };
    groupSaving = true;
    groupPending = false;
    errorEl.textContent = '';
    setGroupStatus('Saving…', '');
    try {
        let group;
        if (editingId) {
            group = await api(`/api/permissions/groups/${encodeURIComponent(editingId)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
        } else {
            group = await api('/api/permissions/groups', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            // From here on this is an existing group: hold its id and update in place, so a
            // second change doesn't create a duplicate.
            editingId = group.id;
            modalTitle.textContent = `Edit ${group.name}`;
        }
        setGroupStatus('Saved', 'autosave--saved');
        await loadGroups();      // refresh the list behind the modal
    } catch (e) {
        // Validation failure (e.g. a duplicate name) writes nothing; show it and leave the
        // editor open. We do NOT auto-retry -- only a genuine new edit (groupPending) does.
        errorEl.textContent = e.message;
        setGroupStatus('', '');
    } finally {
        groupSaving = false;
        if (groupPending) flushGroup();
    }
}

function addFrom(input, list, render, normalize) {
    addValue(list, normalize(input.value), render);
    input.value = '';
    input.focus();
}

// Add one already-normalized value to a draft list (machines or members), re-render its
// chips and save. Shared by the Add button, the Enter key, and an autocomplete pick.
function addValue(list, value, render) {
    if (!value) return;
    if (!list.includes(value)) list.push(value);
    render();
    autoSaveGroup();
}

// ---------------------------------------------------------------- pickers
//
// Both fields are search-as-you-type comboboxes (autocomplete.js). The machine picker
// filters the already-loaded fleet client-side across name + the three identifiers; the
// member picker queries the Registered Users directory server-side. Both still accept
// free text on Enter/Add -- a machine can be scoped before it enrolls, and a member added
// by an email that has never signed in -- so the dropdown assists without gating.

function machineSublabel(row) {
    // Show whichever identifiers the row has, so a search hit on serial/asset/service is
    // visible in the suggestion rather than looking like a bare name match.
    return [
        row.asset_tag && `Asset ${row.asset_tag}`,
        row.serial_number && `Serial ${row.serial_number}`,
        row.service_tag && `Service ${row.service_tag}`,
    ].filter(Boolean).join('  ·  ');
}

function machineMatches(query) {
    const q = query.toLowerCase();
    const fields = ['machine', 'asset_tag', 'serial_number', 'service_tag'];
    return machineDirectory
        .filter((row) => !draftMachines.includes(row.machine))
        .filter((row) => fields.some((f) => row[f] && String(row[f]).toLowerCase().includes(q)))
        .slice(0, 20)
        .map((row) => ({ value: row.machine, label: row.machine, sublabel: machineSublabel(row) }));
}

function attachPickers() {
    attachAutocomplete(machineInput, {
        minChars: 0,
        emptyText: 'No matching machines — press Add to scope one that has not enrolled yet.',
        source: (query) => machineMatches(query),
        onSelect: (item) => {
            addValue(draftMachines, item.value, renderMachineChips);
            machineInput.value = '';
            machineInput.focus();
        },
    });
    attachAutocomplete(memberInput, {
        minChars: 1,
        emptyText: 'No matching users — press Add to invite this email.',
        source: async (query) => {
            const body = await api(`/api/permissions/directory?q=${encodeURIComponent(query)}`);
            return (body.users || []).map((u) => ({
                value: u.email,
                label: u.full_name || u.email,
                sublabel: u.username ? `${u.username}  ·  ${u.email}` : u.email,
            }));
        },
        onSelect: (item) => {
            addValue(draftMembers, String(item.value).toLowerCase(), renderMemberChips);
            memberInput.value = '';
            memberInput.focus();
        },
    });
}

// ---------------------------------------------------------------- wiring

document.getElementById('new-group').addEventListener('click', () => openEditor(null));
document.getElementById('group-cancel').addEventListener('click', () => modal.close());

// Auto-save wiring. Text fields debounce; everything else saves on change. Capabilities
// are caught by delegation on their container, so a checkbox added by a future capability
// is covered without extra wiring.
nameInput.addEventListener('input', autoSaveGroupDebounced);
descriptionInput.addEventListener('input', autoSaveGroupDebounced);
capabilityList.addEventListener('change', autoSaveGroup);
modal.querySelectorAll('input[name="scope-mode"]').forEach((radio) => {
    radio.addEventListener('change', () => { syncScopeMode(); autoSaveGroup(); });
});

document.getElementById('machine-add').addEventListener('click',
    () => addFrom(machineInput, draftMachines, renderMachineChips, (v) => v.trim()));
document.getElementById('member-add').addEventListener('click',
    () => addFrom(memberInput, draftMembers, renderMemberChips, (v) => v.trim().toLowerCase()));

// Enter inside the chip inputs adds the entry rather than submitting the dialog --
// which would otherwise close the editor and discard everything typed so far.
[[machineInput, () => document.getElementById('machine-add').click()],
 [memberInput, () => document.getElementById('member-add').click()]].forEach(([input, add]) => {
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            add();
        }
    });
});

// Clicking the backdrop (the dialog element itself, which covers the viewport) closes
// the editor -- same convention as the favorites dialog.
modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.close();
});

async function init() {
    try {
        const doc = await api('/api/permissions/capabilities');
        capabilities = doc.capabilities || [];
    } catch (e) {
        errorEl.textContent = `Could not load capabilities: ${e.message}`;
    }
    // Suggestions only -- a hostname that hasn't reported yet can still be typed in,
    // so a machine can be scoped before it enrolls. /api/machines is itself scope
    // filtered, so a non-superuser admin is only ever offered machines they can see.
    try {
        machineDirectory = await api('/api/machines');
    } catch (e) { /* suggestions are optional; typing still works */ }
    attachPickers();
    await loadGroups();
}

init();
