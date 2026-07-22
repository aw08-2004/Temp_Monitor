// Backups page: configure where backups go, prove the key is safe, watch runs land.
//
// Same two rules as packages.js and permissions.js, for the same reasons:
//
//  * Everything is built with textContent / createElement, never innerHTML. Destination
//    names, object keys and — most of all — provider error strings echoed back into the
//    run list are arbitrary text from operators and remote servers.
//  * The destination-kind vocabulary comes from GET /api/backups, not a copy here.
//
// One rule of its own: the master key is never held in a variable longer than the modal
// that shows it, never written to localStorage, and never put in a URL. It is displayed,
// copied, and dropped.
//
// The run list polls while a backup is in flight. The hub's scheduler ticks on its own,
// so this page is a viewer of that state and never a driver of it — closing the tab does
// not stop or start anything.

const hubPane = document.getElementById('hub-pane');
const settingsPane = document.getElementById('settings-pane');
const destinationsPane = document.getElementById('destinations-pane');
const keyBanner = document.getElementById('key-banner');

const destinationModal = document.getElementById('destination-modal');
const destinationError = document.getElementById('destination-error');
const destinationStatus = document.getElementById('destination-status');
const keyModal = document.getElementById('key-modal');
const keyError = document.getElementById('key-error');
const keyValue = document.getElementById('key-value');

let state = { destinations: [], runs: [], schedule: {}, key: {}, destination_kinds: [],
              files: {}, path_tokens: [] };
let editingDestinationId = null;
let draftKind = 's3';
let pollTimer = null;

// Working copies of the two path lists, edited as chips before being saved. Kept out of
// `state` because they are the operator's unsaved intent — a background poll refreshing
// `state` must not silently discard paths someone is halfway through typing.
let draftInclude = [];
let draftExclude = [];
let previewMachine = '';
let previewTimer = null;

async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
    if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);
    return body;
}

