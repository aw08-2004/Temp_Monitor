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

// Lives in the Tools page's Backup tab now, above the per-PC policy that backup-tab.js
// renders. What used to be this page's own tab strip (Hub / Settings / Destinations) is
// three folds instead: a tablist nested inside the Tools tablist would be two meanings for
// one control, and the operator would have had to work out which one the arrow keys meant.
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
        title = t('backups.key.missing_library_title');
        text = t('backups.key.missing_library_text');
    } else if (!key.configured) {
        title = t('backups.key.none_title');
        text = t('backups.key.none_text');
        const create = el('button', 'btn btn--primary', t('backups.key.create'));
        create.addEventListener('click', createKey);
        actions.push(create);
    } else if (!key.escrowed_at) {
        modifier = 'bk-banner--danger';
        title = t('backups.key.unescrowed_title');
        text = t('backups.key.unescrowed_text');
        const reveal = el('button', 'btn btn--primary', t('backups.key.reveal'));
        reveal.addEventListener('click', revealKey);
        actions.push(reveal);
    } else {
        modifier = '';
        title = t('backups.key.ok_title');
        text = t('backups.key.ok_text',
                 { id: key.key_id, when: fmtTime(key.escrowed_at) });
        const reveal = el('button', 'btn', t('backups.key.reveal'));
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
        keyError.textContent = t('backups.key.copied');
    } catch (e) {
        // Clipboard access is refused outside a secure context, and a hub reached over
        // plain http on a lab network is exactly that. Selecting the text is the fallback,
        // and .bk-key is user-select: all so one click takes the whole key.
        keyError.textContent = t('backups.key.copy_failed');
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
    card.appendChild(el('h2', 'section-title', t('backups.schedule.title')));

    if (!state.destinations.length) {
        card.appendChild(el('p', 'stat-card__meta', t('backups.schedule.no_destination')));
        return card;
    }

    const grid = el('div', 'bk-schedule-grid');

    const enabledWrap = el('div');
    const enabledLabel = el('label', 'checkbox');
    const enabled = el('input');
    enabled.type = 'checkbox';
    enabled.id = 'schedule-enabled';
    enabled.checked = !!schedule.enabled;
    enabled.addEventListener('change', saveSchedule);
    enabledLabel.appendChild(enabled);
    enabledLabel.appendChild(document.createTextNode(' ' + t('backups.schedule.enabled')));
    enabledWrap.appendChild(enabledLabel);
    // next_due_at is 0 for "never run, so due immediately" — a falsy number that would
    // otherwise render as the "it's off" message on a schedule that is very much on.
    let dueText;
    if (!schedule.enabled) {
        dueText = t('backups.schedule.off');
    } else if (!schedule.next_due_at || schedule.next_due_at * 1000 <= Date.now()) {
        dueText = t('backups.schedule.due_now');
    } else {
        dueText = t('backups.schedule.next_due', { when: fmtTime(schedule.next_due_at) });
    }
    enabledWrap.appendChild(el('p', 'setting__default', dueText));
    grid.appendChild(enabledWrap);

    const destWrap = el('div');
    destWrap.appendChild(el('label', 'setting__label', t('backups.schedule.destination')));
    const select = el('select', 'input');
    select.id = 'schedule-destination';
    select.style.width = '100%';
    const blank = el('option', null, t('backups.schedule.choose_destination'));
    blank.value = '';
    select.appendChild(blank);
    state.destinations.forEach((d) => {
        const option = el('option', null, d.name);
        option.value = d.id;
        if (d.id === schedule.destination_id) option.selected = true;
        select.appendChild(option);
    });
    select.addEventListener('change', saveSchedule);
    destWrap.appendChild(select);
    grid.appendChild(destWrap);

    grid.appendChild(numberField('schedule-interval', t('backups.schedule.interval'),
                                 schedule.interval_hours, 1, 720, saveSchedule));
    grid.appendChild(numberField('schedule-keep', t('backups.schedule.keep'),
                                 schedule.keep_generations, 1, 365, saveSchedule));
    card.appendChild(grid);

    card.appendChild(el('p', 'setting__default', t('backups.schedule.rotation_note')));

    // No Save button -- the checkbox, destination and numbers above save themselves. This
    // span is the feedback.
    const status = el('span', 'autosave', scheduleStatus.text);
    status.id = 'schedule-save-status';
    if (scheduleStatus.cls) status.className = `autosave ${scheduleStatus.cls}`;
    card.appendChild(status);
    return card;
}

