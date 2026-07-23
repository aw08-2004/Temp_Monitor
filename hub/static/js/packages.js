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
    if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);
    return body;
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
    if (!source) return 'No payload';
    if (source.kind === 'winget') return `winget: ${source.ref}`;
    if (source.kind === 'upload') return source.file_name || 'Uploaded file';
    return source.ref || source.kind;
}

function renderPackages(list) {
    packagesPane.replaceChildren();
    if (!list.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, 'No packages defined yet.'));
        empty.appendChild(el('p', 'stat-card__meta',
            'A package is an installer plus how to run it silently and how to tell it worked.'));
        packagesPane.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    ['Package', 'Payload', 'Command', 'Detection', ''].forEach((label) => {
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
    if (!rule || rule.kind === 'none') return 'Exit code only';
    if (rule.kind === 'file_exists') return rule.path;
    if (rule.kind === 'registry_value') {
        const base = `${rule.root}\\${rule.key}\\${rule.name}`;
        return rule.equals === undefined ? base : `${base} = ${rule.equals}`;
    }
    if (rule.kind === 'installed_version') {
        return rule.min_version ? `${rule.name} >= ${rule.min_version}` : rule.name;
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
        `exit ${pkg.success_exit_codes.join(', ')} · ${pkg.timeout_seconds}s`));
    tr.appendChild(cmdCell);

    tr.appendChild(el('td', 'pkg-target-error', detectionSummary(pkg.detection)));

    const actions = el('td');
    const deployBtn = el('button', 'btn btn--primary', 'Deploy');
    deployBtn.type = 'button';
    deployBtn.addEventListener('click', () => openDeploy(pkg));
    actions.appendChild(deployBtn);

    const editBtn = el('button', 'btn', 'Edit');
    editBtn.type = 'button';
    editBtn.style.marginLeft = 'var(--space-2)';
    editBtn.addEventListener('click', () => openPackage(pkg));
    actions.appendChild(editBtn);

    const delBtn = el('button', 'btn', 'Delete');
    delBtn.type = 'button';
    delBtn.style.marginLeft = 'var(--space-2)';
    delBtn.addEventListener('click', async () => {
        if (!confirm(`Delete the package "${pkg.name}"? Deployment history is kept.`)) return;
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

const SOURCE_LABELS = {
    upload: ['Upload a file to the hub', 'Stored here, hash-pinned, served to agents over the authenticated channel.'],
    winget: ['winget package id', 'winget resolves and verifies its own payload.'],
    url: ['Download from a URL', 'The agent fetches it directly. Pin a hash if you can.'],
    unc: ['Copy from a UNC path', 'The agent reads it from a share it can already reach.'],
};

const REF_PLACEHOLDERS = {
    winget: '7zip.7zip',
    url: 'https://example.com/installer.msi',
    unc: '\\\\fileserver\\software\\installer.msi',
};

function renderSourceKinds() {
    const host = document.getElementById('source-kinds');
    host.replaceChildren();
    vocab.source_kinds.forEach((kind) => {
        const [label, help] = SOURCE_LABELS[kind] || [kind, ''];
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
    document.getElementById('pkg-ref-help').textContent =
        (SOURCE_LABELS[kind] || ['', ''])[1];
    // winget has its own trust chain and its own command line, so both the hash pin and
    // the command field are meaningless there — say so rather than accepting input the
    // server will reject.
    document.getElementById('pkg-ref-sha').disabled = kind === 'winget';
    const command = document.getElementById('pkg-command');
    command.disabled = kind === 'winget';
    command.placeholder = kind === 'winget' ? 'winget (built by the agent)' : 'msiexec.exe';
    document.getElementById('pkg-cmd-help').textContent = kind === 'winget'
        ? 'The agent builds the winget command line. Anything here is appended as extra switches.'
        : `Use ${vocab.file_placeholder || '{file}'} where the downloaded payload goes — it must appear in the command or the arguments.`;
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
    document.getElementById('package-modal-title').textContent =
        pkg ? `Edit ${pkg.name}` : 'New package';

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
        ? `Current payload: ${source.file_name} (${fmtBytes(source.file_size)}). Choose a file to replace it.`
        : 'The hub stores the file and pins its SHA-256. Agents verify that hash before running anything.';
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
    document.getElementById('pkg-file-state').textContent = 'Uploading…';
    const result = await api('/api/packages/upload', { method: 'POST', body: form });
    document.getElementById('pkg-file-state').textContent =
        `Uploaded ${result.file_name} (${fmtBytes(result.file_size)}), sha256 ${result.sha256.slice(0, 16)}…`;
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
        if (!source) throw new Error('Choose a file to upload.');

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
    document.getElementById('deploy-modal-title').textContent = `Deploy ${pkg.name}`;
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
        if (n) tally.appendChild(el('span', null, `${status.replace('_', ' ')}: ${n}`));
    });
    return tally;
}

function renderDeployments(list) {
    deploymentsPane.replaceChildren();
    if (!list.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, 'Nothing has been deployed yet.'));
        empty.appendChild(el('p', 'stat-card__meta',
            'Deploy a package from the Packages tab and its progress shows up here.'));
        deploymentsPane.appendChild(empty);
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    ['Package', 'Scheduled', 'By', 'Status', 'Progress', ''].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    list.forEach((dep) => {
        const tr = el('tr');
        const nameCell = el('td');
        // A deployment outlives the package definition on purpose, so this can be null.
        nameCell.appendChild(el('div', null, dep.package_name || '(deleted package)'));
        if (dep.note) nameCell.appendChild(el('div', 'stat-card__meta', dep.note));
        tr.appendChild(nameCell);

        const whenCell = el('td');
        whenCell.appendChild(el('div', null, fmtTime(dep.created_at)));
        if (dep.window_start) {
            whenCell.appendChild(el('div', 'stat-card__meta', `starts ${fmtTime(dep.window_start)}`));
        }
        if (dep.window_end) {
            whenCell.appendChild(el('div', 'stat-card__meta', `gives up ${fmtTime(dep.window_end)}`));
        }
        tr.appendChild(whenCell);

        tr.appendChild(el('td', 'stat-card__meta', dep.created_by));
        tr.appendChild(el('td', null, dep.status));

        const progressCell = el('td');
        progressCell.appendChild(renderProgressBar(dep.target_counts, dep.target_total));
        progressCell.appendChild(renderTally(dep.target_counts));
        tr.appendChild(progressCell);

        const actions = el('td');
        const view = el('button', 'btn', 'View');
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
        `${deployment.package_name || '(deleted package)'} — ${deployment.status}`;
    progressBody.replaceChildren();
    progressBody.appendChild(renderProgressBar(deployment.target_counts, deployment.target_total));
    progressBody.appendChild(renderTally(deployment.target_counts));

    const table = el('table', 'data-table');
    table.style.marginTop = 'var(--space-4)';
    const head = el('thead');
    const headRow = el('tr');
    ['Machine', 'Status', 'Attempts', 'Detail'].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = el('tbody');
    deployment.targets.forEach((target) => {
        const tr = el('tr');
        tr.appendChild(el('td', null, target.machine));
        tr.appendChild(el('td', null, target.status.replace('_', ' ')));
        tr.appendChild(el('td', null, String(target.attempts)));

        const detail = el('td');
        if (target.last_error) {
            detail.appendChild(el('div', 'pkg-target-error', target.last_error));
        } else if (target.next_attempt_at) {
            detail.appendChild(el('div', 'stat-card__meta',
                `retries ${fmtTime(target.next_attempt_at)}`));
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
    progressBody.replaceChildren(el('p', 'stat-card__meta', 'Loading…'));
    progressModal.showModal();
    await refreshProgress();
}

document.getElementById('progress-close').addEventListener('click', () => {
    clearTimeout(pollTimer);
    progressModal.close();
    loadDeployments();
});

document.getElementById('progress-cancel-deploy').addEventListener('click', async () => {
    if (!confirm('Stop this deployment? Machines already running the installer will finish.')) return;
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
