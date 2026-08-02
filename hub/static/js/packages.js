// Packages page: define an installer, deploy it, watch it land.
//
// Same two rules as permissions.js, for the same reasons:
//
//  * Everything is built with textContent / createElement, never innerHTML. Package
//    names, hostnames and — most of all — installer output echoed back into the target
//    list are arbitrary strings from operators and agents.
//  * The vocabularies (detection kinds, source kinds, registry roots, retry defaults)
//    come from GET /api/packages, not a copy here. A hardcoded list silently stops
//    offering a new kind, which reads to an operator as "the feature is broken".
//
// The deployment view polls while a deploy is unresolved. The hub's scheduler ticks on
// its own interval, so the page is a viewer of that state, never a driver of it — there
// is no client-side retry or dispatch, and closing the tab does not stop a rollout.

const packagesPane = document.getElementById('packages-pane');
const deploymentsPane = document.getElementById('deployments-pane');

const packageModal = document.getElementById('package-modal');
const packageError = document.getElementById('package-error');
const deployModal = document.getElementById('deploy-modal');
const deployError = document.getElementById('deploy-error');
const progressModal = document.getElementById('progress-modal');
const progressBody = document.getElementById('progress-body');

let vocab = { detection_kinds: [], source_kinds: [], registry_roots: [], defaults: {} };
let editingPackageId = null;
let deployPackageId = null;
let draftMachines = [];
let openDeploymentId = null;
let pollTimer = null;

// Statuses that mean "nothing more will happen here". Mirrors packages.TARGET_TERMINAL;
// used only to decide whether to keep polling, so drift costs a wasted request, not
// correctness.
const TERMINAL = ['succeeded', 'failed', 'expired', 'cancelled'];

async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
    if (!resp.ok) throw new Error((body && body.error) || httpMessage(resp.status));
    return body;
}

// A bare status number is a dead end for the operator staring at it. 413 in particular
// never comes from the hub -- its own limit (deploy.max_upload_mb, 512 MB by default) is
// enforced in Python and answers 400 with a JSON reason. A 413 means the TLS terminator in
// front of the hub rejected the body before Flask ever saw it, and nginx's default
// client_max_body_size is 1 MB, so the first installer anyone uploads hits it.
function httpMessage(status) {
    if (status === 413) {
        return t('packages.http_413');
    }
    if (status === 502 || status === 504) {
        return t('packages.http_502', { status });
    }
    return t('packages.http_other', { status });
}