function json(method, payload) {
    // Content-Type: application/json is load-bearing, not cosmetic — it is what makes a
    // cross-origin POST preflight and fail. See fleet_web.py's module docstring.
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) };
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function fmtBytes(n) {
    if (!n && n !== 0) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function fmtTime(epoch) {
    if (!epoch) return '—';
    return new Date(epoch * 1000).toLocaleString();
}

function fmtDuration(from, to) {
    if (!from || !to) return '—';
    const secs = Math.max(0, to - from);
    if (secs < 60) return `${secs}s`;
    return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

// ---------------------------------------------------------------- loading

async function load() {
    state = await api('/api/backups');
    // Seed the editors from what was saved, but only when they are untouched — see the
    // comment on draftInclude.
    if (!draftDirty) {
        draftInclude = (state.files.include || []).slice();
        draftExclude = (state.files.exclude || []).slice();
    }
    render();
    schedulePoll();
}

// Set the moment a chip is added or removed, cleared on a successful save. Guards the
// reseed above.
let draftDirty = false;

// Only poll while something is actually moving. A backup takes minutes and the page is
// otherwise static, so a fixed interval would be almost entirely wasted requests.
function schedulePoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    const busy = state.schedule.running || (state.runs || []).some((r) => r.status === 'running');
    if (!busy) return;
    pollTimer = setTimeout(async () => {
        try {
            const fresh = await api('/api/backups/runs');
            state.runs = fresh.runs;
            state.schedule = fresh.schedule;
            render();
        } catch (e) { /* transient — the next user action will resync */ }
        schedulePoll();
    }, 4000);
}

function render() {
    renderKeyBanner();
    renderHubPane();
    renderSettingsPane();
    renderDestinations();
}

// ---------------------------------------------------------------- key banner

function renderKeyBanner() {
    keyBanner.replaceChildren();
    const key = state.key || {};

    // Three states, three different messages. The distinction that matters is between
    // "no key" (nothing can be backed up yet) and "key never written down" (backups are
    // running and are one disk failure away from being worthless) — the second is the
    // quieter and more dangerous of the two, so it is not softened.
    let modifier = 'bk-banner--warn';
    let title;
    let text;
    const actions = [];

    if (key.crypto_available === false) {
        modifier = 'bk-banner--danger';
        title = 'Encryption library missing';
        text = 'The cryptography package is not installed, so backups cannot be encrypted '
             + 'or restored. Run: pip install -r requirements.txt, then restart the hub.';
    } else if (!key.configured) {
        title = 'No encryption key yet';
        text = 'Backups are encrypted on this server before upload. Create the key to get '
             + 'started — you will be shown it once, and you must store it somewhere other '
             + 'than this machine.';
        const create = el('button', 'btn btn--primary', 'Create encryption key');
        create.addEventListener('click', createKey);
        actions.push(create);
    } else if (!key.escrowed_at) {
        modifier = 'bk-banner--danger';
        title = 'The encryption key has never been stored anywhere else';
        text = 'If this server is lost, every backup it has taken becomes permanently '
             + 'unreadable. Reveal the key, store it in a password manager or a sealed '
             + 'envelope, and confirm.';
        const reveal = el('button', 'btn btn--primary', 'Reveal key');
        reveal.addEventListener('click', revealKey);
        actions.push(reveal);
    } else {
        modifier = '';
        title = 'Encryption key configured';
        text = `Key ${key.key_id} — confirmed stored offline on ${fmtTime(key.escrowed_at)}. `
             + 'Restore with restore_backup.py, which needs only this key and the backup file.';
        const reveal = el('button', 'btn', 'Reveal key');
        reveal.addEventListener('click', revealKey);
        actions.push(reveal);
    }

    const banner = el('div', `bk-banner ${modifier}`.trim());
    const body = el('div', 'bk-banner__body');
    body.appendChild(el('div', 'bk-banner__title', title));
    body.appendChild(el('div', 'bk-banner__text', text));
    banner.appendChild(body);
    if (actions.length) {
        const wrap = el('div', 'bk-banner__actions');
        actions.forEach((a) => wrap.appendChild(a));
        banner.appendChild(wrap);
    }
    keyBanner.appendChild(banner);
}

async function createKey() {
    try {
        const result = await api('/api/backups/key', json('POST'));
        state.key = result.state;
        showKey(result.key);
        renderKeyBanner();
    } catch (e) {
        alert(e.message);
    }
}

async function revealKey() {
    try {
        const result = await api('/api/backups/key/reveal', json('POST'));
        showKey(result.key);
    } catch (e) {
        alert(e.message);
    }
}

function showKey(key) {
    keyError.textContent = '';
    keyValue.textContent = key;
    keyModal.showModal();
}

document.getElementById('key-copy').addEventListener('click', async () => {
    try {
        await navigator.clipboard.writeText(keyValue.textContent);
        keyError.textContent = 'Copied. Paste it somewhere durable before closing this.';
    } catch (e) {
        // Clipboard access is refused outside a secure context, and a hub reached over
        // plain http on a lab network is exactly that. Selecting the text is the fallback,
        // and .bk-key is user-select: all so one click takes the whole key.
        keyError.textContent = 'Could not copy automatically — click the key to select it.';
    }
});

document.getElementById('key-ack').addEventListener('click', async () => {
    try {
        const result = await api('/api/backups/key/escrowed', json('POST'));
        state.key = result.key;
        // Cleared before the dialog closes, so the key does not sit in the DOM behind it.
        keyValue.textContent = '';
        keyModal.close();
        renderKeyBanner();
    } catch (e) {
        keyError.textContent = e.message;
    }
});

keyModal.addEventListener('close', () => { keyValue.textContent = ''; });

// ---------------------------------------------------------------- hub database pane

function renderHubPane() {
    hubPane.replaceChildren();
    hubPane.appendChild(renderScheduleCard());
    hubPane.appendChild(renderRunsCard());
}

function renderScheduleCard() {
    const schedule = state.schedule || {};
    const card = el('div', 'card');
    card.appendChild(el('h2', 'section-title', 'Schedule'));

    if (!state.destinations.length) {
        card.appendChild(el('p', 'stat-card__meta',
            'Add a destination first — there is nowhere to put a backup yet.'));
        return card;
    }

    const grid = el('div', 'bk-schedule-grid');

    const enabledWrap = el('div');
    const enabledLabel = el('label', 'checkbox');
    const enabled = el('input');
    enabled.type = 'checkbox';
    enabled.id = 'schedule-enabled';
    enabled.checked = !!schedule.enabled;
    enabledLabel.appendChild(enabled);
    enabledLabel.appendChild(document.createTextNode(' Back up automatically'));
    enabledWrap.appendChild(enabledLabel);
    // next_due_at is 0 for "never run, so due immediately" — a falsy number that would
    // otherwise render as the "it's off" message on a schedule that is very much on.
    let dueText;
    if (!schedule.enabled) {
        dueText = 'Nothing is uploaded while this is off.';
    } else if (!schedule.next_due_at || schedule.next_due_at * 1000 <= Date.now()) {
        dueText = 'Due now — the next scheduler pass will take it.';
    } else {
        dueText = `Next due ${fmtTime(schedule.next_due_at)}.`;
    }
    enabledWrap.appendChild(el('p', 'setting__default', dueText));
    grid.appendChild(enabledWrap);

    const destWrap = el('div');
    destWrap.appendChild(el('label', 'setting__label', 'Destination'));
    const select = el('select', 'input');
    select.id = 'schedule-destination';
    select.style.width = '100%';
    const blank = el('option', null, 'Choose a destination…');
    blank.value = '';
    select.appendChild(blank);
    state.destinations.forEach((d) => {
        const option = el('option', null, d.name);
        option.value = d.id;
        if (d.id === schedule.destination_id) option.selected = true;
        select.appendChild(option);
    });
    destWrap.appendChild(select);
    grid.appendChild(destWrap);

    grid.appendChild(numberField('schedule-interval', 'Back up every (hours)',
                                 schedule.interval_hours, 1, 720));
    grid.appendChild(numberField('schedule-keep', 'Keep this many backups',
                                 schedule.keep_generations, 1, 365));
    card.appendChild(grid);

    card.appendChild(el('p', 'setting__default',
        'Older backups beyond that count are deleted from the destination after each '
        + 'successful upload.'));

    const actions = el('div', 'settings-actions');
    const save = el('button', 'btn btn--primary', 'Save schedule');
    const status = el('span', 'settings-actions__status');
    save.addEventListener('click', async () => {
        status.textContent = '';
        try {
            const result = await api('/api/backups/schedule', json('PUT', {
                'backup.hub_enabled': enabled.checked,
                'backup.hub_destination': select.value,
                'backup.hub_interval_hours': Number(document.getElementById('schedule-interval').value),
                'backup.hub_keep_generations': Number(document.getElementById('schedule-keep').value),
            }));
            state.schedule = result.schedule;
            render();
        } catch (e) {
            status.textContent = e.message;
        }
    });
    actions.appendChild(save);
    actions.appendChild(status);
    card.appendChild(actions);
    return card;
}

function numberField(id, label, value, min, max) {
    const wrap = el('div');
    const labelEl = el('label', 'setting__label', label);
    labelEl.htmlFor = id;
    wrap.appendChild(labelEl);
    const input = el('input', 'input');
    input.type = 'number';
    input.id = id;
    input.min = String(min);
    input.max = String(max);
    input.value = value === undefined || value === null ? '' : String(value);
    input.style.width = '100%';
    wrap.appendChild(input);
    return wrap;
}

function renderRunsCard() {
    const card = el('div', 'card');
    card.style.marginTop = 'var(--space-5)';
    card.appendChild(el('h2', 'section-title', 'Recent backups'));

    if (!state.runs.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, 'No backups have run yet.'));
        empty.appendChild(el('p', 'stat-card__meta',
            'Press "Back up now" to take one immediately, or turn the schedule on.'));
        card.appendChild(empty);
        return card;
    }

    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    ['Started', 'Status', 'Destination', 'Size', 'Took', 'Trigger'].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    state.runs.forEach((run) => body.appendChild(renderRunRow(run)));
    table.appendChild(body);
    card.appendChild(table);
    return card;
}