// Persisted outside render() so a "Saved"/error message survives the re-render that a save
// triggers. The seq guard drops a slow response a newer edit has already superseded.
let scheduleStatus = { text: '', cls: '' };
let scheduleSaveSeq = 0;

function setScheduleStatus(text, cls) {
    scheduleStatus = { text, cls: cls || '' };
    const node = document.getElementById('schedule-save-status');
    if (node) {
        node.textContent = text;
        node.className = cls ? `autosave ${cls}` : 'autosave';
    }
}

async function saveSchedule() {
    const seq = ++scheduleSaveSeq;
    setScheduleStatus(t('common.saving'), '');
    try {
        const result = await api('/api/backups/schedule', json('PUT', {
            'backup.hub_enabled': document.getElementById('schedule-enabled').checked,
            'backup.hub_destination': document.getElementById('schedule-destination').value,
            'backup.hub_interval_hours': Number(document.getElementById('schedule-interval').value),
            'backup.hub_keep_generations': Number(document.getElementById('schedule-keep').value),
        }));
        if (seq !== scheduleSaveSeq) return;
        state.schedule = result.schedule;
        scheduleStatus = { text: t('common.saved'), cls: 'autosave--saved' };
        render();
    } catch (e) {
        if (seq !== scheduleSaveSeq) return;
        setScheduleStatus(e.message, 'autosave--error');
    }
}

function numberField(id, label, value, min, max, onCommit) {
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
    // Save on `change` (blur / Enter), not on every keystroke, so a half-typed number is
    // not sent and bounced against its min on the way to a valid one.
    if (onCommit) input.addEventListener('change', onCommit);
    wrap.appendChild(input);
    return wrap;
}