function json(method, payload) {
    // Content-Type: application/json is load-bearing, not cosmetic — it is what makes a
    // cross-origin POST preflight and fail. See fleet_web.py's module docstring.
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function fmtBytes(n) {
    if (!n && n !== 0) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function fmtTime(epoch) {
    if (!epoch) return '—';
    return new Date(epoch * 1000).toLocaleString();
}

// datetime-local has no timezone, so it is read as local time — which is what the
// operator meant when they typed it — and sent as a unix timestamp.
function toEpoch(value) {
    if (!value) return null;
    const ms = new Date(value).getTime();
    return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}

// ---------------------------------------------------------------- tabs
//
// tabs.js owns the switching (roles, roving tabindex, arrow keys, #hash, persistence).
// This page only reacts to a panel becoming visible: a deployment list goes stale within
// seconds, so it is refetched on show rather than left at whatever it said last time.
deploymentsPane.addEventListener('tab:shown', () => loadDeployments());

// ---------------------------------------------------------------- package list

function sourceSummary(source) {
    if (!source) return t('packages.no_payload');
    if (source.kind === 'winget') return t('packages.winget_ref', { ref: source.ref });
    if (source.kind === 'upload') return source.file_name || t('packages.uploaded_file');
    return source.ref || source.kind;
}

// What the Command column says for a step-based package. The kinds in order, because that
// is the one thing worth seeing at a glance from a list -- "zip → extract → pnputil" tells
// an operator which package this is; a truncated first command line does not.
function installSummary(pkg) {
    if (!pkg.steps || !pkg.steps.length) {
        return `${pkg.install_command || 'winget'} ${pkg.install_args || ''}`.trim();
    }
    return pkg.steps.map((step) => stepText(step.kind)[0]).join(' → ');
}

function renderPackages(list) {
    packagesPane.replaceChildren();
    if (!list.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('packages.empty')));
        empty.appendChild(el('p', 'stat-card__meta', t('packages.empty_hint')));
        packagesPane.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    [t('packages.col.package'), t('packages.col.payload'), t('packages.col.command'),
     t('packages.col.detection'), ''].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    list.forEach((pkg) => body.appendChild(renderPackageRow(pkg)));
    table.appendChild(body);
    card.appendChild(table);
    packagesPane.appendChild(card);
}

function detectionSummary(rule) {
    if (!rule || rule.kind === 'none') return t('packages.detect_exit_only');
    if (rule.kind === 'file_exists') return rule.path;
    if (rule.kind === 'registry_value') {
        const parts = { root: rule.root, key: rule.key, name: rule.name };
        return rule.equals === undefined
            ? t('packages.detect_registry', parts)
            : t('packages.detect_registry_equals', { ...parts, value: rule.equals });
    }
    if (rule.kind === 'installed_version') {
        return rule.min_version
            ? t('packages.detect_version_min', { name: rule.name, version: rule.min_version })
            : rule.name;
    }
    return rule.kind;
}

function renderPackageRow(pkg) {
    const tr = el('tr');

    const nameCell = el('td');
    nameCell.appendChild(el('div', null, pkg.version ? `${pkg.name} ${pkg.version}` : pkg.name));
    if (pkg.description) nameCell.appendChild(el('div', 'stat-card__meta', pkg.description));
    tr.appendChild(nameCell);

    const payloadCell = el('td');
    const sources = pkg.sources && pkg.sources.length
        ? pkg.sources : (pkg.source ? [pkg.source] : []);
    if (!sources.length) payloadCell.appendChild(el('div', null, t('packages.no_payload')));
    sources.forEach((source) => {
        // The slot name matters once there is more than one: it is what the steps say.
        payloadCell.appendChild(el('div', null, sources.length > 1
            ? t('packages.payload_named', { name: source.name, payload: sourceSummary(source) })
            : sourceSummary(source)));
        if (source.sha256) {
            payloadCell.appendChild(el('div', 'pkg-hash', source.sha256.slice(0, 16) + '…'));
        }
        if (source.file_size) {
            payloadCell.appendChild(el('div', 'stat-card__meta', fmtBytes(source.file_size)));
        }
    });
    tr.appendChild(payloadCell);

    const cmdCell = el('td');
    cmdCell.appendChild(el('div', 'pkg-hash', installSummary(pkg)));
    const meta = t('packages.exit_summary', { codes: pkg.success_exit_codes.join(', '),
                                              timeout: pkg.timeout_seconds });
    cmdCell.appendChild(el('div', 'stat-card__meta', (pkg.steps && pkg.steps.length)
        ? `${tPlural('packages.step_count', pkg.steps.length)} · ${meta}`
        : meta));
    tr.appendChild(cmdCell);

    tr.appendChild(el('td', 'pkg-target-error', detectionSummary(pkg.detection)));

    const actions = el('td');
    const deployBtn = el('button', 'btn btn--primary', t('packages.deploy'));
    deployBtn.type = 'button';
    deployBtn.addEventListener('click', () => openDeploy(pkg));
    actions.appendChild(deployBtn);

    const editBtn = el('button', 'btn', t('common.edit'));
    editBtn.type = 'button';
    editBtn.style.marginLeft = 'var(--space-2)';
    editBtn.addEventListener('click', () => openPackage(pkg));
    actions.appendChild(editBtn);

    const delBtn = el('button', 'btn', t('common.delete'));
    delBtn.type = 'button';
    delBtn.style.marginLeft = 'var(--space-2)';
    delBtn.addEventListener('click', async () => {
        if (!confirm(t('packages.confirm_delete', { package: pkg.name }))) return;
        try {
            await api(`/api/packages/${encodeURIComponent(pkg.id)}`, { method: 'DELETE' });
            loadPackages();
        } catch (e) { alert(e.message); }
    });
    actions.appendChild(delBtn);
    tr.appendChild(actions);

    return tr;
}

async function loadPackages() {
    try {
        const doc = await api('/api/packages');
        vocab = doc;
        renderPackages(doc.packages);
    } catch (e) {
        packagesPane.replaceChildren(el('p', 'setting__error', e.message));
    }
}

// ---------------------------------------------------------------- package editor
//
// The editor holds a package as two arrays — payloads and steps — and rebuilds the DOM
// from them. Field edits write straight into the array WITHOUT re-rendering; only
// structural changes (add, remove, reorder, change a kind) redraw. Re-rendering on every
// keystroke would move focus out of the box being typed in, which is exactly the bug a
// naive "re-render on change" editor ships with.

let draftPayloads = [];   // {name, kind, ref, sha256, file_name, file_size, file}
let draftSteps = [];

function installMode() {
    const checked = document.querySelector('input[name="install-mode"]:checked');
    return checked ? checked.value : 'command';
}

// Spelled out per kind rather than built from `'packages.source.' + kind`: a computed
// key is invisible to the literal-key scan in tests/test_i18n.py, and a source kind added
// server-side without catalog entries would then label its own option with a key.
const SOURCE_LABELS = {
    upload: () => [t('packages.source.upload.label'), t('packages.source.upload.help')],
    winget: () => [t('packages.source.winget.label'), t('packages.source.winget.help')],
    url: () => [t('packages.source.url.label'), t('packages.source.url.help')],
    unc: () => [t('packages.source.unc.label'), t('packages.source.unc.help')],
};

function sourceText(kind) {
    const get = SOURCE_LABELS[kind];
    return get ? get() : [kind, ''];
}

const STEP_LABELS = {
    run: () => [t('packages.step.run.label'), t('packages.step.run.description')],
    powershell: () => [t('packages.step.powershell.label'), t('packages.step.powershell.description')],
    winget: () => [t('packages.step.winget.label'), t('packages.step.winget.description')],
    extract: () => [t('packages.step.extract.label'), t('packages.step.extract.description')],
    pnputil: () => [t('packages.step.pnputil.label'), t('packages.step.pnputil.description')],
};

function stepText(kind) {
    // The server sends label+description for every step kind; these literals are the
    // fallback for a kind this page is older than, and they keep the key scan honest.
    const served = (vocab.step_kinds || []).find((k) => k.name === kind);
    if (served) return [served.label, served.description];
    const get = STEP_LABELS[kind];
    return get ? get() : [kind, ''];
}

const REF_PLACEHOLDERS = {
    winget: '7zip.7zip',
    url: 'https://example.com/drivers.zip',
    unc: '\\\\fileserver\\software\\installer.msi',
};

// ---- small field builders. Each returns a labelled block wired to a setter. ----

function fieldBlock(labelText, control, hint) {
    const wrap = el('div');
    const label = el('label', 'setting__label', labelText);
    if (hint) {
        label.appendChild(document.createTextNode(' '));
        label.appendChild(el('span', 'setting__default', hint));
    }
    label.htmlFor = control.id;
    wrap.appendChild(label);
    wrap.appendChild(control);
    return wrap;
}

let controlSeq = 0;

function textField(labelText, value, onInput, options) {
    const opts = options || {};
    const input = document.createElement(opts.multiline ? 'textarea' : 'input');
    input.className = 'input';
    input.id = `pkg-field-${++controlSeq}`;
    input.value = value || '';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.style.width = '100%';
    if (opts.placeholder) input.placeholder = opts.placeholder;
    if (opts.multiline) input.rows = opts.rows || 5;
    if (opts.type) input.type = opts.type;
    input.addEventListener('input', () => onInput(input.value));
    return fieldBlock(labelText, input, opts.hint);
}

function selectField(labelText, value, choices, onChange) {
    const select = document.createElement('select');
    select.className = 'input';
    select.id = `pkg-field-${++controlSeq}`;
    select.style.width = '100%';
    choices.forEach(([choiceValue, choiceLabel]) => {
        const option = document.createElement('option');
        option.value = choiceValue;
        option.textContent = choiceLabel;
        select.appendChild(option);
    });
    select.value = value;
    select.addEventListener('change', () => onChange(select.value));
    return fieldBlock(labelText, select);
}

function checkboxField(labelText, checked, onChange) {
    const wrap = el('label', 'checkbox');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = !!checked;
    box.addEventListener('change', () => onChange(box.checked));
    wrap.appendChild(box);
    wrap.appendChild(document.createTextNode(' ' + labelText));
    return wrap;
}

function row(...blocks) {
    const grid = el('div', 'pkg-row');
    blocks.forEach((block) => grid.appendChild(block));
    return grid;
}

// ---- payloads ----

// Every name a step may use at this point in the list. Payload names are bound before
// step 1; an extract step binds its own name for the steps after it. Mirrors
// packages.validate_steps so the hint matches what the server will accept.
function boundVariables(uptoStepIndex) {
    const names = [vocab.work_variable || 'work'];
    draftPayloads.forEach((p) => { if (p.name) names.push(p.name); });
    if (draftPayloads.length === 1 && draftPayloads[0].name !== 'file') names.push('file');
    draftSteps.slice(0, uptoStepIndex === undefined ? draftSteps.length : uptoStepIndex)
        .forEach((step) => {
            if (step.kind !== 'extract') return;
            // A blank save_as is not "no name" — the server picks one. Mirroring that
            // choice here (packages.validate_steps) is what lets the hint name the folder
            // the next step has to point at, instead of leaving the operator to guess.
            names.push(step.save_as || autoExtractName(names));
        });
    return names;
}

function autoExtractName(taken) {
    let name = 'extracted';
    let suffix = 2;
    while (taken.includes(name)) name = `extracted${suffix++}`;
    return name;
}

function syncVariableHint() {
    const hint = document.getElementById('step-variables');
    if (!hint) return;
    hint.textContent = t('packages.editor.variables_help',
        { variables: boundVariables().map((n) => `{${n}}`).join(', ') });
}

function renderPayloads() {
    const host = document.getElementById('payload-list');
    host.replaceChildren();
    if (!draftPayloads.length) {
        host.appendChild(el('p', 'setting__default', t('packages.editor.no_payloads')));
    }
    draftPayloads.forEach((payload, index) => {
        const card = el('div', 'pkg-card');

        const head = el('div', 'pkg-card__head');
        head.appendChild(el('span', 'pkg-card__title',
            t('packages.editor.payload_n', { number: index + 1 })));
        const remove = el('button', 'btn pkg-card__btn', t('common.delete'));
        remove.type = 'button';
        remove.addEventListener('click', () => {
            draftPayloads.splice(index, 1);
            renderPayloads();
            renderSteps();
        });
        head.appendChild(remove);
        card.appendChild(head);

        card.appendChild(row(
            textField(t('packages.editor.payload_name'), payload.name,
                (v) => { payload.name = v; syncVariableHint(); },
                { placeholder: vocab.defaults.source_name || 'payload',
                  hint: t('packages.editor.payload_name_hint') }),
            selectField(t('packages.editor.payload_kind'), payload.kind,
                (vocab.source_kinds || []).map((kind) => [kind, sourceText(kind)[0]]),
                (v) => { payload.kind = v; renderPayloads(); })));

        card.appendChild(el('p', 'setting__default', sourceText(payload.kind)[1]));

        if (payload.kind === 'upload') {
            const picker = document.createElement('input');
            picker.className = 'input';
            picker.type = 'file';
            picker.style.width = '100%';
            picker.addEventListener('change', () => {
                payload.file = picker.files.length ? picker.files[0] : null;
                state.textContent = payload.file
                    ? t('packages.editor.will_upload', { file: payload.file.name })
                    : payloadState(payload);
            });
            card.appendChild(picker);
            const state = el('p', 'setting__default', payloadState(payload));
            payload.stateNode = state;   // so the upload progress line can find it
            card.appendChild(state);
        } else {
            card.appendChild(textField(t('packages.editor.location'), payload.ref,
                (v) => { payload.ref = v; },
                { placeholder: REF_PLACEHOLDERS[payload.kind] || '' }));
            // winget has its own trust chain, so a hash pin there is meaningless.
            if (payload.kind !== 'winget') {
                card.appendChild(textField(t('packages.editor.sha256'), payload.sha256,
                    (v) => { payload.sha256 = v; },
                    { hint: t('packages.editor.sha256_hint') }));
            }
        }
        host.appendChild(card);
    });
    syncVariableHint();
    syncInstallPanes();
}

function payloadState(payload) {
    if (payload.file) return t('packages.editor.will_upload', { file: payload.file.name });
    if (payload.file_name) {
        return t('packages.editor.current_payload',
            { file: payload.file_name, size: fmtBytes(payload.file_size) });
    }
    return t('packages.editor.upload_help');
}

document.getElementById('add-payload').addEventListener('click', () => {
    // The first payload takes the default name so the common one-payload package needs no
    // naming at all; later ones are numbered, and the operator renames them.
    const base = vocab.defaults.source_name || 'payload';
    let name = base;
    let n = 2;
    while (draftPayloads.some((p) => p.name === name)) name = `${base}${n++}`;
    draftPayloads.push({ name, kind: 'upload', ref: '', sha256: '', file: null });
    renderPayloads();
});

// ---- steps ----

function renderSteps() {
    const host = document.getElementById('step-list');
    host.replaceChildren();
    if (!draftSteps.length) {
        host.appendChild(el('p', 'setting__default', t('packages.editor.no_steps')));
    }
    draftSteps.forEach((step, index) => host.appendChild(renderStep(step, index)));
    syncVariableHint();
}

function moveStep(from, to) {
    if (to < 0 || to >= draftSteps.length) return;
    const [moved] = draftSteps.splice(from, 1);
    draftSteps.splice(to, 0, moved);
    renderSteps();
}

function renderStep(step, index) {
    const card = el('div', 'pkg-card');
    const head = el('div', 'pkg-card__head');
    head.appendChild(el('span', 'pkg-card__title',
        t('packages.editor.step_n', { number: index + 1, kind: stepText(step.kind)[0] })));

    const buttons = el('span', 'pkg-card__actions');
    [[t('packages.editor.move_up'), () => moveStep(index, index - 1)],
     [t('packages.editor.move_down'), () => moveStep(index, index + 1)],
     [t('common.delete'), () => { draftSteps.splice(index, 1); renderSteps(); }],
    ].forEach(([label, action]) => {
        const button = el('button', 'btn pkg-card__btn', label);
        button.type = 'button';
        button.addEventListener('click', action);
        buttons.appendChild(button);
    });
    head.appendChild(buttons);
    card.appendChild(head);

    card.appendChild(textField(t('packages.editor.step_label'), step.name,
        (v) => { step.name = v; }, { placeholder: stepText(step.kind)[0] }));

    stepFields(step).forEach((node) => card.appendChild(node));

    // Per-step overrides. Blank means "use the package's own", which is why these are text
    // rather than number inputs pre-filled with the package value — a pre-filled box would
    // silently freeze the package default into every step.
    card.appendChild(row(
        textField(t('packages.editor.step_timeout'), step.timeout_seconds,
            (v) => { step.timeout_seconds = v; },
            { type: 'number', hint: t('packages.editor.inherits') }),
        textField(t('packages.editor.step_exit_codes'), step.success_exit_codes,
            (v) => { step.success_exit_codes = v; },
            { hint: step.kind === 'pnputil'
                ? t('packages.editor.pnputil_codes')
                : t('packages.editor.inherits') })));

    const carryOn = checkboxField(t('packages.editor.continue_on_error'),
        step.continue_on_error, (v) => { step.continue_on_error = v; });
    carryOn.style.marginTop = 'var(--space-3)';
    card.appendChild(carryOn);
    return card;
}

function stepFields(step) {
    if (step.kind === 'run') {
        return [row(
            textField(t('packages.editor.command'), step.command,
                (v) => { step.command = v; }, { placeholder: 'msiexec.exe' }),
            textField(t('packages.editor.args'), step.args,
                (v) => { step.args = v; }, { placeholder: '/i "{file}" /qn /norestart' }))];
    }
    if (step.kind === 'powershell') {
        return [textField(t('packages.editor.script'), step.script,
            (v) => { step.script = v; },
            { multiline: true, placeholder: 'Copy-Item "{work}\\config.xml" "C:\\ProgramData\\App\\"' })];
    }
    if (step.kind === 'winget') {
        return [row(
            textField(t('packages.editor.winget_id'), step.id,
                (v) => { step.id = v; }, { placeholder: '7zip.7zip' }),
            textField(t('packages.editor.args'), step.args,
                (v) => { step.args = v; }, { hint: t('common.optional') }))];
    }
    if (step.kind === 'extract') {
        const first = draftPayloads.length ? `{${draftPayloads[0].name}}` : '{payload}';
        return [
            row(textField(t('packages.editor.archive'), step.archive,
                    (v) => { step.archive = v; }, { placeholder: first }),
                textField(t('packages.editor.dest'), step.dest,
                    (v) => { step.dest = v; },
                    { hint: t('packages.editor.dest_hint'), placeholder: '{work}\\drivers' })),
            textField(t('packages.editor.save_as'), step.save_as,
                (v) => { step.save_as = v; syncVariableHint(); },
                { hint: t('packages.editor.save_as_hint'), placeholder: 'extracted' }),
        ];
    }
    // pnputil
    return [
        textField(t('packages.editor.driver_path'), step.path,
            (v) => { step.path = v; },
            { placeholder: '{extracted}', hint: t('packages.editor.driver_path_hint') }),
        checkboxField(t('packages.editor.subdirs'), step.subdirs !== false,
            (v) => { step.subdirs = v; }),
    ];
}

function renderStepPalette() {
    const select = document.getElementById('step-kind');
    select.replaceChildren();
    (vocab.step_kinds || []).forEach((kind) => {
        const option = document.createElement('option');
        option.value = kind.name;
        option.textContent = kind.label;
        select.appendChild(option);
    });
    const help = () => {
        document.getElementById('step-kind-help').textContent = stepText(select.value)[1];
    };
    select.addEventListener('change', help);
    help();
}

document.getElementById('add-step').addEventListener('click', () => {
    const kind = document.getElementById('step-kind').value;
    if (draftSteps.length >= (vocab.max_steps || 25)) {
        packageError.textContent = t('packages.editor.too_many_steps',
            { max: vocab.max_steps || 25 });
        return;
    }
    draftSteps.push({ kind, subdirs: true });
    renderSteps();
});

document.querySelectorAll('input[name="install-mode"]').forEach((radio) => {
    radio.addEventListener('change', syncInstallPanes);
});

function syncInstallPanes() {
    const steps = installMode() === 'steps';
    document.getElementById('install-command-pane').hidden = steps;
    document.getElementById('install-steps-pane').hidden = !steps;
    if (steps) return;

    // Single-command mode is the original recipe and still assumes exactly one payload.
    const kind = draftPayloads.length ? draftPayloads[0].kind : 'upload';
    const command = document.getElementById('pkg-command');
    command.disabled = kind === 'winget';
    command.placeholder = kind === 'winget'
        ? t('packages.editor.command_winget_placeholder')
        : t('packages.editor.command_placeholder');
    document.getElementById('pkg-cmd-help').textContent = kind === 'winget'
        ? t('packages.editor.cmd_help_winget')
        : t('packages.editor.cmd_help',
            { placeholder: vocab.file_placeholder || '{file}' });
}

function renderDetectionKinds() {
    const select = document.getElementById('pkg-detect-kind');
    select.replaceChildren();
    vocab.detection_kinds.forEach((kind) => {
        const option = document.createElement('option');
        option.value = kind.name;
        option.textContent = kind.label;
        select.appendChild(option);
    });
    const roots = document.getElementById('detect-root');
    roots.replaceChildren();
    vocab.registry_roots.forEach((root) => {
        const option = document.createElement('option');
        option.value = root;
        option.textContent = root;
        roots.appendChild(option);
    });
    select.addEventListener('change', syncDetectionPanes);
}

function syncDetectionPanes() {
    const kind = document.getElementById('pkg-detect-kind').value;
    document.getElementById('detect-file').hidden = kind !== 'file_exists';
    document.getElementById('detect-registry').hidden = kind !== 'registry_value';
    document.getElementById('detect-version').hidden = kind !== 'installed_version';
    const found = vocab.detection_kinds.find((k) => k.name === kind);
    document.getElementById('pkg-detect-help').textContent = found ? found.description : '';
}

function openPackage(pkg) {
    editingPackageId = pkg ? pkg.id : null;
    packageError.textContent = '';
    document.getElementById('package-modal-title').textContent = pkg
        ? t('packages.editor.edit_title', { package: pkg.name })
        : t('packages.editor.new_title');

    document.getElementById('pkg-name').value = pkg ? pkg.name : '';
    document.getElementById('pkg-version').value = (pkg && pkg.version) || '';
    document.getElementById('pkg-description').value = (pkg && pkg.description) || '';
    document.getElementById('pkg-timeout').value = pkg ? pkg.timeout_seconds : 900;
    document.getElementById('pkg-command').value = (pkg && pkg.install_command) || '';
    document.getElementById('pkg-args').value = (pkg && pkg.install_args) || '';
    document.getElementById('pkg-exit-codes').value =
        (pkg ? pkg.success_exit_codes : (vocab.defaults.success_exit_codes || [0, 3010])).join(', ');

    // Copies, not the fetched objects: the editor mutates these as the operator types, and
    // a cancelled edit must leave the list behind it untouched.
    const existing = (pkg && pkg.sources) || (pkg && pkg.source ? [pkg.source] : null);
    draftPayloads = (existing || [{ kind: 'upload', name: vocab.defaults.source_name || 'payload' }])
        .map((source) => ({
            name: source.name || vocab.defaults.source_name || 'payload',
            kind: source.kind || 'upload',
            ref: source.ref || '',
            // An upload's hash is the hub's, not something to re-type; a url/unc hash is
            // the operator's pin and is theirs to edit.
            sha256: source.kind === 'upload' ? '' : (source.sha256 || ''),
            file_name: source.file_name || '',
            file_size: source.file_size || 0,
            stored_sha256: source.sha256 || '',
            file: null,
        }));
    draftSteps = ((pkg && pkg.steps) || []).map((step) => ({
        ...step,
        // Both arrive typed from the server and are edited as text here.
        timeout_seconds: step.timeout_seconds || '',
        success_exit_codes: (step.success_exit_codes || []).join(', '),
    }));

    const mode = draftSteps.length ? 'steps' : 'command';
    const modeRadio = document.querySelector(`input[name="install-mode"][value="${mode}"]`);
    if (modeRadio) modeRadio.checked = true;

    renderPayloads();
    renderSteps();

    const rule = (pkg && pkg.detection) || { kind: 'none' };
    document.getElementById('pkg-detect-kind').value = rule.kind;
    document.getElementById('detect-path').value = rule.path || '';
    document.getElementById('detect-root').value = rule.root || 'HKLM';
    document.getElementById('detect-key').value = rule.key || '';
    document.getElementById('detect-name').value = rule.name || '';
    const hasEquals = rule.equals !== undefined;
    document.getElementById('detect-equals-on').checked = hasEquals;
    document.getElementById('detect-equals').disabled = !hasEquals;
    document.getElementById('detect-equals').value = hasEquals ? rule.equals : '';
    document.getElementById('detect-product').value =
        rule.kind === 'installed_version' ? (rule.name || '') : '';
    document.getElementById('detect-min').value = rule.min_version || '';
    syncDetectionPanes();

    packageModal.showModal();
}

function collectDetection() {
    const kind = document.getElementById('pkg-detect-kind').value;
    if (kind === 'file_exists') {
        return { kind, path: document.getElementById('detect-path').value };
    }
    if (kind === 'registry_value') {
        const rule = {
            kind,
            root: document.getElementById('detect-root').value,
            key: document.getElementById('detect-key').value,
            name: document.getElementById('detect-name').value,
        };
        // Only send `equals` when the operator asked for an exact match — omitting it is
        // what means "the value merely has to exist", and an empty string is a real
        // (different) requirement.
        if (document.getElementById('detect-equals-on').checked) {
            rule.equals = document.getElementById('detect-equals').value;
        }
        return rule;
    }
    if (kind === 'installed_version') {
        const rule = { kind, name: document.getElementById('detect-product').value };
        const min = document.getElementById('detect-min').value.trim();
        if (min) rule.min_version = min;
        return rule;
    }
    return { kind: 'none' };
}

// Upload every payload the operator picked a new file for, one at a time. Serial rather
// than Promise.all on purpose: these are installers, and three 400 MB uploads racing each
// other through one proxy is how the read timeout in packages.http_502 gets hit.
async function uploadPayloads() {
    for (const payload of draftPayloads) {
        if (payload.kind !== 'upload' || !payload.file) continue;
        const form = new FormData();
        form.append('file', payload.file);
        if (payload.stateNode) payload.stateNode.textContent = t('packages.editor.uploading');
        const result = await api('/api/packages/upload', { method: 'POST', body: form });
        payload.stored_sha256 = result.sha256;
        payload.file_name = result.file_name;
        payload.file_size = result.file_size;
        payload.file = null;
        if (payload.stateNode) {
            payload.stateNode.textContent = t('packages.editor.uploaded',
                { file: result.file_name, size: fmtBytes(result.file_size),
                  sha256: result.sha256.slice(0, 16) });
        }
    }
}

function collectSources() {
    return draftPayloads.map((payload) => {
        if (payload.kind === 'upload') {
            // The stored hash carries over untouched when no new file was chosen, so
            // editing a command line doesn't mean re-uploading 200 MB.
            if (!payload.stored_sha256) throw new Error(t('packages.editor.choose_file'));
            return { name: payload.name, kind: 'upload', sha256: payload.stored_sha256,
                     file_name: payload.file_name, file_size: payload.file_size };
        }
        const source = { name: payload.name, kind: payload.kind, ref: payload.ref };
        const sha = (payload.sha256 || '').trim();
        if (sha && payload.kind !== 'winget') source.sha256 = sha;
        return source;
    });
}

// Blank overrides are DROPPED rather than sent as empty strings: absent means "inherit the
// package's timeout / exit codes", and the server distinguishes the two.
function collectSteps() {
    if (installMode() !== 'steps') return [];
    // An empty list would reach the server as "no steps", which it reads as the
    // single-command recipe and rejects for having no command — a message about a field
    // this operator cannot even see right now.
    if (!draftSteps.length) throw new Error(t('packages.editor.no_steps'));
    return draftSteps.map((step) => {
        const out = { kind: step.kind };
        ['name', 'command', 'args', 'script', 'id', 'archive', 'dest', 'save_as', 'path']
            .forEach((field) => {
                const value = (step[field] || '').trim ? (step[field] || '').trim() : step[field];
                if (value) out[field] = value;
            });
        if (step.kind === 'pnputil') out.subdirs = step.subdirs !== false;
        if (String(step.timeout_seconds || '').trim()) {
            out.timeout_seconds = Number(step.timeout_seconds);
        }
        if (String(step.success_exit_codes || '').trim()) {
            out.success_exit_codes = step.success_exit_codes;
        }
        if (step.continue_on_error) out.continue_on_error = true;
        return out;
    });
}

document.getElementById('package-save').addEventListener('click', async () => {
    packageError.textContent = '';
    const saveBtn = document.getElementById('package-save');
    saveBtn.disabled = true;
    try {
        await uploadPayloads();

        const steps = collectSteps();
        const commandMode = installMode() === 'command';
        const payload = {
            name: document.getElementById('pkg-name').value,
            version: document.getElementById('pkg-version').value,
            description: document.getElementById('pkg-description').value,
            sources: collectSources(),
            steps,
            // A package stores one shape or the other, so switching to steps has to CLEAR
            // the old command line rather than leave it sitting there for the server to
            // reject with a message about a field the operator can no longer see.
            install_command: (commandMode && !document.getElementById('pkg-command').disabled)
                ? document.getElementById('pkg-command').value : '',
            install_args: commandMode ? document.getElementById('pkg-args').value : '',
            timeout_seconds: Number(document.getElementById('pkg-timeout').value),
            success_exit_codes: document.getElementById('pkg-exit-codes').value,
            detection: collectDetection(),
        };

        if (editingPackageId) {
            await api(`/api/packages/${encodeURIComponent(editingPackageId)}`, json('PUT', payload));
        } else {
            await api('/api/packages', json('POST', payload));
        }
        packageModal.close();
        loadPackages();
    } catch (e) {
        packageError.textContent = e.message;
    } finally {
        saveBtn.disabled = false;
    }
});

document.getElementById('package-cancel').addEventListener('click', () => packageModal.close());
document.getElementById('new-package').addEventListener('click', () => openPackage(null));
document.getElementById('detect-equals-on').addEventListener('change', (e) => {
    document.getElementById('detect-equals').disabled = !e.target.checked;
});

// ---------------------------------------------------------------- deploy

function renderMachineChips() {
    const host = document.getElementById('deploy-machine-chips');
    host.replaceChildren();
    draftMachines.forEach((machine) => {
        const chip = el('span', 'chip');
        chip.appendChild(el('span', 'chip__name', machine));
        const remove = el('button', 'chip__remove', '×');
        remove.type = 'button';
        remove.addEventListener('click', () => {
            draftMachines = draftMachines.filter((m) => m !== machine);
            renderMachineChips();
        });
        chip.appendChild(remove);
        host.appendChild(chip);
    });
}

function addMachine() {
    const input = document.getElementById('deploy-machine-input');
    const name = input.value.trim();
    if (name && !draftMachines.includes(name)) {
        draftMachines.push(name);
        renderMachineChips();
    }
    input.value = '';
}

document.getElementById('deploy-machine-add').addEventListener('click', addMachine);
document.getElementById('deploy-machine-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addMachine(); }
});