function renderRunRow(run) {
    const row = el('tr');
    row.appendChild(el('td', null, fmtTime(run.started_at)));

    const statusCell = el('td');
    statusCell.appendChild(el('span', `bk-dot bk-dot--${run.status}`));
    statusCell.appendChild(document.createTextNode(run.status));
    if (run.status === 'failed' && run.error) {
        // The provider's own words, not a paraphrase: "SignatureDoesNotMatch" is the
        // whole diagnosis, and rewording it into "upload failed" throws that away.
        statusCell.appendChild(el('div', 'bk-error', run.error));
    } else if (run.object_key) {
        statusCell.appendChild(el('div', 'bk-error', run.object_key));
    }
    row.appendChild(statusCell);

    row.appendChild(el('td', null, run.destination_name || '(deleted destination)'));

    const sizeCell = el('td', null, fmtBytes(run.stored_bytes));
    if (run.source_bytes && run.stored_bytes) {
        sizeCell.appendChild(el('div', 'setting__default',
            `from ${fmtBytes(run.source_bytes)}`));
    }
    row.appendChild(sizeCell);

    row.appendChild(el('td', null, fmtDuration(run.started_at, run.finished_at)));
    row.appendChild(el('td', null, run.trigger));
    return row;
}

document.getElementById('run-now').addEventListener('click', async () => {
    const destination = (state.schedule && state.schedule.destination_id)
        || (state.destinations[0] && state.destinations[0].id);
    if (!destination) {
        alert('Add a destination first.');
        return;
    }
    try {
        await api('/api/backups/run', json('POST', { destination_id: destination }));
        state.schedule = Object.assign({}, state.schedule, { running: true });
        render();
        schedulePoll();
    } catch (e) {
        alert(e.message);
    }
});

