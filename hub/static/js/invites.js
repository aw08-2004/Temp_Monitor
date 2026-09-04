// Invite links admin page (roadmap #22).
//
// One list + one <dialog> creator + one dialog that shows the finished link. The rules it
// sticks to are permissions.js's, for the same reasons:
//
//  * Everything is built with textContent / createElement, never innerHTML. Labels, group
//    names and redeemer emails are operator-supplied strings re-rendered on every load,
//    and this page's audience is by definition the people who can hand out capabilities.
//  * The capability vocabulary for a custom group comes from the server (GET
//    /api/permissions/capabilities), not a copy here.
//  * **Status labels are looked up through a literal branch, never by concatenating the
//    status onto a key prefix.** tests/test_i18n.py only scans literal t() calls, so a
//    computed key is invisible to the test that would have caught the missing translation
//    -- the status would render its own key on the page with nothing failing. (The scan is
//    textual, so even naming the computed form in a comment trips it. That is the check
//    working, not a false alarm: a comment is where such a key gets copied from.)
//
// Unlike permissions.js this has an explicit Create button and no auto-save: an invite is
// created once and is then immutable (revoke or delete, nothing in between), so there is
// no partial state worth streaming -- and half a link is not a thing that should exist.
(function () {
'use strict';

const invitesHost = document.getElementById('invites-host');
const modal = document.getElementById('invite-modal');
const labelInput = document.getElementById('invite-label');
const groupChips = document.getElementById('group-chips');
const groupInput = document.getElementById('group-input');
const existingPicker = document.getElementById('existing-picker');
const newGroupEditor = document.getElementById('new-group-editor');
const newGroupName = document.getElementById('new-group-name');
const capabilityList = document.getElementById('capability-list');
const machinePicker = document.getElementById('machine-picker');
const machineChips = document.getElementById('machine-chips');
const machineInput = document.getElementById('machine-input');
const usesInput = document.getElementById('invite-uses');
const expirySelect = document.getElementById('invite-expiry');
const pinnedChips = document.getElementById('pinned-chips');
const pinnedInput = document.getElementById('pinned-input');
const errorEl = document.getElementById('invite-error');
const statusEl = document.getElementById('invite-status');
const saveBtn = document.getElementById('invite-save');

const linkModal = document.getElementById('link-modal');
const linkValue = document.getElementById('link-value');
const linkStatus = document.getElementById('link-status');

let capabilities = [];     // [{name, label, description}]
let allGroups = [];        // [{id, name}] -- for the picker and for name lookup in the list
let draftGroups = [];      // group ids attached to the invite being created
let draftMachines = [];
let draftPinned = [];

async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
    if (!resp.ok) {
        throw new Error((body && body.error) || `HTTP ${resp.status}`);
    }
    return body;
}

function groupName(id) {
    const found = allGroups.find((g) => g.id === id);
    // A group deleted after the invite was made. Named as such rather than shown as a raw
    // uuid: the invite still exists and an admin looking at it needs to know that what it
    // used to grant is gone.
    return found ? found.name : t('invites.deleted_group');
}

// Literal keys, one per status -- see the note at the top of this file.
function statusLabel(status) {
    if (status === 'active') return t('invites.status.active');
    if (status === 'used_up') return t('invites.status.used_up');
    if (status === 'expired') return t('invites.status.expired');
    if (status === 'revoked') return t('invites.status.revoked');
    return status;
}


// ---------------------------------------------------------------- the list

function renderInvites(invites) {
    invitesHost.replaceChildren();
    if (!invites.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('invites.empty')));
        empty.appendChild(el('p', 'stat-card__meta', t('invites.empty_hint')));
        invitesHost.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    [t('invites.col.label'), t('invites.col.grants'), t('invites.col.uses'),
     t('invites.col.expires'), t('invites.col.status'), ''].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    invites.forEach((invite) => body.appendChild(renderInviteRow(invite)));
    table.appendChild(body);
    card.appendChild(table);
    invitesHost.appendChild(card);
}