function openDeploy(pkg) {
    deployPackageId = pkg.id;
    draftMachines = [];
    deployError.textContent = '';
    renderMachineChips();
    document.getElementById('deploy-modal-title').textContent =
        t('packages.deploy_editor.title_for', { package: pkg.name });
    document.getElementById('deploy-start').value = '';
    document.getElementById('deploy-end').value = '';
    document.getElementById('deploy-note').value = '';
    document.getElementById('deploy-attempts').value = vocab.defaults.max_attempts || 3;
    document.getElementById('deploy-backoff').value = vocab.defaults.retry_backoff_seconds || 900;
    deployModal.showModal();
}

document.getElementById('deploy-cancel').addEventListener('click', () => deployModal.close());

document.getElementById('deploy-save').addEventListener('click', async () => {
    deployError.textContent = '';
    try {
        const created = await api('/api/deployments', json('POST', {
            package_id: deployPackageId,
            machines: draftMachines,
            note: document.getElementById('deploy-note').value,
            window_start: toEpoch(document.getElementById('deploy-start').value),
            window_end: toEpoch(document.getElementById('deploy-end').value),
            max_attempts: Number(document.getElementById('deploy-attempts').value),
            retry_backoff_seconds: Number(document.getElementById('deploy-backoff').value),
        }));
        deployModal.close();
        // Click the real tab rather than toggling classes: tabs.js is the single owner of
        // which tab is selected, and a second writer is how the underline ends up on one
        // tab while the other's panel is showing. The tab:shown handler reloads the list.
        document.getElementById('tab-btn-deployments').click();
        openProgress(created.id);
    } catch (e) {
        deployError.textContent = e.message;
    }
});