// ---------------------------------------------------------------- backup settings
//
// The per-PC policy: which folders are backed up on every managed machine. The whole
// point of the token grammar is that this is written ONCE and keeps being right as people
// come and go, so the editor leads with the token reference and a live preview against a
// real machine — a pattern you cannot see the effect of is a pattern you cannot trust.

settingsPane.addEventListener('tab:shown', () => { renderSettingsPane(); refreshPreview(); });

function renderSettingsPane() {
    // Preserve focus across the re-render: this pane redraws on every chip add, and
    // yanking focus out of the text field after each one makes it unusable.
    const active = document.activeElement;
    const focusId = active && active.id ? active.id : null;
    const caret = active && active.selectionStart;

    settingsPane.replaceChildren();
    const files = state.files || {};

    // ---- policy card ----
    const policy = el('div', 'card');
    policy.appendChild(el('h2', 'section-title', 'What gets backed up on managed PCs'));
    policy.appendChild(el('p', 'stat-card__meta',
        'Applies to every machine unless that machine overrides it on its own Backup tab. '
        + 'Paths are expanded on each PC, so one pattern covers everyone — including '
        + 'people who sign in for the first time next week.'));

    const grid = el('div', 'bk-schedule-grid');

    const enabledWrap = el('div');
    const enabledLabel = el('label', 'checkbox');
    const enabled = el('input');
    enabled.type = 'checkbox';
    enabled.id = 'files-enabled';
    enabled.checked = !!files.enabled;
    enabledLabel.appendChild(enabled);
    enabledLabel.appendChild(document.createTextNode(' Back up files on managed PCs'));
    enabledWrap.appendChild(enabledLabel);
    enabledWrap.appendChild(el('p', 'setting__default',
        files.enabled ? 'Machines are backed up on the schedule below.'
                      : 'Nothing on any PC is backed up while this is off.'));
    grid.appendChild(enabledWrap);

    const destWrap = el('div');
    destWrap.appendChild(el('label', 'setting__label', 'Destination'));
    destWrap.appendChild(destinationSelect('files-destination', files.destination_id));
    grid.appendChild(destWrap);

    grid.appendChild(numberField('files-interval', 'Back up every (hours)',
                                 files.interval_hours, 1, 720));
    grid.appendChild(numberField('files-full-every', 'Full backup every (runs)',
                                 files.full_every, 1, 90));
    grid.appendChild(numberField('files-keep-chains', 'Keep this many chains',
                                 files.keep_chains, 1, 52));
    grid.appendChild(numberField('files-max-file', 'Skip files bigger than (MB)',
                                 files.max_file_mb, 1, 102400));
    grid.appendChild(numberField('files-max-set', 'Abort a run bigger than (GB)',
                                 files.max_set_gb, 1, 10240));

    const vssWrap = el('div');
    const vssLabel = el('label', 'checkbox');
    const vss = el('input');
    vss.type = 'checkbox';
    vss.id = 'files-vss';
    vss.checked = files.use_vss !== false;
    vssLabel.appendChild(vss);
    vssLabel.appendChild(document.createTextNode(' Use a shadow copy (VSS)'));
    vssWrap.appendChild(vssLabel);
    vssWrap.appendChild(el('p', 'setting__default',
        'Captures files that are open, like an Outlook PST.'));
    grid.appendChild(vssWrap);

    policy.appendChild(grid);
    settingsPane.appendChild(policy);

    // ---- path editors ----
    const paths = el('div', 'card');
    paths.style.marginTop = 'var(--space-5)';
    paths.appendChild(el('h2', 'section-title', 'Paths'));
    paths.appendChild(pathEditor(
        'Include', 'include', draftInclude,
        'A folder to back up. Use %Users% (or %User%) to cover every profile, or %Desktop% to follow '
        + 'each user’s real Desktop even when OneDrive has redirected it.',
        'e.g. %Desktop% or %User%\\Scripts'));
    paths.appendChild(pathEditor(
        'Never back up', 'exclude', draftExclude,
        'Matched against the whole path, case-insensitively. A pattern with no backslash '
        + 'matches on filename anywhere; ** crosses folders. Excluding a folder also '
        + 'excludes everything inside it.',
        'e.g. *.tmp or **\\node_modules\\**'));
    paths.appendChild(tokenReference());
    settingsPane.appendChild(paths);

    // ---- save ----
    const actions = el('div', 'settings-actions');
    const save = el('button', 'btn btn--primary', 'Save backup settings');
    const status = el('span', 'settings-actions__status');
    status.id = 'files-save-status';
    save.addEventListener('click', () => saveFileSettings(status));
    actions.appendChild(save);
    actions.appendChild(status);
    settingsPane.appendChild(actions);

    settingsPane.appendChild(renderPreviewCard());
    settingsPane.appendChild(renderExceptionsCard());

    if (focusId) {
        const restored = document.getElementById(focusId);
        if (restored) {
            restored.focus();
            if (caret !== null && caret !== undefined && restored.setSelectionRange) {
                try { restored.setSelectionRange(caret, caret); } catch (e) { /* not a text input */ }
            }
        }
    }
}

