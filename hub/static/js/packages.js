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
let uploadedSource = null;      // {sha256, file_name, file_size} from the upload endpoint
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
    payloadCell.appendChild(el('div', null, sourceSummary(pkg.source)));
    if (pkg.source && pkg.source.sha256) {
        payloadCell.appendChild(el('div', 'pkg-hash', pkg.source.sha256.slice(0, 16) + '…'));
    }
    if (pkg.source && pkg.source.file_size) {
        payloadCell.appendChild(el('div', 'stat-card__meta', fmtBytes(pkg.source.file_size)));
    }
    tr.appendChild(payloadCell);

    const cmdCell = el('td');
    cmdCell.appendChild(el('div', 'pkg-hash',
        `${pkg.install_command || 'winget'} ${pkg.install_args || ''}`.trim()));
    cmdCell.appendChild(el('div', 'stat-card__meta',
        t('packages.exit_summary', { codes: pkg.success_exit_codes.join(', '),
                                     timeout: pkg.timeout_seconds })));
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

function selectedSourceKind() {
    const checked = document.querySelector('input[name="source-kind"]:checked');
    return checked ? checked.value : 'upload';
}

// Spelled out per kind rather than built from `'packages.source.' + kind`: a computed
// key is invisible to the literal-key scan in tests/test_i18n.py, and a source kind added
// server-side without catalog entries would then label its own radio button with a key.
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

const REF_PLACEHOLDERS = {
    winget: '7zip.7zip',
    url: 'https://example.com/installer.msi',
    unc: '\\\\fileserver\\software\\installer.msi',
};

function renderSourceKinds() {
    const host = document.getElementById('source-kinds');
    host.replaceChildren();
    vocab.source_kinds.forEach((kind) => {
        const [label, help] = sourceText(kind);
        const wrap = el('label', 'perm-capability');
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'source-kind';
        radio.value = kind;
        radio.addEventListener('change', syncSourcePanes);
        wrap.appendChild(radio);
        const text = el('span');
        text.appendChild(el('span', 'perm-capability__label', label));
        text.appendChild(el('span', 'perm-capability__help', help));
        wrap.appendChild(text);
        host.appendChild(wrap);
    });
}

function syncSourcePanes() {
    const kind = selectedSourceKind();
    document.getElementById('source-upload').hidden = kind !== 'upload';
    document.getElementById('source-ref').hidden = kind === 'upload';
    document.getElementById('pkg-ref').placeholder = REF_PLACEHOLDERS[kind] || '';
    document.getElementById('pkg-ref-help').textContent = sourceText(kind)[1];
    // winget has its own trust chain and its own command line, so both the hash pin and
    // the command field are meaningless there — say so rather than accepting input the
    // server will reject.
    document.getElementById('pkg-ref-sha').disabled = kind === 'winget';
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
    uploadedSource = null;
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
    document.getElementById('pkg-file').value = '';

    const source = (pkg && pkg.source) || { kind: 'upload' };
    const radio = document.querySelector(`input[name="source-kind"][value="${source.kind}"]`);
    if (radio) radio.checked = true;
    document.getElementById('pkg-ref').value = source.ref || '';
    document.getElementById('pkg-ref-sha').value =
        source.kind === 'upload' ? '' : (source.sha256 || '');
    document.getElementById('pkg-file-state').textContent = source.file_name
        ? t('packages.editor.current_payload',
            { file: source.file_name, size: fmtBytes(source.file_size) })
        : t('packages.editor.upload_help');
    syncSourcePanes();

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

async function uploadIfNeeded() {
    const input = document.getElementById('pkg-file');
    if (selectedSourceKind() !== 'upload' || !input.files.length) return null;
    const form = new FormData();
    form.append('file', input.files[0]);
    document.getElementById('pkg-file-state').textContent = t('packages.editor.uploading');
    const result = await api('/api/packages/upload', { method: 'POST', body: form });
    document.getElementById('pkg-file-state').textContent =
        t('packages.editor.uploaded', { file: result.file_name,
                                        size: fmtBytes(result.file_size),
                                        sha256: result.sha256.slice(0, 16) });
    return result;
}

function collectSource(existing) {
    const kind = selectedSourceKind();
    if (kind === 'upload') {
        // A freshly uploaded blob wins; otherwise keep whatever the package already
        // points at, so editing the command line doesn't require re-uploading 200 MB.
        const blob = uploadedSource || (existing && existing.kind === 'upload' ? existing : null);
        if (!blob) return null;
        return { kind, sha256: blob.sha256, file_name: blob.file_name, file_size: blob.file_size };
    }
    const source = { kind, ref: document.getElementById('pkg-ref').value };
    const sha = document.getElementById('pkg-ref-sha').value.trim();
    if (sha && kind !== 'winget') source.sha256 = sha;
    return source;
}

document.getElementById('package-save').addEventListener('click', async () => {
    packageError.textContent = '';
    const saveBtn = document.getElementById('package-save');
    saveBtn.disabled = true;
    try {
        uploadedSource = (await uploadIfNeeded()) || uploadedSource;

        let existingSource = null;
        if (editingPackageId) {
            const current = await api(`/api/packages/${encodeURIComponent(editingPackageId)}`);
            existingSource = current.source;
        }
        const source = collectSource(existingSource);
        if (!source) throw new Error(t('packages.editor.choose_file'));

        const payload = {
            name: document.getElementById('pkg-name').value,
            version: document.getElementById('pkg-version').value,
            description: document.getElementById('pkg-description').value,
            source,
            install_command: document.getElementById('pkg-command').disabled
                ? '' : document.getElementById('pkg-command').value,
            install_args: document.getElementById('pkg-args').value,
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
        renderProgress(await api(
            `/api/deployments/${encodeURIComponent(openDeploymentId)}/cancel`, { method: 'POST' }));
    } catch (e) { alert(e.message); }
});

document.getElementById('progress-retry').addEventListener('click', async () => {
    try {
        renderProgress(await api(
            `/api/deployments/${encodeURIComponent(openDeploymentId)}/retry`, { method: 'POST' }));
    } catch (e) { alert(e.message); }
});

// ---------------------------------------------------------------- boot

(async function init() {
    await loadPackages();
    renderSourceKinds();
    renderDetectionKinds();
    syncSourcePanes();
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