// ---------------------------------------------------------------- deployments

// Deployment and per-machine statuses are wire values ('in_flight'), so they need a
// display name. Literal keys per status rather than a computed one, for the same reason
// SOURCE_LABELS is spelled out: the key scan in tests/test_i18n.py only sees literals.
const STATUS_LABELS = {
    scheduled: () => t('packages.status.scheduled'),
    running: () => t('packages.status.running'),
    complete: () => t('packages.status.complete'),
    cancelled: () => t('packages.status.cancelled'),
    pending: () => t('packages.status.pending'),
    in_flight: () => t('packages.status.in_flight'),
    succeeded: () => t('packages.status.succeeded'),
    failed: () => t('packages.status.failed'),
    expired: () => t('packages.status.expired'),
};

function statusLabel(status) {
    const get = STATUS_LABELS[status];
    // An unknown status shows itself rather than a key: it can only come from a hub
    // newer than this page, and the raw word is more use than nothing.
    return get ? get() : String(status || '').replace('_', ' ');
}

const STATUS_ORDER = ['succeeded', 'in_flight', 'pending', 'failed', 'expired', 'cancelled'];

function renderProgressBar(counts, total) {
    const bar = el('div', 'pkg-progress');
    if (!total) return bar;
    STATUS_ORDER.forEach((status) => {
        const n = counts[status] || 0;
        if (!n) return;
        const seg = el('div', `pkg-progress__seg pkg-progress__seg--${status}`);
        seg.style.width = `${(n / total) * 100}%`;
        bar.appendChild(seg);
    });
    return bar;
}