function destinationSelect(id, selected) {
    const select = el('select', 'input');
    select.id = id;
    select.style.width = '100%';
    const blank = el('option', null, 'Choose a destination…');
    blank.value = '';
    select.appendChild(blank);
    state.destinations.forEach((d) => {
        const option = el('option', null, d.name);
        option.value = d.id;
        if (d.id === selected) option.selected = true;
        select.appendChild(option);
    });
    return select;
}

// One chip list per path list. Built with createElement throughout — these strings are
// operator input echoed straight back, and a path is a perfectly good place to hide
// markup.
function pathEditor(title, kind, values, help, placeholder) {
    const wrap = el('div');
    wrap.appendChild(el('h3', 'perm-subhead', title));
    wrap.appendChild(el('p', 'setting__default', help));

    const chips = el('div', 'chip-list');
    values.forEach((value, index) => {
        const chip = el('span', 'chip');
        chip.appendChild(el('span', 'chip__name', value));
        const remove = el('button', 'chip__remove');
        remove.type = 'button';
        remove.textContent = '×';
        remove.setAttribute('aria-label', `Remove ${value}`);
        remove.addEventListener('click', () => {
            values.splice(index, 1);
            draftDirty = true;
            renderSettingsPane();
            refreshPreview();
        });
        chip.appendChild(remove);
        chips.appendChild(chip);
    });
    wrap.appendChild(chips);

    const adder = el('div', 'chip-add');
    const input = el('input', 'input');
    input.id = `path-input-${kind}`;
    input.placeholder = placeholder;
    input.autocomplete = 'off';
    input.spellcheck = false;
    const add = el('button', 'btn', 'Add');
    const commit = () => {
        const value = input.value.trim();
        if (!value) return;
        values.push(value);
        input.value = '';
        draftDirty = true;
        renderSettingsPane();
        refreshPreview();
    };
    add.addEventListener('click', commit);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
    });
    adder.appendChild(input);
    adder.appendChild(add);
    wrap.appendChild(adder);
    return wrap;
}