function renderInviteRow(invite) {
    const tr = el('tr');

    const labelCell = el('td');
    labelCell.appendChild(el('div', null, invite.label));
    labelCell.appendChild(el('div', 'stat-card__meta',
        t('invites.created_by', { email: invite.created_by })));
    if (invite.pinned_emails.length) {
        labelCell.appendChild(el('div', 'stat-card__meta',
            t('invites.pinned_to', { emails: invite.pinned_emails.join(', ') })));
    }
    tr.appendChild(labelCell);

    const grantsCell = el('td');
    invite.group_ids.forEach((id) => {
        grantsCell.appendChild(el('span', 'cap-badge', groupName(id)));
    });
    tr.appendChild(grantsCell);

    const usesCell = el('td');
    usesCell.appendChild(el('span', null,
        `${invite.used_count} / ${invite.max_uses}`));
    // Who actually came in through this link. The whole point of keeping redemption rows
    // after the seats are gone -- "how did this person get access" is the question an
    // audit asks first, and it should be answerable from the row that admitted them.
    if (invite.redeemed_by.length) {
        usesCell.appendChild(el('div', 'stat-card__meta', invite.redeemed_by.join(', ')));
    }
    tr.appendChild(usesCell);

    tr.appendChild(el('td', 'stat-card__meta',
        invite.expires_at ? fmtTime(invite.expires_at) : t('invites.never_expires')));

    const statusCell = el('td');
    statusCell.appendChild(el('span',
        invite.status === 'active' ? 'cap-badge' : 'stat-card__meta',
        statusLabel(invite.status)));
    tr.appendChild(statusCell);

    const actions = el('td');
    const wrap = el('div', 'perm-row-actions');
    if (invite.status === 'active') {
        const revoke = el('button', 'btn', t('invites.revoke'));
        revoke.type = 'button';
        revoke.addEventListener('click', () => revokeInvite(invite, revoke));
        wrap.appendChild(revoke);
    }
    const remove = el('button', 'btn', t('common.delete'));
    remove.type = 'button';
    remove.addEventListener('click', () => deleteInvite(invite, remove));
    wrap.appendChild(remove);
    actions.appendChild(wrap);
    tr.appendChild(actions);

    return tr;
}

async function revokeInvite(invite, btn) {
    btn.disabled = true;
    try {
        await api(`/api/invites/${encodeURIComponent(invite.invite_id)}/revoke`,
                  { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: '{}' });
        await load();
    } catch (e) {
        btn.disabled = false;
        window.alert(e.message);
    }
}

async function deleteInvite(invite, btn) {
    // Says out loud that deleting the invite does not take anyone's access away -- the
    // redeemers are permission group members now, exactly as if they had been added by
    // hand, and that is where access is removed.
    if (!window.confirm(t('invites.confirm_delete', { label: invite.label }))) return;
    btn.disabled = true;
    try {
        await api(`/api/invites/${encodeURIComponent(invite.invite_id)}`,
                  { method: 'DELETE' });
        await load();
    } catch (e) {
        btn.disabled = false;
        window.alert(e.message);
    }
}

async function load() {
    try {
        const body = await api('/api/invites');
        allGroups = body.groups || [];
        renderInvites(body.invites || []);
    } catch (e) {
        invitesHost.replaceChildren();
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('invites.load_failed', { error: e.message })));
        invitesHost.appendChild(empty);
    }
}


// ---------------------------------------------------------------- the creator

function renderChips(host, values, render, onRemove) {
    host.replaceChildren();
    values.forEach((value) => {
        const chip = el('span', 'chip');
        chip.appendChild(el('span', 'chip__name', render(value)));
        const x = el('button', 'chip__remove', '×');
        x.type = 'button';
        x.setAttribute('aria-label', t('permissions.editor.remove', { value: render(value) }));
        x.addEventListener('click', () => onRemove(value));
        chip.appendChild(x);
        host.appendChild(chip);
    });
}

function renderGroupChips() {
    renderChips(groupChips, draftGroups, groupName, (value) => {
        draftGroups = draftGroups.filter((g) => g !== value);
        renderGroupChips();
    });
}

function renderMachineChips() {
    renderChips(machineChips, draftMachines, (v) => v, (value) => {
        draftMachines = draftMachines.filter((m) => m !== value);
        renderMachineChips();
    });
}

function renderPinnedChips() {
    renderChips(pinnedChips, draftPinned, (v) => v, (value) => {
        draftPinned = draftPinned.filter((p) => p !== value);
        renderPinnedChips();
    });
}

function renderCapabilities() {
    capabilityList.replaceChildren();
    capabilities.forEach((capability) => {
        const row = el('label', 'perm-capability');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.value = capability.name;
        const text = el('span');
        text.appendChild(el('span', 'perm-capability__label', capability.label));
        text.appendChild(el('span', 'perm-capability__help', capability.description));
        row.append(box, text);
        capabilityList.appendChild(row);
    });
}

function grantMode() {
    const checked = modal.querySelector('input[name="grant-mode"]:checked');
    return checked ? checked.value : 'existing';
}

function scopeMode() {
    const checked = modal.querySelector('input[name="scope-mode"]:checked');
    return checked ? checked.value : 'list';
}

function syncModes() {
    const custom = grantMode() === 'new';
    existingPicker.hidden = custom;
    newGroupEditor.hidden = !custom;
    machinePicker.hidden = scopeMode() === 'all';
}

function openEditor() {
    draftGroups = [];
    draftMachines = [];
    draftPinned = [];
    labelInput.value = '';
    newGroupName.value = '';
    usesInput.value = '1';
    expirySelect.value = '7';
    modal.querySelector('input[name="grant-mode"][value="existing"]').checked = true;
    modal.querySelector('input[name="scope-mode"][value="list"]').checked = true;
    renderGroupChips();
    renderMachineChips();
    renderPinnedChips();
    renderCapabilities();
    syncModes();
    errorEl.textContent = '';
    statusEl.textContent = '';
    modal.showModal();
    labelInput.focus();
}

