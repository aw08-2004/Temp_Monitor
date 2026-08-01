// Firmware page: the BIOS image library, and the updates aimed at machines from it
// (roadmap #9, `update_bios`).
//
// **The states this renders are not the deploy states, and flattening them would be the
// bug.** A package deployment is succeeded/failed. A flash has `rebooting` -- the agent
// staged an image and nothing is known yet -- and `unknown`, where the machine came back on
// a version that is neither the old one nor the new one. Both are rendered as their own
// thing, because "in progress" hides the first and "succeeded" would invent the second.
//
// **`refused` is not a failure and is not styled as one.** A machine of the wrong model was
// never dispatched to; nothing went wrong, the image simply is not for it. That distinction
// is the whole reason the hub records refusals as targets instead of dropping them.
//
// Built with textContent/createElement, never innerHTML -- model strings, vendor names and
// machine-supplied error text all arrive from elsewhere -- and every string comes from the
// catalog.

(function () {
    'use strict';

    const imagesBody = document.getElementById('images-body');
    const jobsBody = document.getElementById('jobs-body');
    if (!imagesBody) return;

    const imageModal = document.getElementById('image-modal');
    const flashModal = document.getElementById('flash-modal');
    const fileInput = document.getElementById('img-file');
    const fileState = document.getElementById('img-file-state');
    const imageError = document.getElementById('image-error');
    const flashError = document.getElementById('flash-error');
    const flashRefused = document.getElementById('flash-refused');
    const flashSummary = document.getElementById('flash-summary');
    const flashChips = document.getElementById('flash-machine-list');
    const machineInput = document.getElementById('flash-machine');

    let images = [];
    let jobs = [];
    let fleetMachines = [];
    let uploaded = null;     // {sha256, file_size, file_name} once an image is stored
    let flashPayload = null; // the image the flash dialog is aimed with
    let flashTargets = [];

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    async function api(path, options) {
        const resp = await fetch(path, options);
        let payload = null;
        try { payload = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
        return payload;
    }

    function fmtTime(epoch) {
        return epoch ? new Date(epoch * 1000).toLocaleString() : '—';
    }

    function fmtSize(bytes) {
        const mb = (Number(bytes) || 0) / (1024 * 1024);
        return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round((Number(bytes) || 0) / 1024)} KB`;
    }

    // A wire value shown to an operator needs a display name, and the map is spelled out --
    // one literal t() per value, so the key scan in tests/test_i18n.py can see them.
    const TARGET_LABELS = {
        pending: () => t('firmware.target.pending'),
        in_flight: () => t('firmware.target.in_flight'),
        flashing: () => t('firmware.target.flashing'),
        rebooting: () => t('firmware.target.rebooting'),
        applied: () => t('firmware.target.applied'),
        failed: () => t('firmware.target.failed'),
        unknown: () => t('firmware.target.unknown'),
        refused: () => t('firmware.target.refused'),
        expired: () => t('firmware.target.expired'),
        cancelled: () => t('firmware.target.cancelled'),
    };
    const JOB_LABELS = {
        scheduled: () => t('firmware.job.scheduled'),
        running: () => t('firmware.job.running'),
        complete: () => t('firmware.job.complete'),
        cancelled: () => t('firmware.job.cancelled'),
    };
    // `refused` is muted rather than red on purpose: nothing went wrong, the image is not
    // for that hardware. `rebooting` is muted too -- it is genuinely still in progress.
    const TARGET_TONES = {
        pending: 'muted', in_flight: 'muted', flashing: 'warn', rebooting: 'warn',
        applied: 'ok', failed: 'danger', unknown: 'warn', refused: 'muted',
        expired: 'muted', cancelled: 'muted',
    };

    function labelFor(map, value) {
        const fn = map[value];
        // An unrecognised value shows itself: a newer hub adding a state is better rendered
        // as its own name than as a missing catalog key.
        return fn ? fn() : value;
    }

    function pill(tone, text) {
        const node = el('span', `status-pill status-pill--${tone}`);
        node.appendChild(el('span', 'status-pill__dot'));
        node.appendChild(el('span', null, text));
        return node;
    }

    // ---------------------------------------------------------------- images
    function renderImages() {
        imagesBody.replaceChildren();
        if (!images.length) {
            imagesBody.appendChild(el('p', 'stat-card__meta', t('firmware.no_images')));
            return;
        }
        const table = el('table', 'table');
        const head = el('tr');
        for (const key of ['image', 'vendor', 'models', 'installs', 'size', 'uploaded']) {
            head.appendChild(el('th', null, t(`firmware.col.${key}`)));
        }
        head.appendChild(el('th'));
        table.appendChild(el('thead')).appendChild(head);
        const tbody = el('tbody');

        for (const image of images) {
            const row = el('tr');
            row.appendChild(el('td', null, image.name));
            row.appendChild(el('td', null, image.vendor));
            row.appendChild(el('td', null, (image.models || []).join(', ')));
            row.appendChild(el('td', null, image.to_version));
            row.appendChild(el('td', null, fmtSize(image.size_bytes)));
            row.appendChild(el('td', null, fmtTime(image.created_at)));

            const actions = el('td');
            const flashBtn = el('button', 'btn btn--ghost', t('firmware.flash_button'));
            flashBtn.type = 'button';
            flashBtn.addEventListener('click', () => openFlash(image));
            actions.appendChild(flashBtn);

            const del = el('button', 'btn btn--ghost', t('common.delete'));
            del.type = 'button';
            del.addEventListener('click', () => deleteImage(image));
            actions.appendChild(del);
            row.appendChild(actions);
            tbody.appendChild(row);
        }
        table.appendChild(tbody);
        imagesBody.appendChild(table);
    }

    async function deleteImage(image) {
        if (!window.confirm(t('firmware.confirm_delete', { name: image.name }))) return;
        try {
            await api(`/api/firmware/payloads/${encodeURIComponent(image.id)}`,
                      { method: 'DELETE' });
        } catch (e) {
            window.alert(e.message);
            return;
        }
        await loadAll();
    }

    // ---------------------------------------------------------------- jobs
    function renderJobs() {
        jobsBody.replaceChildren();
        if (!jobs.length) {
            jobsBody.appendChild(el('p', 'stat-card__meta', t('firmware.no_jobs')));
            return;
        }
        for (const job of jobs) {
            const card = el('div', 'card');
            const head = el('div', 'terminal__head');
            head.appendChild(el('div', 'section-title',
                                job.payload_name || t('firmware.deleted_image')));
            head.appendChild(pill(job.status === 'complete' ? 'ok' : 'muted',
                                  labelFor(JOB_LABELS, job.status)));
            const actions = el('div', 'terminal__head-actions');
            if (job.status === 'scheduled' || job.status === 'running') {
                const cancel = el('button', 'btn btn--ghost', t('firmware.cancel_job'));
                cancel.type = 'button';
                cancel.addEventListener('click', () => cancelJob(job));
                actions.appendChild(cancel);
            }
            head.appendChild(actions);
            card.appendChild(head);

            const meta = [
                t('firmware.job_meta', {
                    version: job.payload_version || '—',
                    machines: job.target_total,
                    when: fmtTime(job.created_at),
                }),
            ];
            if (job.window_start || job.window_end) {
                meta.push(t('firmware.job_window', {
                    start: fmtTime(job.window_start), end: fmtTime(job.window_end),
                }));
            }
            card.appendChild(el('p', 'stat-card__meta', meta.join(' · ')));

            const counts = job.target_counts || {};
            const summary = el('div', 'perm-chips');
            for (const status of Object.keys(counts)) {
                summary.appendChild(pill(TARGET_TONES[status] || 'muted',
                                         `${labelFor(TARGET_LABELS, status)}: ${counts[status]}`));
            }
            card.appendChild(summary);

            const details = el('details');
            details.appendChild(el('summary', null, t('firmware.show_machines')));
            const list = el('div');
            details.appendChild(list);
            // Targets are fetched on expand rather than with the list: a fleet-wide job has
            // one row per machine, and the summary above answers the usual question.
            details.addEventListener('toggle', async () => {
                if (!details.open || list.childElementCount) return;
                list.replaceChildren(el('p', 'stat-card__meta', t('common.loading')));
                try {
                    const full = await api(`/api/firmware/jobs/${encodeURIComponent(job.id)}`);
                    renderTargets(list, full.targets || []);
                } catch (e) {
                    list.replaceChildren(el('p', 'setting__error', e.message));
                }
            });
            card.appendChild(details);
            jobsBody.appendChild(card);
        }
    }

    function renderTargets(container, targets) {
        container.replaceChildren();
        const table = el('table', 'table');
        const tbody = el('tbody');
        for (const target of targets) {
            const row = el('tr');
            row.appendChild(el('td', null, target.machine));
            const state = el('td');
            state.appendChild(pill(TARGET_TONES[target.status] || 'muted',
                                   labelFor(TARGET_LABELS, target.status)));
            row.appendChild(state);
            row.appendChild(el('td', null, target.observed_version || target.from_version || '—'));
            // The message is the point of a refused or failed row -- "this machine reports
            // model 'OptiPlex 7010', which this image does not list" is actionable, and
            // "refused" alone is not.
            row.appendChild(el('td', null, target.error || ''));

            const actions = el('td');
            if (target.status === 'pending' || target.status === 'in_flight') {
                const cancel = el('button', 'btn btn--ghost', t('firmware.cancel_target'));
                cancel.type = 'button';
                cancel.addEventListener('click', async () => {
                    try {
                        await api(`/api/firmware/updates/${encodeURIComponent(target.id)}/cancel`,
                                  { method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: '{}' });
                    } catch (e) {
                        window.alert(e.message);
                    }
                    await loadAll();
                });
                actions.appendChild(cancel);
            }
            row.appendChild(actions);
            tbody.appendChild(row);
        }
        table.appendChild(tbody);
        container.appendChild(table);
    }

    async function cancelJob(job) {
        if (!window.confirm(t('firmware.confirm_cancel_job'))) return;
        let answer;
        try {
            answer = await api(`/api/firmware/jobs/${encodeURIComponent(job.id)}/cancel`,
                               { method: 'POST',
                                 headers: { 'Content-Type': 'application/json' },
                                 body: '{}' });
        } catch (e) {
            window.alert(e.message);
            return;
        }
        // Both numbers, always. A job cancelled while machines are already flashing has
        // stopped nothing for those machines, and saying otherwise would be the console
        // lying at the worst possible moment.
        if (answer.still_flashing) {
            window.alert(t('firmware.cancel_partial', { count: answer.still_flashing }));
        }
        await loadAll();
    }

    // ---------------------------------------------------------------- image editor
    document.getElementById('new-image').addEventListener('click', () => {
        uploaded = null;
        for (const id of ['img-name', 'img-vendor', 'img-version', 'img-models',
                          'img-args', 'img-notes']) {
            document.getElementById(id).value = '';
        }
        fileInput.value = '';
        fileState.textContent = t('firmware.editor.file_hint',
                                  { mb: fileState.dataset.maxMb });
        imageError.hidden = true;
        imageModal.showModal();
    });

    document.getElementById('image-cancel').addEventListener('click',
                                                             () => imageModal.close());

    fileInput.addEventListener('change', async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        uploaded = null;
        fileState.textContent = t('firmware.editor.uploading');
        const form = new FormData();
        form.append('file', file);
        try {
            uploaded = await api('/api/firmware/upload', { method: 'POST', body: form });
        } catch (e) {
            fileState.textContent = e.message;
            return;
        }
        // The digest is shown because it is what the agent verifies before it flashes --
        // an operator comparing it against the vendor's published hash is exactly the check
        // this feature wants people to make.
        fileState.textContent = t('firmware.editor.uploaded', {
            name: uploaded.file_name,
            size: fmtSize(uploaded.file_size),
            sha256: uploaded.sha256,
        });
        const name = document.getElementById('img-name');
        if (!name.value) name.value = uploaded.file_name;
    });

    document.getElementById('image-save').addEventListener('click', async () => {
        imageError.hidden = true;
        if (!uploaded) {
            imageError.textContent = t('firmware.editor.need_file');
            imageError.hidden = false;
            return;
        }
        const models = document.getElementById('img-models').value
            .split(',').map((m) => m.trim()).filter(Boolean);
        try {
            await api('/api/firmware/payloads', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: document.getElementById('img-name').value,
                    vendor: document.getElementById('img-vendor').value,
                    to_version: document.getElementById('img-version').value,
                    models: models,
                    install_args: document.getElementById('img-args').value,
                    notes: document.getElementById('img-notes').value,
                    sha256: uploaded.sha256,
                    file_size: uploaded.file_size,
                    file_name: uploaded.file_name,
                }),
            });
        } catch (e) {
            imageError.textContent = e.message;
            imageError.hidden = false;
            return;
        }
        imageModal.close();
        await loadAll();
    });

    // ---------------------------------------------------------------- flash dialog
    function renderChips() {
        flashChips.replaceChildren();
        for (const machine of flashTargets) {
            const chip = el('span', 'chip');
            chip.appendChild(el('span', 'chip__name', machine));
            const remove = el('button', 'chip__remove', '×');
            remove.type = 'button';
            remove.addEventListener('click', () => {
                flashTargets = flashTargets.filter((m) => m !== machine);
                renderChips();
            });
            chip.appendChild(remove);
            flashChips.appendChild(chip);
        }
    }

    function openFlash(image) {
        flashPayload = image;
        flashTargets = [];
        flashError.hidden = true;
        flashRefused.replaceChildren();
        renderChips();
        machineInput.value = '';
        document.getElementById('flash-start').value = '';
        document.getElementById('flash-end').value = '';
        document.getElementById('flash-note').value = '';
        flashSummary.textContent = t('firmware.flash.summary', {
            name: image.name, vendor: image.vendor,
            models: (image.models || []).join(', '), version: image.to_version,
        });
        flashModal.showModal();
    }

    document.getElementById('flash-cancel').addEventListener('click',
                                                             () => flashModal.close());

    function localToEpoch(value) {
        if (!value) return null;
        const ms = new Date(value).getTime();
        return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
    }

    document.getElementById('flash-submit').addEventListener('click', async () => {
        flashError.hidden = true;
        flashRefused.replaceChildren();
        // Free text typed and not picked from the list still counts -- a machine can be
        // targeted before its name appears in a loaded roster.
        const typed = machineInput.value.trim();
        if (typed && !flashTargets.includes(typed)) flashTargets.push(typed);
        machineInput.value = '';
        renderChips();
        if (!flashTargets.length) {
            flashError.textContent = t('firmware.flash.need_machines');
            flashError.hidden = false;
            return;
        }
        let answer;
        try {
            answer = await api('/api/firmware/jobs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    payload_id: flashPayload.id,
                    machines: flashTargets,
                    note: document.getElementById('flash-note').value,
                    window_start: localToEpoch(document.getElementById('flash-start').value),
                    window_end: localToEpoch(document.getElementById('flash-end').value),
                }),
            });
        } catch (e) {
            flashError.textContent = e.message;
            flashError.hidden = false;
            return;
        }
        await loadAll();
        // Refusals keep the dialog OPEN and name every machine. Closing on a partial
        // result would leave the operator believing the whole set was queued, which is
        // precisely the outcome the hub records refusals to prevent.
        if ((answer.refused || []).length) {
            const box = el('div', 'notice notice--warn');
            box.appendChild(el('div', 'section-title',
                               t('firmware.flash.refused_title',
                                 { count: answer.refused.length })));
            const list = el('ul', 'plain-list');
            for (const item of answer.refused) {
                list.appendChild(el('li', null, `${item.machine}: ${item.reason}`));
            }
            box.appendChild(list);
            flashRefused.appendChild(box);
            return;
        }
        flashModal.close();
    });

    // ---------------------------------------------------------------- load
    async function loadAll() {
        try {
            const [imageResp, jobResp] = await Promise.all([
                api('/api/firmware/payloads'),
                api('/api/firmware/jobs'),
            ]);
            images = imageResp.payloads || [];
            jobs = jobResp.jobs || [];
        } catch (e) {
            imagesBody.replaceChildren(el('p', 'setting__error', e.message));
            return;
        }
        renderImages();
        renderJobs();
    }

    async function loadFleet() {
        try {
            // A bare array, and already scope-filtered -- the same source the backup
            // and permission pickers use, so this one cannot suggest a machine the
            // operator would then be refused.
            const rows = await api('/api/machines');
            fleetMachines = (rows || []).map((m) => m.machine || m.name).filter(Boolean);
        } catch (e) {
            fleetMachines = [];   // free text still works; the picker just stops suggesting
        }
        if (window.attachAutocomplete) {
            window.attachAutocomplete(machineInput, {
                minChars: 0,
                emptyText: t('firmware.flash.no_matches'),
                source: (query) => fleetMachines
                    .filter((m) => !flashTargets.includes(m))
                    .filter((m) => m.toLowerCase().includes(query.toLowerCase()))
                    .slice(0, 20)
                    .map((m) => ({ value: m, label: m })),
                onSelect: (item) => {
                    if (!flashTargets.includes(item.value)) flashTargets.push(item.value);
                    machineInput.value = '';
                    renderChips();
                },
            });
        }
    }

    loadAll();
    loadFleet();
}());