function renderTally(counts) {
    const tally = el('div', 'pkg-tally');
    STATUS_ORDER.forEach((status) => {
        const n = counts[status] || 0;
        if (n) {
            tally.appendChild(el('span', null,
                t('packages.deployments.tally', { status: statusLabel(status), count: n })));
        }
    });
    return tally;
}

function renderDeployments(list) {
    deploymentsPane.replaceChildren();
    if (!list.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('packages.deployments.empty')));
        empty.appendChild(el('p', 'stat-card__meta', t('packages.deployments.empty_hint')));
        deploymentsPane.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    [t('packages.deployments.col.package'), t('packages.deployments.col.scheduled'),
     t('packages.deployments.col.by'), t('packages.deployments.col.status'),
     t('packages.deployments.col.progress'), ''].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    list.forEach((dep) => {
        const tr = el('tr');
        const nameCell = el('td');
        // A deployment outlives the package definition on purpose, so this can be null.
        nameCell.appendChild(el('div', null,
            dep.package_name || t('packages.deployments.deleted_package')));
        if (dep.note) nameCell.appendChild(el('div', 'stat-card__meta', dep.note));
        tr.appendChild(nameCell);

        const whenCell = el('td');
        whenCell.appendChild(el('div', null, fmtTime(dep.created_at)));
        if (dep.window_start) {
            whenCell.appendChild(el('div', 'stat-card__meta',
                t('packages.deployments.starts', { when: fmtTime(dep.window_start) })));
        }
        if (dep.window_end) {
            whenCell.appendChild(el('div', 'stat-card__meta',
                t('packages.deployments.gives_up', { when: fmtTime(dep.window_end) })));
        }
        tr.appendChild(whenCell);

        tr.appendChild(el('td', 'stat-card__meta', dep.created_by));
        tr.appendChild(el('td', null, statusLabel(dep.status)));

        const progressCell = el('td');
        progressCell.appendChild(renderProgressBar(dep.target_counts, dep.target_total));
        progressCell.appendChild(renderTally(dep.target_counts));
        tr.appendChild(progressCell);

        const actions = el('td');
        const view = el('button', 'btn', t('packages.deployments.view'));
        view.type = 'button';
        view.addEventListener('click', () => openProgress(dep.id));
        actions.appendChild(view);
        tr.appendChild(actions);

        body.appendChild(tr);
    });
    table.appendChild(body);
    card.appendChild(table);
    deploymentsPane.appendChild(card);
}