function payload() {
    const body = {
        label: labelInput.value.trim(),
        max_uses: Number(usesInput.value) || 1,
        pinned_emails: draftPinned,
        // "never" has to cross the wire as an explicit null: the server reads the KEY, so
        // that a missing key can still mean "the default lifetime" rather than "forever".
        ttl_days: expirySelect.value === 'never' ? null : Number(expirySelect.value),
    };
    if (grantMode() === 'new') {
        body.new_group = {
            name: newGroupName.value.trim(),
            capabilities: Array.from(capabilityList.querySelectorAll('input:checked'))
                .map((b) => b.value),
            scope_mode: scopeMode(),
            machines: scopeMode() === 'all' ? [] : draftMachines,
        };
    } else {
        body.group_ids = draftGroups;
    }
    return body;
}

async function createInvite() {
    saveBtn.disabled = true;
    errorEl.textContent = '';
    statusEl.textContent = t('common.saving');
    try {
        const created = await api('/api/invites', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload()),
        });
        modal.close();
        showLink(created.link);
        await load();
    } catch (e) {
        errorEl.textContent = e.message;
        statusEl.textContent = '';
    } finally {
        saveBtn.disabled = false;
    }
}

function showLink(link) {
    linkValue.value = link || '';
    linkStatus.textContent = '';
    linkModal.showModal();
    linkValue.select();
}

async function copyLink() {
    try {
        await navigator.clipboard.writeText(linkValue.value);
        linkStatus.textContent = t('invites.link.copied');
    } catch (e) {
        // Clipboard access is refused outside a secure context, and a hub reached over
        // plain http on a LAN is exactly that. The text is already selected, so say to use
        // the keyboard rather than failing silently.
        linkStatus.textContent = t('invites.link.copy_failed');
        linkValue.select();
    }
}


// ---------------------------------------------------------------- pickers

function addValue(list, value, render) {
    const text = String(value || '').trim();
    if (!text || list.includes(text)) return;
    list.push(text);
    render();
}

function attachPickers() {
    attachAutocomplete(groupInput, {
        minChars: 0,
        emptyText: t('invites.editor.group_empty'),
        source: (query) => allGroups
            .filter((g) => !draftGroups.includes(g.id))
            .filter((g) => g.name.toLowerCase().includes(query.toLowerCase()))
            .slice(0, 20)
            .map((g) => ({ value: g.id, label: g.name })),
        onSelect: (item) => {
            addValue(draftGroups, item.value, renderGroupChips);
            groupInput.value = '';
            groupInput.focus();
        },
    });
    // No free-text fallback on the group field, unlike permissions.js's machine and member
    // pickers: those accept a value that does not exist yet on purpose, and a permission
    // group id typed by hand is a uuid nobody knows. The Add button resolves by NAME
    // instead, so the only ids that reach the server came from this list.
    document.getElementById('group-add').addEventListener('click', () => {
        const typed = groupInput.value.trim().toLowerCase();
        const match = allGroups.find((g) => g.name.toLowerCase() === typed);
        if (match) {
            addValue(draftGroups, match.id, renderGroupChips);
            groupInput.value = '';
        }
    });

    document.getElementById('machine-add').addEventListener('click', () => {
        addValue(draftMachines, machineInput.value, renderMachineChips);
        machineInput.value = '';
    });
    document.getElementById('pinned-add').addEventListener('click', () => {
        addValue(draftPinned, pinnedInput.value.toLowerCase(), renderPinnedChips);
        pinnedInput.value = '';
    });
}


// ---------------------------------------------------------------- wiring

document.getElementById('new-invite').addEventListener('click', openEditor);
document.getElementById('invite-cancel').addEventListener('click', () => modal.close());
saveBtn.addEventListener('click', createInvite);
document.getElementById('link-copy').addEventListener('click', copyLink);
document.getElementById('link-close').addEventListener('click', () => linkModal.close());

modal.querySelectorAll('input[name="grant-mode"], input[name="scope-mode"]')
    .forEach((radio) => radio.addEventListener('change', syncModes));

// Enter in a chip field adds rather than submitting the dialog (method="dialog" would
// close it). The same guard permissions.js applies, for the same reason.
[machineInput, pinnedInput, groupInput].forEach((input) => {
    input.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        if (input === machineInput) {
            addValue(draftMachines, machineInput.value, renderMachineChips);
            machineInput.value = '';
        } else if (input === pinnedInput) {
            addValue(draftPinned, pinnedInput.value.toLowerCase(), renderPinnedChips);
            pinnedInput.value = '';
        } else {
            document.getElementById('group-add').click();
        }
    });
});

modal.addEventListener('click', (e) => { if (e.target === modal) modal.close(); });

(async function init() {
    try {
        const body = await api('/api/permissions/capabilities');
        capabilities = body.capabilities || [];
    } catch (e) {
        capabilities = [];
    }
    await load();
    attachPickers();
})();

})();