function renderRunsCard() {
    const card = el('div', 'card');
    card.style.marginTop = 'var(--space-5)';
    card.appendChild(el('h2', 'section-title', t('backups.runs.title')));

    if (!state.runs.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('backups.runs.empty')));
        empty.appendChild(el('p', 'stat-card__meta', t('backups.runs.empty_hint')));
        card.appendChild(empty);
        return card;
    }

    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    [t('backups.runs.col.started'), t('backups.runs.col.status'),
     t('backups.runs.col.destination'), t('backups.runs.col.size'),
     t('backups.runs.col.took'), t('backups.runs.col.trigger')].forEach((label) => {
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

// Run status and trigger are wire values, so they need display names. Literal keys per
// value rather than a computed one -- only literals are visible to the key scan in
// tests/test_i18n.py, and an unknown value showing itself beats showing a key.
const RUN_STATUS_LABELS = {
    running: () => t('backups.status.running'),
    succeeded: () => t('backups.status.succeeded'),
    failed: () => t('backups.status.failed'),
    cancelled: () => t('backups.status.cancelled'),
};

function runStatusLabel(status) {
    const get = RUN_STATUS_LABELS[status];
    return get ? get() : String(status || '');
}

const TRIGGER_LABELS = {
    schedule: () => t('backups.trigger.schedule'),
    manual: () => t('backups.trigger.manual'),
};

function triggerLabel(trigger) {
    const get = TRIGGER_LABELS[trigger];
    return get ? get() : String(trigger || '');
}

function renderRunRow(run) {
    const row = el('tr');
    row.appendChild(el('td', null, fmtTime(run.started_at)));

    const statusCell = el('td');
    statusCell.appendChild(el('span', `bk-dot bk-dot--${run.status}`));
    statusCell.appendChild(document.createTextNode(runStatusLabel(run.status)));
    if (run.status === 'failed' && run.error) {
        // The provider's own words, not a paraphrase: "SignatureDoesNotMatch" is the
        // whole diagnosis, and rewording it into "upload failed" throws that away.
        statusCell.appendChild(el('div', 'bk-error', run.error));
    } else if (run.object_key) {
        statusCell.appendChild(el('div', 'bk-error', run.object_key));
    }
    row.appendChild(statusCell);

    row.appendChild(el('td', null,
        run.destination_name || t('backups.runs.deleted_destination')));

    const sizeCell = el('td', null, fmtBytes(run.stored_bytes));
    if (run.source_bytes && run.stored_bytes) {
        sizeCell.appendChild(el('div', 'setting__default',
            t('backups.runs.from_size', { size: fmtBytes(run.source_bytes) })));
    }
    row.appendChild(sizeCell);

    row.appendChild(el('td', null, fmtDuration(run.started_at, run.finished_at)));
    row.appendChild(el('td', null, triggerLabel(run.trigger)));
    return row;
}

document.getElementById('run-now').addEventListener('click', async () => {
    const destination = (state.schedule && state.schedule.destination_id)
        || (state.destinations[0] && state.destinations[0].id);
    if (!destination) {
        alert(t('backups.runs.add_destination_first'));
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

// Rendered when its fold is opened rather than on load: the preview costs a round trip
// that resolves the token grammar against a real machine, and most visits to this tab are
// to look at the run history above.
foldFor(settingsPane).addEventListener('toggle', () => {
    if (foldFor(settingsPane).open) { renderSettingsPane(); refreshPreview(); }
});

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
    policy.appendChild(el('h2', 'section-title', t('backups.files.title')));
    policy.appendChild(el('p', 'stat-card__meta', t('backups.files.intro')));

    const grid = el('div', 'bk-schedule-grid');

    const enabledWrap = el('div');
    const enabledLabel = el('label', 'checkbox');
    const enabled = el('input');
    enabled.type = 'checkbox';
    enabled.id = 'files-enabled';
    enabled.checked = !!files.enabled;
    enabled.addEventListener('change', saveFileSettings);
    enabledLabel.appendChild(enabled);
    enabledLabel.appendChild(document.createTextNode(' ' + t('backups.files.enabled')));
    enabledWrap.appendChild(enabledLabel);
    enabledWrap.appendChild(el('p', 'setting__default',
        files.enabled ? t('backups.files.on_note') : t('backups.files.off_note')));
    grid.appendChild(enabledWrap);

    const destWrap = el('div');
    destWrap.appendChild(el('label', 'setting__label', t('backups.schedule.destination')));
    const filesDest = destinationSelect('files-destination', files.destination_id);
    filesDest.addEventListener('change', saveFileSettings);
    destWrap.appendChild(filesDest);
    grid.appendChild(destWrap);

    grid.appendChild(numberField('files-interval', t('backups.files.interval'),
                                 files.interval_hours, 1, 720, saveFileSettings));
    grid.appendChild(numberField('files-full-every', t('backups.files.full_every'),
                                 files.full_every, 1, 90, saveFileSettings));
    grid.appendChild(numberField('files-keep-chains', t('backups.files.keep_chains'),
                                 files.keep_chains, 1, 52, saveFileSettings));
    grid.appendChild(numberField('files-max-file', t('backups.files.max_file'),
                                 files.max_file_mb, 1, 102400, saveFileSettings));
    grid.appendChild(numberField('files-max-set', t('backups.files.max_set'),
                                 files.max_set_gb, 1, 10240, saveFileSettings));
    grid.appendChild(numberField('files-max-concurrent', t('backups.files.max_concurrent'),
                                 files.max_concurrent, 0, 100, saveFileSettings));

    const vssWrap = el('div');
    const vssLabel = el('label', 'checkbox');
    const vss = el('input');
    vss.type = 'checkbox';
    vss.id = 'files-vss';
    vss.checked = files.use_vss !== false;
    vss.addEventListener('change', saveFileSettings);
    vssLabel.appendChild(vss);
    vssLabel.appendChild(document.createTextNode(' ' + t('backups.files.vss')));
    vssWrap.appendChild(vssLabel);
    vssWrap.appendChild(el('p', 'setting__default', t('backups.files.vss_note')));
    grid.appendChild(vssWrap);

    policy.appendChild(grid);
    settingsPane.appendChild(policy);

    // ---- path editors ----
    const paths = el('div', 'card');
    paths.style.marginTop = 'var(--space-5)';
    paths.appendChild(el('h2', 'section-title', t('backups.files.paths')));
    paths.appendChild(pathEditor(
        t('backups.files.include'), 'include', draftInclude,
        t('backups.files.include_help'), t('backups.files.include_placeholder')));
    paths.appendChild(pathEditor(
        t('backups.files.exclude'), 'exclude', draftExclude,
        t('backups.files.exclude_help'), t('backups.files.exclude_placeholder')));
    paths.appendChild(tokenReference());
    settingsPane.appendChild(paths);

    // ---- auto-save status ----
    // No Save button: every control above (and the path chips) saves itself. This is the
    // only feedback.
    const status = el('span', 'autosave', filesStatus.text);
    status.id = 'files-save-status';
    if (filesStatus.cls) status.className = `autosave ${filesStatus.cls}`;
    settingsPane.appendChild(status);

    settingsPane.appendChild(renderRunFleetCard());
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
    const blank = el('option', null, t('backups.schedule.choose_destination'));
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
        remove.setAttribute('aria-label', t('backups.files.remove', { value }));
        remove.addEventListener('click', () => {
            values.splice(index, 1);
            draftDirty = true;
            renderSettingsPane();
            refreshPreview();
            saveFileSettings();
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
    const add = el('button', 'btn', t('backups.files.add'));
    const commit = () => {
        const value = input.value.trim();
        if (!value) return;
        values.push(value);
        input.value = '';
        draftDirty = true;
        renderSettingsPane();
        refreshPreview();
        saveFileSettings();
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
    const summary = el('summary', null, t('backups.files.tokens'));
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

// ---- back up the whole fleet now ----

let fleetRunBusy = false;
let fleetRunMessage = '';
let fleetRunError = '';

function renderRunFleetCard() {
    const card = el('div', 'card');
    card.style.marginTop = 'var(--space-5)';
    card.appendChild(el('h2', 'section-title', t('backups.fleet_run.title')));
    card.appendChild(el('p', 'stat-card__meta', t('backups.fleet_run.intro')));

    const actions = el('div', 'card-actions');
    const run = el('button', 'btn btn--primary',
                   fleetRunBusy ? t('backups.fleet_run.working')
                                : t('backups.fleet_run.run_all'));
    run.id = 'files-run-fleet';
    run.disabled = fleetRunBusy;
    run.addEventListener('click', runFleetBackup);
    actions.appendChild(run);

    // Always offered, not gated on "is anything running" — the console does not track
    // live per-machine state on this page, and a cancel with nothing to stop is a
    // harmless no-op that says so. The counterpart to "Back up all PCs now".
    const cancel = el('button', 'btn btn--danger',
                      fleetRunBusy ? t('backups.fleet_run.working')
                                   : t('backups.fleet_run.cancel_all'));
    cancel.id = 'files-cancel-fleet';
    cancel.disabled = fleetRunBusy;
    cancel.addEventListener('click', cancelFleetBackup);
    actions.appendChild(cancel);

    const status = el('span',
                      fleetRunError ? 'setting__error' : 'settings-actions__status');
    status.textContent = fleetRunError || fleetRunMessage;
    actions.appendChild(status);
    card.appendChild(actions);
    return card;
}

async function cancelFleetBackup() {
    if (fleetRunBusy) return;
    fleetRunBusy = true;
    fleetRunMessage = '';
    fleetRunError = '';
    renderSettingsPane();
    try {
        const result = await api('/api/backups/files/cancel', json('POST', {}));
        const parts = [];
        if (result.requests_cleared) {
            parts.push(t('backups.fleet_run.queued_dropped',
                         { count: result.requests_cleared }));
        }
        if (result.stopped_before_start) {
            parts.push(t('backups.fleet_run.stopped_before_start',
                         { count: result.stopped_before_start }));
        }
        // Named separately because these are the ones cancel cannot fully stop: the PC
        // is already uploading and will finish, with its result discarded.
        if (result.stopped_in_flight) {
            parts.push(t('backups.fleet_run.stopped_in_flight',
                         { count: result.stopped_in_flight }));
        }
        fleetRunMessage = parts.length ? parts.join(', ') + '.'
                                       : t('backups.fleet_run.nothing_to_cancel');
    } catch (e) {
        fleetRunError = e.message;
    } finally {
        fleetRunBusy = false;
        renderSettingsPane();
    }
}

async function runFleetBackup() {
    if (fleetRunBusy) return;
    fleetRunBusy = true;
    fleetRunMessage = '';
    fleetRunError = '';
    renderSettingsPane();
    try {
        const result = await api('/api/backups/files/run', json('POST', {}));
        // Reported as three separate numbers on purpose. "Queued 40" would read as
        // success while nothing had actually started, and "skipped" is the number that
        // tells an operator their policy excludes machines they thought it covered.
        const parts = [];
        if (result.started) {
            parts.push(t('backups.fleet_run.started', { count: result.started }));
        }
        if (result.queued) {
            parts.push(t('backups.fleet_run.queued', { count: result.queued }));
        }
        if (result.skipped) {
            parts.push(t('backups.fleet_run.skipped', { count: result.skipped }));
        }
        fleetRunMessage = parts.length ? parts.join(', ') + '.'
                                       : t('backups.fleet_run.none_configured');
    } catch (e) {
        fleetRunError = e.message;
    } finally {
        fleetRunBusy = false;
        renderSettingsPane();
    }
}

// Persisted outside render() so the message survives the re-render a save triggers.
let filesStatus = { text: '', cls: '' };
let filesSaveSeq = 0;

function setFilesStatus(text, cls) {
    filesStatus = { text, cls: cls || '' };
    const node = document.getElementById('files-save-status');
    if (node) {
        node.textContent = text;
        node.className = cls ? `autosave ${cls}` : 'autosave';
    }
}

async function saveFileSettings() {
    const seq = ++filesSaveSeq;
    setFilesStatus(t('common.saving'), '');
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
            'backup.files_max_concurrent':
                Number(document.getElementById('files-max-concurrent').value),
        }));
        if (seq !== filesSaveSeq) return;   // a later save is already in flight
        state.files = result.files;
        // The server normalises patterns (separators, duplicates), so adopt what it
        // stored rather than what was typed — otherwise the editor and the policy
        // disagree until the next reload.
        draftInclude = (result.files.include || []).slice();
        draftExclude = (result.files.exclude || []).slice();
        draftDirty = false;
        filesStatus = { text: t('common.saved'), cls: 'autosave--saved' };
        renderSettingsPane();
        refreshPreview();
    } catch (e) {
        if (seq !== filesSaveSeq) return;
        setFilesStatus(e.message, 'autosave--error');
    }
}

// ---- preview ----

function renderPreviewCard() {
    const card = el('div', 'card');
    card.style.marginTop = 'var(--space-5)';
    card.appendChild(el('h2', 'section-title', t('backups.preview.title')));
    card.appendChild(el('p', 'stat-card__meta', t('backups.preview.intro')));

    const picker = el('div', 'chip-add');
    const input = el('input', 'input');
    input.id = 'preview-machine';
    input.placeholder = t('backups.preview.machine_placeholder');
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
        body.appendChild(el('p', 'setting__default', t('backups.preview.choose_machine')));
        return;
    }
    if (!result.has_profiles) {
        body.appendChild(el('p', 'setting__default',
            t('backups.preview.no_profiles', { machine: previewMachine })));
        return;
    }

    const preview = result.preview || {};
    if (preview.roots && preview.roots.length) {
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('backups.preview.col.folder'), t('backups.preview.col.user'),
         t('backups.preview.col.from')].forEach(
            (label) => headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);
        const tbody = el('tbody');
        preview.roots.forEach((root) => {
            const row = el('tr');
            row.appendChild(el('td', null, root.path));
            row.appendChild(el('td', null, root.user || t('backups.preview.no_user')));
            row.appendChild(el('td', null, root.pattern));
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        body.appendChild(table);
    } else {
        body.appendChild(el('p', 'setting__default', t('backups.preview.covers_nothing')));
    }

    (preview.problems || []).forEach((problem) => {
        body.appendChild(el('p', 'setting__error', problem));
    });
}

// ---- machines that differ from the fleet policy ----

function renderExceptionsCard() {
    const card = el('div', 'card');
    card.style.marginTop = 'var(--space-5)';
    card.appendChild(el('h2', 'section-title', t('backups.exceptions.title')));
    const body = el('div');
    body.id = 'exceptions-body';
    body.appendChild(el('p', 'setting__default', t('common.loading')));
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
        body.appendChild(el('p', 'setting__default', t('backups.exceptions.none')));
        return;
    }
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    [t('backups.exceptions.col.machine'), t('backups.exceptions.col.backups'),
     t('backups.exceptions.col.destination'), t('backups.exceptions.col.extra')].forEach(
        (label) => headRow.appendChild(el('th', null, label)));
    head.appendChild(headRow);
    table.appendChild(head);
    const tbody = el('tbody');
    result.machines.forEach((m) => {
        const row = el('tr');
        const nameCell = el('td');
        const link = el('a', null, m.machine);
        // Straight to this machine's policy in the half below, rather than to its page --
        // the per-PC backup view left /machine/<name> when the tools did.
        link.href = `/tools?tab=backup&machine=${encodeURIComponent(m.machine)}`;
        nameCell.appendChild(link);
        row.appendChild(nameCell);
        row.appendChild(el('td', null, m.overridden.enabled
            ? (m.enabled ? t('backups.exceptions.on_override')
                         : t('backups.exceptions.off_override'))
            : (m.enabled ? t('backups.exceptions.on') : t('backups.exceptions.off'))));
        row.appendChild(el('td', null, m.overridden.destination_id
            ? destinationName(m.destination_id)
            : t('backups.exceptions.fleet_default')));
        const extra = (m.extra_include || []).concat(m.extra_exclude || []);
        row.appendChild(el('td', null,
            extra.length ? extra.join(', ') : t('backups.exceptions.none_extra')));
        tbody.appendChild(row);
    });
    table.appendChild(tbody);
    body.appendChild(table);
}

function destinationName(id) {
    const found = state.destinations.find((d) => d.id === id);
    return found ? found.name : t('backups.runs.deleted_destination');
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

foldFor(destinationsPane).addEventListener('toggle', () => {
    if (foldFor(destinationsPane).open) renderDestinations();
});

function renderDestinations() {
    destinationsPane.replaceChildren();

    const bar = el('div', 'toolbar');
    bar.style.justifyContent = 'flex-end';
    bar.style.marginBottom = 'var(--space-4)';
    const add = el('button', 'btn btn--primary', t('backups.destinations.new'));
    add.addEventListener('click', () => openDestination(null));
    bar.appendChild(add);
    destinationsPane.appendChild(bar);

    if (!state.destinations.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('backups.destinations.empty')));
        empty.appendChild(el('p', 'stat-card__meta', t('backups.destinations.empty_hint')));
        destinationsPane.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    [t('backups.destinations.col.destination'), t('backups.destinations.col.kind'),
     t('backups.destinations.col.where'), t('backups.destinations.col.credentials'),
     ''].forEach((label) => {
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
        nameCell.appendChild(el('div', 'setting__default',
            t('backups.destinations.scheduled_here')));
    }
    row.appendChild(nameCell);
    row.appendChild(el('td', null, dest.kind));
    row.appendChild(el('td', null, whereSummary(dest)));
    row.appendChild(el('td', null, dest.has_credentials
        ? t('backups.destinations.stored') : t('backups.destinations.missing')));

    const actions = el('td', 'data-table__actions');
    const edit = el('button', 'btn', t('common.edit'));
    edit.addEventListener('click', () => openDestination(dest));
    const remove = el('button', 'btn', t('common.delete'));
    remove.addEventListener('click', async () => {
        if (!confirm(t('backups.destinations.confirm_delete', { name: dest.name }))) return;
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
    document.getElementById('dest-user-label').textContent = isS3
        ? t('backups.destinations.editor.access_key_id')
        : t('backups.destinations.editor.username');
    document.getElementById('dest-secret-label').textContent = isS3
        ? t('backups.destinations.editor.secret_access_key')
        : t('backups.destinations.editor.password');
    document.getElementById('dest-secret-help').textContent = editingDestinationId
        ? t('backups.destinations.editor.keep_credentials')
        : t('backups.destinations.editor.new_credentials');
}

function openDestination(dest) {
    editingDestinationId = dest ? dest.id : null;
    draftKind = dest ? dest.kind : (state.destination_kinds[0] || {}).name || 's3';
    destinationError.textContent = '';
    destinationStatus.textContent = '';

    const config = (dest && dest.config) || {};
    document.getElementById('destination-modal-title').textContent = dest
        ? t('backups.destinations.editor.edit_title', { name: dest.name })
        : t('backups.destinations.editor.new_title');
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
    destinationStatus.textContent = t('backups.destinations.editor.testing');
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

/** The <details> a pane sits in, so "became visible" is one question with one answer. */
function foldFor(pane) {
    return pane.closest('details');
}

/**
 * Boot when the Backup tab is first shown, not when the page parses.
 *
 * The Tools page loads every tool's script up front, and three of the four tabs are hidden
 * at that moment. Fetching the fleet backup state for somebody who came to open a terminal
 * is a request nobody asked for -- and on this tab in particular it is the request that
 * decides whether the master-key banner is shown, which should happen when the operator is
 * looking at it.
 */
function boot() {
    loadMachineOptions();
    load().catch(onLoadFailed);
    // Whichever folds are open on arrival still need their contents; the toggle listeners
    // above only fire on a change.
    if (foldFor(settingsPane).open) { renderSettingsPane(); refreshPreview(); }
    if (foldFor(destinationsPane).open) renderDestinations();
}

const backupPanel = document.getElementById('tool-backup');
let booted = false;
backupPanel.addEventListener('tab:shown', () => {
    if (booted) return;
    booted = true;
    boot();
});
if (!backupPanel.hidden) { booted = true; boot(); }

function onLoadFailed(e) {
    keyBanner.replaceChildren();
    const banner = el('div', 'bk-banner bk-banner--danger');
    const body = el('div', 'bk-banner__body');
    body.appendChild(el('div', 'bk-banner__title', t('backups.load_failed')));
    body.appendChild(el('div', 'bk-banner__text', e.message));
    banner.appendChild(body);
    keyBanner.appendChild(banner);
}