async function loadDeployments() {
    try {
        const doc = await api('/api/deployments');
        renderDeployments(doc.deployments);
    } catch (e) {
        deploymentsPane.replaceChildren(el('p', 'setting__error', e.message));
    }
}

// ---------------------------------------------------------------- progress view

function renderProgress(deployment) {
    document.getElementById('progress-title').textContent =
        t('packages.deployments.progress_title', {
            package: deployment.package_name || t('packages.deployments.deleted_package'),
            status: statusLabel(deployment.status),
        });
    progressBody.replaceChildren();
    progressBody.appendChild(renderProgressBar(deployment.target_counts, deployment.target_total));
    progressBody.appendChild(renderTally(deployment.target_counts));

    const table = el('table', 'data-table');
    table.style.marginTop = 'var(--space-4)';
    const head = el('thead');
    const headRow = el('tr');
    [t('packages.deployments.target_col.machine'), t('packages.deployments.target_col.status'),
     t('packages.deployments.target_col.attempts'),
     t('packages.deployments.target_col.detail')].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    deployment.targets.forEach((target) => {
        const tr = el('tr');
        tr.appendChild(el('td', null, target.machine));
        tr.appendChild(el('td', null, statusLabel(target.status)));
        tr.appendChild(el('td', null, String(target.attempts)));

        const detail = el('td');
        if (target.last_error) {
            detail.appendChild(el('div', 'pkg-target-error', target.last_error));
        } else if (target.next_attempt_at) {
            detail.appendChild(el('div', 'stat-card__meta',
                t('packages.deployments.retries_at',
                  { when: fmtTime(target.next_attempt_at) })));
        }
        tr.appendChild(detail);
        body.appendChild(tr);
    });
    table.appendChild(body);
    progressBody.appendChild(table);

    // Keep watching only while something can still change. The hub's scheduler is what
    // actually advances the deploy; this is a viewer, so a closed tab costs nothing.
    const unresolved = deployment.targets.some((t) => !TERMINAL.includes(t.status));
    clearTimeout(pollTimer);
    if (unresolved && progressModal.open) {
        pollTimer = setTimeout(() => refreshProgress(), 5000);
    }
}

