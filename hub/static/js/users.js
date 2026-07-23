// Registered Users directory admin page.
//
// One searchable list + one <dialog> editor. Two rules, same as permissions.js:
//
//  * Everything is built with textContent / createElement, never innerHTML. Names,
//    usernames and emails are operator-supplied strings re-rendered on every load.
//  * This page edits PROFILES, not access. It never touches permission groups; the
//    copy on the page says so, and so does the server (users.py is not an auth table).
//
// Unlike permissions.js this uses an explicit Save button rather than auto-save: the
// email is the primary key and cannot change after create, so there is no half-formed
// "create as you type" state worth streaming -- one atomic Save is simpler and avoids
// creating a row from a half-typed address.

const usersHost = document.getElementById('users-host');
const searchInput = document.getElementById('user-search');
const modal = document.getElementById('user-modal');
const modalTitle = document.getElementById('user-modal-title');
const emailInput = document.getElementById('user-email');
const fullNameInput = document.getElementById('user-full-name');
const usernameInput = document.getElementById('user-username');
const titleInput = document.getElementById('user-title');
const departmentInput = document.getElementById('user-department');
const phoneInput = document.getElementById('user-phone');
const notesInput = document.getElementById('user-notes');
const errorEl = document.getElementById('user-error');
const statusEl = document.getElementById('user-status');
const saveBtn = document.getElementById('user-save');

let editingEmail = null;   // null while creating
let searchDebounce = null;

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

// ---------------------------------------------------------------- the list

function renderUsers(users) {
    usersHost.replaceChildren();
    if (!users.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, searchInput.value.trim()
            ? 'No users match your search.'
            : 'No registered users yet.'));
        if (!searchInput.value.trim()) {
            empty.appendChild(el('p', 'stat-card__meta',
                'Anyone who signs in appears here automatically.'));
        }
        usersHost.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    ['Name', 'Username', 'Email', 'Department', 'Last login', ''].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    users.forEach((user) => body.appendChild(renderUserRow(user)));
    table.appendChild(body);
    card.appendChild(table);
    usersHost.appendChild(card);
}

function formatLastLogin(ts) {
    if (!ts) return 'Never';
    return new Date(ts * 1000).toLocaleString();
}

function renderUserRow(user) {
    const tr = el('tr');

    const nameCell = el('td');
    nameCell.appendChild(el('div', null, user.full_name || '—'));
    if (user.title) nameCell.appendChild(el('div', 'stat-card__meta', user.title));
    tr.appendChild(nameCell);

    tr.appendChild(el('td', null, user.username || '—'));
    tr.appendChild(el('td', null, user.email));
    tr.appendChild(el('td', null, user.department || '—'));

    const loginCell = el('td', 'stat-card__meta', formatLastLogin(user.last_login_at));
    // A manually-added user who has never signed in is worth calling out: it means the
    // email may not match a real account yet.
    if (!user.last_login_at) loginCell.classList.add('stat-card__meta');
    tr.appendChild(loginCell);

    const actions = el('td');
    const wrap = el('div', 'perm-row-actions');
    const edit = el('button', 'btn', 'Edit');
    edit.type = 'button';
    edit.addEventListener('click', () => openEditor(user));
    const remove = el('button', 'btn', 'Delete');
    remove.type = 'button';
    remove.addEventListener('click', () => deleteUser(user, remove));
    wrap.append(edit, remove);
    actions.appendChild(wrap);
    tr.appendChild(actions);

    return tr;
}

async function deleteUser(user, btn) {
    const warning = `Remove "${user.full_name || user.email}" from the directory?\n\n`
        + 'This only removes their profile. It does NOT change what they can do or '
        + 'whether they can sign in -- that is set by their Permission Groups.';
    if (!window.confirm(warning)) return;
    btn.disabled = true;
    try {
        await api(`/api/users/${encodeURIComponent(user.email)}`, { method: 'DELETE' });
        await loadUsers();
    } catch (e) {
        btn.disabled = false;
        window.alert(`Could not delete "${user.email}": ${e.message}`);
    }
}

async function loadUsers() {
    const q = searchInput.value.trim();
    try {
        const path = q ? `/api/users?q=${encodeURIComponent(q)}` : '/api/users';
        renderUsers(await api(path));
    } catch (e) {
        usersHost.replaceChildren();
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, `Could not load users: ${e.message}`));
        usersHost.appendChild(empty);
    }
}

// ---------------------------------------------------------------- the editor

function openEditor(user) {
    editingEmail = user ? user.email : null;
    modalTitle.textContent = user ? `Edit ${user.full_name || user.email}` : 'Add user';
    emailInput.value = user ? user.email : '';
    // Email is the primary key -- immutable once the row exists.
    emailInput.disabled = !!user;
    fullNameInput.value = (user && user.full_name) || '';
    usernameInput.value = (user && user.username) || '';
    titleInput.value = (user && user.title) || '';
    departmentInput.value = (user && user.department) || '';
    phoneInput.value = (user && user.phone) || '';
    notesInput.value = (user && user.notes) || '';
    errorEl.textContent = '';
    statusEl.textContent = '';
    modal.showModal();
    (user ? fullNameInput : emailInput).focus();
}

function payload() {
    return {
        email: emailInput.value.trim(),
        full_name: fullNameInput.value.trim(),
        username: usernameInput.value.trim(),
        title: titleInput.value.trim(),
        department: departmentInput.value.trim(),
        phone: phoneInput.value.trim(),
        notes: notesInput.value.trim(),
    };
}

async function saveUser() {
    const data = payload();
    if (!editingEmail && !data.email) {
        errorEl.textContent = 'An email address is required.';
        return;
    }
    saveBtn.disabled = true;
    errorEl.textContent = '';
    statusEl.textContent = 'Saving…';
    try {
        if (editingEmail) {
            await api(`/api/users/${encodeURIComponent(editingEmail)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            });
        } else {
            await api('/api/users', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            });
        }
        modal.close();
        await loadUsers();
    } catch (e) {
        errorEl.textContent = e.message;
        statusEl.textContent = '';
    } finally {
        saveBtn.disabled = false;
    }
}

// ---------------------------------------------------------------- wiring

document.getElementById('new-user').addEventListener('click', () => openEditor(null));
document.getElementById('user-cancel').addEventListener('click', () => modal.close());
saveBtn.addEventListener('click', saveUser);

searchInput.addEventListener('input', () => {
    if (searchDebounce) clearTimeout(searchDebounce);
    searchDebounce = setTimeout(loadUsers, 250);
});

// Enter in a single-line field saves rather than submitting the dialog (which would
// close it via method="dialog"). The notes textarea keeps Enter for newlines.
modal.querySelectorAll('input').forEach((input) => {
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            saveUser();
        }
    });
});

modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.close();
});

loadUsers();