function tokenReference() {
    const wrap = el('details', 'bk-tokens');
    const summary = el('summary', null, 'Available tokens');
    wrap.appendChild(summary);
    const table = el('table', 'data-table');
    const body = el('tbody');
    (state.path_tokens || []).forEach((entry) => {
        const row = el('tr');
        const name = el('td');
        name.appendChild(el('code', null, entry.token));
        row.appendChild(name);
        row.appendChild(el('td', null, entry.help));
        body.appendChild(row);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
}

async function saveFileSettings(status) {
    status.textContent = '';
    try {
        const result = await api('/api/backups/schedule', json('PUT', {
            'backup.files_enabled': document.getElementById('files-enabled').checked,
            'backup.files_destination': document.getElementById('files-destination').value,
            'backup.files_include': draftInclude,
            'backup.files_exclude': draftExclude,
            'backup.files_interval_hours': Number(document.getElementById('files-interval').value),
            'backup.files_full_every': Number(document.getElementById('files-full-every').value),
            'backup.files_keep_chains': Number(document.getElementById('files-keep-chains').value),
            'backup.files_max_file_mb': Number(document.getElementById('files-max-file').value),
            'backup.files_max_set_gb': Number(document.getElementById('files-max-set').value),
            'backup.files_use_vss': document.getElementById('files-vss').checked,
        }));
        state.files = result.files;
        // The server normalises patterns (separators, duplicates), so adopt what it
        // stored rather than what was typed — otherwise the editor and the policy
        // disagree until the next reload.
        draftInclude = (result.files.include || []).slice();
        draftExclude = (result.files.exclude || []).slice();
        draftDirty = false;
        renderSettingsPane();
        refreshPreview();
        document.getElementById('files-save-status').textContent = 'Saved.';
    } catch (e) {
        status.textContent = e.message;
    }
}

// ---- preview ----

function renderPreviewCard() {
    const card = el('div', 'card');
    card.style.marginTop = 'var(--space-5)';
    card.appendChild(el('h2', 'section-title', 'Preview'));
    card.appendChild(el('p', 'stat-card__meta',
        'What these patterns resolve to on a real machine, using the profiles its agent '
        + 'last reported. This is the only way to see that a folder is redirected into '
        + 'OneDrive before a restore comes up empty.'));

    const picker = el('div', 'chip-add');
    const input = el('input', 'input');
    input.id = 'preview-machine';
    input.placeholder = 'Machine name';
    input.setAttribute('list', 'preview-machine-options');
    input.autocomplete = 'off';
    input.value = previewMachine;
    const list = el('datalist');
    list.id = 'preview-machine-options';
    machineOptions.forEach((name) => {
        const option = el('option');
        option.value = name;
        list.appendChild(option);
    });
    input.addEventListener('change', () => {
        previewMachine = input.value.trim();
        refreshPreview();
    });
    picker.appendChild(input);
    picker.appendChild(list);
    card.appendChild(picker);

    const body = el('div');
    body.id = 'preview-body';
    card.appendChild(body);
    return card;
}

// Debounced: the pane re-renders on every chip change, and each one would otherwise be a
// request.
function refreshPreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
        const body = document.getElementById('preview-body');
        if (!body) return;
        try {
            const result = await api('/api/backups/preview', json('POST', {
                machine: previewMachine,
                include: draftInclude,
                exclude: draftExclude,
            }));
            renderPreview(body, result);
        } catch (e) {
            body.replaceChildren(el('p', 'setting__error', e.message));
        }
    }, 350);
}