async function refreshProgress() {
    if (!openDeploymentId || !progressModal.open) return;
    try {
        renderProgress(await api(`/api/deployments/${encodeURIComponent(openDeploymentId)}`));
    } catch (e) {
        progressBody.replaceChildren(el('p', 'setting__error', e.message));
    }
}

async function openProgress(deploymentId) {
    openDeploymentId = deploymentId;
    progressBody.replaceChildren(el('p', 'stat-card__meta', t('common.loading')));
    progressModal.showModal();
    await refreshProgress();
}

document.getElementById('progress-close').addEventListener('click', () => {
    clearTimeout(pollTimer);
    progressModal.close();
    loadDeployments();
});

document.getElementById('progress-cancel-deploy').addEventListener('click', async () => {
    if (!confirm(t('packages.deployments.confirm_cancel'))) return;
    try {
        // Content-Type is load-bearing, not habit: app.login_required refuses a POST
        // without it, which is what keeps a cross-site form from cancelling a deploy.
        renderProgress(await api(
            `/api/deployments/${encodeURIComponent(openDeploymentId)}/cancel`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }));
    } catch (e) { alert(e.message); }
});

document.getElementById('progress-retry').addEventListener('click', async () => {
    try {
        renderProgress(await api(
            `/api/deployments/${encodeURIComponent(openDeploymentId)}/retry`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }));
    } catch (e) { alert(e.message); }
});

// ---------------------------------------------------------------- boot

(async function init() {
    await loadPackages();
    renderStepPalette();
    renderDetectionKinds();
    syncInstallPanes();
    syncDetectionPanes();

    // The machine picker lists what the hub knows about, not just what has enrolled, so a
    // machine can be targeted before its agent checks in. /api/machines is itself scope
    // filtered, so this never offers a machine the operator cannot deploy to.
    try {
        const machines = await api('/api/machines');
        const options = document.getElementById('deploy-machine-options');
        options.replaceChildren();
        machines.forEach((m) => {
            const option = document.createElement('option');
            option.value = m.machine || m.name || m;
            options.appendChild(option);
        });
    } catch (e) { /* the picker still accepts free text */ }
})();