function renderPreview(body, result) {
    body.replaceChildren();
    if (!previewMachine) {
        body.appendChild(el('p', 'setting__default',
            'Choose a machine to see what these patterns actually cover on it.'));
        return;
    }
    if (!result.has_profiles) {
        body.appendChild(el('p', 'setting__default',
            `${previewMachine} has not reported its user profiles yet — its agent sends `
            + 'them on the next heartbeat after an upgrade. Until then these patterns '
            + 'cannot be resolved here; they will still expand correctly on the machine.'));
        return;
    }

    const preview = result.preview || {};
    if (preview.roots && preview.roots.length) {
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        ['Folder', 'User', 'From'].forEach((label) => headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);
        const tbody = el('tbody');
        preview.roots.forEach((root) => {
            const row = el('tr');
            row.appendChild(el('td', null, root.path));
            row.appendChild(el('td', null, root.user || '—'));
            row.appendChild(el('td', null, root.pattern));
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        body.appendChild(table);
    } else {
        body.appendChild(el('p', 'setting__default',
            'These patterns cover nothing on this machine.'));
    }

    (preview.problems || []).forEach((problem) => {
        body.appendChild(el('p', 'setting__error', problem));
    });
}

// ---- machines that differ from the fleet policy ----

function renderExceptionsCard() {
    const card = el('div', 'card');
    card.style.marginTop = 'var(--space-5)';
    card.appendChild(el('h2', 'section-title', 'Machines with their own settings'));
    const body = el('div');
    body.id = 'exceptions-body';
    body.appendChild(el('p', 'setting__default', 'Loading…'));
    card.appendChild(body);
    loadExceptions();
    return card;
}

async function loadExceptions() {
    let result;
    try {
        result = await api('/api/backups/machines');
    } catch (e) {
        return;
    }
    const body = document.getElementById('exceptions-body');
    if (!body) return;
    body.replaceChildren();
    if (!result.machines.length) {
        body.appendChild(el('p', 'setting__default',
            'Every machine follows the settings above. Override one from its own Backup tab.'));
        return;
    }
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    ['Machine', 'Backups', 'Destination', 'Extra paths'].forEach(
        (label) => headRow.appendChild(el('th', null, label)));
    head.appendChild(headRow);
    table.appendChild(head);
    const tbody = el('tbody');
    result.machines.forEach((m) => {
        const row = el('tr');
        const nameCell = el('td');
        const link = el('a', null, m.machine);
        link.href = `/machine/${encodeURIComponent(m.machine)}#backup`;
        nameCell.appendChild(link);
        row.appendChild(nameCell);
        row.appendChild(el('td', null,
            m.overridden.enabled ? (m.enabled ? 'on (override)' : 'OFF (override)')
                                 : (m.enabled ? 'on' : 'off')));
        row.appendChild(el('td', null, m.overridden.destination_id
            ? destinationName(m.destination_id) : 'fleet default'));
        const extra = (m.extra_include || []).concat(m.extra_exclude || []);
        row.appendChild(el('td', null, extra.length ? extra.join(', ') : '—'));
        tbody.appendChild(row);
    });
    table.appendChild(tbody);
    body.appendChild(table);
}

function destinationName(id) {
    const found = state.destinations.find((d) => d.id === id);
    return found ? found.name : '(deleted destination)';
}

// Populated from /api/machines, which is already scope-filtered — the same source the
// packages page uses for its target picker, rather than a second roster query.
let machineOptions = [];

async function loadMachineOptions() {
    try {
        const machines = await api('/api/machines');
        machineOptions = (machines || []).map((m) => m.machine || m.name).filter(Boolean);
    } catch (e) { /* the picker just stays empty */ }
}

// ---------------------------------------------------------------- destinations

destinationsPane.addEventListener('tab:shown', () => renderDestinations());

function renderDestinations() {
    destinationsPane.replaceChildren();

    const bar = el('div', 'toolbar');
    bar.style.justifyContent = 'flex-end';
    bar.style.marginBottom = 'var(--space-4)';
    const add = el('button', 'btn btn--primary', 'New destination');
    add.addEventListener('click', () => openDestination(null));
    bar.appendChild(add);
    destinationsPane.appendChild(bar);

    if (!state.destinations.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, 'No destinations configured.'));
        empty.appendChild(el('p', 'stat-card__meta',
            'A destination is an S3-compatible bucket or a WebDAV share. Credentials are '
            + 'encrypted on this server and never shown again.'));
        destinationsPane.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    ['Destination', 'Kind', 'Where', 'Credentials', ''].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    state.destinations.forEach((dest) => body.appendChild(renderDestinationRow(dest)));
    table.appendChild(body);
    card.appendChild(table);
    destinationsPane.appendChild(card);
}

function whereSummary(dest) {
    const config = dest.config || {};
    if (dest.kind === 's3') {
        const prefix = config.prefix ? `/${config.prefix}` : '';
        return `${config.bucket}${prefix} @ ${config.endpoint}`;
    }
    const prefix = config.prefix ? `/${config.prefix}` : '';
    return `${config.base_url}${prefix}`;
}

function renderDestinationRow(dest) {
    const row = el('tr');
    const nameCell = el('td');
    nameCell.appendChild(el('div', null, dest.name));
    if (state.schedule && state.schedule.destination_id === dest.id) {
        nameCell.appendChild(el('div', 'setting__default', 'scheduled backups go here'));
    }
    row.appendChild(nameCell);
    row.appendChild(el('td', null, dest.kind));
    row.appendChild(el('td', null, whereSummary(dest)));
    row.appendChild(el('td', null, dest.has_credentials ? 'stored' : 'MISSING'));

    const actions = el('td');
    const edit = el('button', 'btn', 'Edit');
    edit.addEventListener('click', () => openDestination(dest));
    const remove = el('button', 'btn', 'Delete');
    remove.style.marginLeft = 'var(--space-2)';
    remove.addEventListener('click', async () => {
        if (!confirm(`Delete destination "${dest.name}"? Backups already uploaded to it `
                     + 'are NOT deleted, but the hub will no longer be able to reach them.')) return;
        try {
            await api(`/api/backups/destinations/${dest.id}`, json('DELETE'));
            await load();
        } catch (e) {
            alert(e.message);
        }
    });
    actions.appendChild(edit);
    actions.appendChild(remove);
    row.appendChild(actions);
    return row;
}

// ---------------------------------------------------------------- destination editor

function renderKindChooser() {
    const wrap = document.getElementById('dest-kinds');
    wrap.replaceChildren();
    (state.destination_kinds || []).forEach((kind) => {
        const row = el('label', 'perm-capability');
        const radio = el('input');
        radio.type = 'radio';
        radio.name = 'dest-kind';
        radio.value = kind.name;
        radio.checked = kind.name === draftKind;
        // The kind is fixed once a destination exists: changing it would mean the stored
        // credentials no longer match the shape being asked for, and the honest fix is a
        // new destination rather than an edit that silently invalidates a secret.
        radio.disabled = editingDestinationId !== null;
        radio.addEventListener('change', () => { draftKind = kind.name; syncKindPanes(); });
        row.appendChild(radio);
        const text = el('div');
        text.appendChild(el('span', 'perm-capability__label', kind.label));
        text.appendChild(el('span', 'perm-capability__help', kind.description));
        row.appendChild(text);
        wrap.appendChild(row);
    });
}

function syncKindPanes() {
    document.getElementById('dest-s3').hidden = draftKind !== 's3';
    document.getElementById('dest-webdav').hidden = draftKind !== 'webdav';
    const isS3 = draftKind === 's3';
    document.getElementById('dest-user-label').textContent = isS3 ? 'Access key id' : 'Username';
    document.getElementById('dest-secret-label').textContent = isS3 ? 'Secret access key' : 'Password';
    document.getElementById('dest-secret-help').textContent = editingDestinationId
        ? 'Leave both blank to keep the stored credentials unchanged.'
        : 'Stored encrypted on this server with the backup master key. Never shown again.';
}

function openDestination(dest) {
    editingDestinationId = dest ? dest.id : null;
    draftKind = dest ? dest.kind : (state.destination_kinds[0] || {}).name || 's3';
    destinationError.textContent = '';
    destinationStatus.textContent = '';

    const config = (dest && dest.config) || {};
    document.getElementById('destination-modal-title').textContent =
        dest ? `Edit ${dest.name}` : 'New destination';
    document.getElementById('dest-name').value = dest ? dest.name : '';
    document.getElementById('dest-endpoint').value = config.endpoint || '';
    document.getElementById('dest-region').value = config.region || '';
    document.getElementById('dest-bucket').value = config.bucket || '';
    document.getElementById('dest-s3-prefix').value = (dest && dest.kind === 's3' && config.prefix) || '';
    document.getElementById('dest-path-style').checked =
        config.path_style === undefined ? true : !!config.path_style;
    document.getElementById('dest-base-url').value = config.base_url || '';
    document.getElementById('dest-dav-prefix').value = (dest && dest.kind === 'webdav' && config.prefix) || '';
    document.getElementById('dest-user').value = '';
    document.getElementById('dest-secret').value = '';

    renderKindChooser();
    syncKindPanes();
    // Testing needs a saved destination — the probe runs server-side against stored
    // credentials, which a brand new one does not have yet.
    document.getElementById('destination-test').disabled = editingDestinationId === null;
    destinationModal.showModal();
}

function readDestinationForm() {
    const name = document.getElementById('dest-name').value.trim();
    const config = draftKind === 's3' ? {
        endpoint: document.getElementById('dest-endpoint').value.trim(),
        region: document.getElementById('dest-region').value.trim(),
        bucket: document.getElementById('dest-bucket').value.trim(),
        prefix: document.getElementById('dest-s3-prefix').value.trim(),
        path_style: document.getElementById('dest-path-style').checked,
    } : {
        base_url: document.getElementById('dest-base-url').value.trim(),
        prefix: document.getElementById('dest-dav-prefix').value.trim(),
    };
    const user = document.getElementById('dest-user').value.trim();
    const secretValue = document.getElementById('dest-secret').value;
    const secret = draftKind === 's3'
        ? { access_key_id: user, secret_access_key: secretValue }
        : { username: user, password: secretValue };
    return { name, kind: draftKind, config, secret };
}

document.getElementById('destination-save').addEventListener('click', async () => {
    destinationError.textContent = '';
    destinationStatus.textContent = '';
    const payload = readDestinationForm();
    try {
        if (editingDestinationId) {
            await api(`/api/backups/destinations/${editingDestinationId}`,
                      json('PUT', payload));
        } else {
            await api('/api/backups/destinations', json('POST', payload));
        }
        destinationModal.close();
        await load();
    } catch (e) {
        destinationError.textContent = e.message;
    }
});

document.getElementById('destination-test').addEventListener('click', async () => {
    destinationError.textContent = '';
    destinationStatus.textContent = 'Testing…';
    try {
        const result = await api(
            `/api/backups/destinations/${editingDestinationId}/test`, json('POST'));
        destinationStatus.textContent = result.detail;
    } catch (e) {
        destinationStatus.textContent = '';
        destinationError.textContent = e.message;
    }
});

document.getElementById('destination-cancel').addEventListener('click', () => {
    destinationModal.close();
});

loadMachineOptions();
load().catch((e) => {
    keyBanner.replaceChildren();
    const banner = el('div', 'bk-banner bk-banner--danger');
    const body = el('div', 'bk-banner__body');
    body.appendChild(el('div', 'bk-banner__title', 'Could not load backup configuration'));
    body.appendChild(el('div', 'bk-banner__text', e.message));
    banner.appendChild(body);
    keyBanner.appendChild(banner);
});
