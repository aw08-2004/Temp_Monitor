// Tools page, Files tab: browsing one machine's disk, and moving things on and off it.
//
// **Nothing here is live.** Every view in this panel is the answer to a question that was
// asked once: the operator navigates, a command goes to the machine, and seconds later a
// listing comes back. There is no poll keeping the folder fresh and there deliberately is
// not one -- a folder does not change while you look at it the way a process list does, and
// a background refresh would silently move rows out from under a selection somebody is about
// to delete. What polls is the WAIT: a request in flight is asked about until it is
// answered, and then the polling stops.
//
// **The listing is always slightly old, and every action is confirmed against what the
// operator SAW.** A file deleted between the render and the click is reported as already
// gone rather than as a failure, and the panel re-lists after every operation instead of
// patching its own table -- a copy that half-succeeded is a real state, and the only honest
// way to draw it is to go and look.
//
// **Copy and Cut fill a clipboard that survives navigation, because that is the whole
// point.** You select in one folder and paste in another; a clipboard scoped to the current
// listing would be a clipboard that could only paste where it copied from. It is cleared
// when the machine changes, because a path on PC-3 means nothing on PC-4.
//
// **Downloads are two hops and the panel says so.** The machine uploads to the hub, the hub
// spools, and the browser collects -- the two ends are never awake on the same network at
// the same instant, which is the assumption this whole product exists to avoid. So a
// download shows as "asking the machine" and then becomes a link, rather than as a click
// that appears to do nothing for thirty seconds.
//
// Same two rules as the rest of the console: built with textContent/createElement, never
// innerHTML -- filenames are arbitrary text arriving from a remote machine, and this table
// renders more remote text than any other page in the product -- and every string comes from
// the catalog.

(function () {
    'use strict';

    const PANEL_ID = 'tool-files';
    const pane = document.getElementById(PANEL_ID);
    if (!pane) return;

    const t = window.t;
    // The plural keys (item_count, clipboard_*, delete_text) MUST go through tPlural: t()
    // looks a key up verbatim, and a plural entry is stored as `<key>.one` / `<key>.other`,
    // so t('files.item_count') finds nothing and renders the key name onto the page. The
    // catalog scan in tests/test_i18n.py accepts either spelling -- it treats `<key>.one`
    // as satisfying `t('<key>')` -- so nothing but reading the page catches this.
    const tPlural = window.tPlural;
    // A call, not a constant: on the Tools page the operator picks a different PC without
    // the page reloading, so every URL is built against the machine chosen now.
    const currentMachine = () => window.MachineContext.current();

    // The release that added the file explorer. An agent below it has no executor for these
    // commands and answers "unknown command type", so without this check the panel would
    // report a hard failure on every click -- which, during a fleet rollout, is every PC
    // that has not self-updated yet. Same shape as processes.js's MIN_PROCESS_AGENT.
    const MIN_FILES_AGENT = '3.33.0';

    // How long to keep asking before giving up on one request. The machine has to poll for
    // the command, do the work and report back; a sleeping laptop may take a while to even
    // hear about it, and a two-gigabyte upload takes as long as its link does.
    const LISTING_ATTEMPTS = 60;
    const TRANSFER_ATTEMPTS = 600;
    const COMMAND_ATTEMPTS = 120;
    let pollSeconds = 2;            // replaced by the hub's own cadence on first response

    const statusPill = document.getElementById('files-status');
    const statusText = document.getElementById('files-status-text');
    const pathInput = document.getElementById('files-path');
    const crumbs = document.getElementById('files-crumbs');
    const body = document.getElementById('files-body');
    const errorEl = document.getElementById('files-error');
    const noteEl = document.getElementById('files-note');
    const clipboardEl = document.getElementById('files-clipboard');
    const selectAll = document.getElementById('files-select-all');
    const fileInput = document.getElementById('files-file-input');

    const btn = {
        up: document.getElementById('files-up'),
        go: document.getElementById('files-go'),
        refresh: document.getElementById('files-refresh'),
        download: document.getElementById('files-download'),
        upload: document.getElementById('files-upload'),
        copy: document.getElementById('files-copy'),
        cut: document.getElementById('files-cut'),
        paste: document.getElementById('files-paste'),
        rename: document.getElementById('files-rename'),
        newFolder: document.getElementById('files-new-folder'),
        remove: document.getElementById('files-delete')
    };

    const nameDialog = document.getElementById('files-name-dialog');
    const deleteDialog = document.getElementById('files-delete-dialog');
    const uploadDialog = document.getElementById('files-upload-dialog');

    // ---- state ----
    let path = null;                // null means the drive list
    let listing = null;             // the last answer from the hub
    let selection = new Set();      // entry names selected in the current folder
    let clipboard = null;           // {op, paths, label}
    let busy = false;               // one request in flight at a time, panel-wide
    let generation = 0;             // bumped on teardown/navigate to orphan stale polls
    let timers = [];                // every setTimeout this panel owns
    let pendingFile = null;         // the File chosen for upload, awaiting its dialog

    // ================================================================
    // plumbing
    // ================================================================
    function sleep(seconds) {
        return new Promise((resolve) => {
            timers.push(setTimeout(resolve, seconds * 1000));
        });
    }

    function clearTimers() {
        timers.forEach(clearTimeout);
        timers = [];
    }

    async function api(url, options) {
        const resp = await fetch(url, options);
        let payload = null;
        try { payload = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
        return payload;
    }

    function post(url, bodyObj) {
        return api(url, {
            method: 'POST',
            // Load-bearing beyond convenience: the hub only accepts JSON bodies on
            // cookie-authenticated state changes, which is what stops a cross-site form POST
            // from deleting a signed-in operator's files. See fleet-api.js.
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(bodyObj || {})
        });
    }

    function base() {
        return `/api/machines/${encodeURIComponent(currentMachine())}/files`;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function setStatus(tone, text) {
        statusPill.className = `status-pill status-pill--${tone}`;
        statusText.textContent = text;
    }

    function showError(message) {
        errorEl.textContent = message || '';
        errorEl.hidden = !message;
    }

    function showNote(message) {
        noteEl.textContent = message || '';
        noteEl.hidden = !message;
    }

    function formatSize(bytes) {
        if (bytes === null || bytes === undefined) return '—';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let value = Number(bytes);
        if (!Number.isFinite(value)) return '—';
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
        // Whole bytes stay whole -- "1.0 B" reads as a rounding of something larger.
        return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${units[unit]}`;
    }

    function formatTime(epoch) {
        return epoch ? new Date(epoch * 1000).toLocaleString() : '—';
    }

    // DriveInfo.DriveType, lowercased by the agent. Spelled out with one literal lookup per
    // value rather than concatenating the kind onto a key prefix, so the catalog scan in
    // tests/test_i18n.py can see them -- a computed key is invisible to a regex, and a
    // missing one renders as its own name in the middle of the drive list.
    const DRIVE_TYPES = {
        fixed: () => t('files.drive_type.fixed'),
        removable: () => t('files.drive_type.removable'),
        network: () => t('files.drive_type.network'),
        cdrom: () => t('files.drive_type.cdrom'),
        ram: () => t('files.drive_type.ram')
    };

    /** The full path of one entry in the folder being shown. */
    function entryPath(entry) {
        if (entry.path) return entry.path;             // a drive row carries its own
        if (!path) return entry.name;
        return path.endsWith('\\') ? path + entry.name : `${path}\\${entry.name}`;
    }

    function selectedEntries() {
        if (!listing) return [];
        return (listing.entries || []).filter((e) => selection.has(e.name));
    }

    // ================================================================
    // the agent version gate
    // ================================================================
    let tooOld = null;

    async function checkAgentVersion() {
        if (tooOld !== null) return;
        try {
            const info = await api(`/api/machines/${encodeURIComponent(currentMachine())}`);
            const version = info && info.companion_version;
            tooOld = (version && versionLess(version, MIN_FILES_AGENT)) ? version : false;
        } catch (e) {
            tooOld = false;      // unknown is not "too old"; let the command answer for itself
        }
    }

    function versionLess(a, b) {
        const pa = String(a).split('.').map(Number);
        const pb = String(b).split('.').map(Number);
        for (let i = 0; i < 3; i++) {
            const x = pa[i] || 0, y = pb[i] || 0;
            if (x !== y) return x < y;
        }
        return false;
    }

    // ================================================================
    // navigation
    // ================================================================
    /**
     * Ask the machine for a folder, wait for the answer, and draw it.
     * `target` null means the drive list.
     */
    async function navigate(target) {
        if (busy) return;
        const mine = ++generation;
        busy = true;
        selection.clear();
        showError('');
        showNote('');
        setStatus('muted', t('files.asking'));
        renderActions();

        try {
            await checkAgentVersion();
            if (tooOld) {
                setStatus('warn', t('files.agent_too_old', { version: tooOld,
                                                             needed: MIN_FILES_AGENT }));
                body.replaceChildren();
                return;
            }
            const started = await post(`${base()}/list`, { path: target || '' });
            if (started.poll_interval) pollSeconds = started.poll_interval;
            const answer = await awaitListing(started.request_id, mine);
            if (answer === null) return;                 // superseded or torn down

            if (answer.status === 'failed') {
                // A refusal is an ANSWER, not a fault: "Access is denied" is what the
                // machine has to say about that folder, and it belongs in the panel rather
                // than styled as a broken request.
                setStatus('warn', t('files.refused'));
                showError(answer.error || t('files.refused'));
                body.replaceChildren();
                return;
            }
            path = answer.path === '\\' ? null : answer.path;
            listing = answer;
            render();
        } catch (e) {
            if (mine !== generation) return;
            setStatus('danger', t('files.load_failed'));
            showError(e.message);
        } finally {
            if (mine === generation) busy = false;
            renderActions();
        }
    }

    /** Poll one listing request until it is answered. Returns null if superseded. */
    async function awaitListing(requestId, mine) {
        for (let attempt = 0; attempt < LISTING_ATTEMPTS; attempt++) {
            await sleep(attempt === 0 ? 0.4 : pollSeconds);
            if (mine !== generation) return null;
            const payload = await api(
                `${base()}/list/${encodeURIComponent(requestId)}`);
            if (payload.poll_interval) pollSeconds = payload.poll_interval;
            if (payload.status !== 'pending') return payload;
        }
        throw new Error(t('files.no_answer'));
    }

    // ================================================================
    // rendering
    // ================================================================
    function render() {
        renderCrumbs();
        renderTable();
        renderActions();
        renderClipboard();
        pathInput.value = path || '';

        const count = (listing && listing.entries ? listing.entries.length : 0)
                      + (listing && listing.drives ? listing.drives.length : 0);
        setStatus('ok', tPlural('files.item_count', count));
        if (listing && listing.truncated > 0) {
            showNote(t('files.truncated', { count: listing.truncated }));
        }
    }

    function renderCrumbs() {
        crumbs.replaceChildren();
        const root = el('button', 'btn btn--ghost files-crumb', t('files.this_pc'));
        root.type = 'button';
        root.addEventListener('click', () => navigate(null));
        crumbs.appendChild(root);
        if (!path) return;

        // Rebuilt from the path string rather than remembered as a stack: the operator can
        // paste a path straight into the bar, and a remembered stack would then describe a
        // journey they never took.
        //
        // The first crumb is the ROOT and it is one crumb whatever shape it has: `C:` on a
        // local disk, `\\server\share` on a UNC path. Splitting the share out from the
        // server would offer a crumb for `\\server`, which is a machine rather than a place
        // the explorer can list.
        const isUnc = path.startsWith('\\\\');
        const parts = path.split('\\').filter(Boolean);
        const rootLabel = isUnc ? `\\\\${parts[0]}\\${parts[1]}` : parts[0];
        const rootPath = isUnc ? rootLabel : `${parts[0]}\\`;
        const rest = parts.slice(isUnc ? 2 : 1);

        appendCrumb(rootLabel, rootPath);
        let walked = rootPath.endsWith('\\') && !isUnc ? parts[0] : rootPath;
        rest.forEach((part) => {
            walked = `${walked}\\${part}`;
            appendCrumb(part, walked);
        });
    }

    function appendCrumb(label, target) {
        crumbs.appendChild(el('span', 'files-crumb__sep', '›'));
        const crumb = el('button', 'btn btn--ghost files-crumb', label);
        crumb.type = 'button';
        crumb.addEventListener('click', () => navigate(target));
        crumbs.appendChild(crumb);
    }

    function renderTable() {
        body.replaceChildren();
        if (!listing) return;

        const drives = listing.drives || [];
        const entries = listing.entries || [];
        if (!drives.length && !entries.length) {
            const row = el('tr');
            const cell = el('td', 'empty-state', t('files.empty'));
            cell.colSpan = 4;
            row.appendChild(cell);
            body.appendChild(row);
            selectAll.checked = false;
            return;
        }

        drives.forEach((drive) => body.appendChild(driveRow(drive)));
        entries.forEach((entry) => body.appendChild(entryRow(entry)));
        selectAll.checked = entries.length > 0 && selection.size === entries.length;
    }

    /** A volume in the root view. Not selectable: none of the verbs mean anything applied
     *  to a whole drive, and a checkbox that can be ticked but never used is furniture. */
    function driveRow(drive) {
        const row = el('tr');
        row.appendChild(el('td', 'files-col-pick'));

        const nameCell = el('td');
        const link = el('button', 'files-name files-name--dir', drive.path);
        link.type = 'button';
        link.addEventListener('click', () => navigate(drive.path));
        nameCell.appendChild(link);
        if (drive.label) nameCell.appendChild(el('span', 'files-badge', drive.label));
        // Unknown kinds (noRootDirectory, unknown) get no badge at all rather than a raw
        // slug: "this drive is of type noRootDirectory" is not a sentence, and a drive whose
        // kind we cannot name is still a drive the operator can click.
        const kind = DRIVE_TYPES[drive.type];
        if (kind) nameCell.appendChild(el('span', 'files-badge', kind()));
        row.appendChild(nameCell);

        // Free-of-total, because "how much room is left on D:" is the question this view is
        // actually asked. A drive that is not ready has neither, and shows a dash.
        row.appendChild(el('td', null, drive.free_bytes === null || drive.free_bytes === undefined
            ? '—'
            : t('files.free_of', { free: formatSize(drive.free_bytes),
                                   total: formatSize(drive.total_bytes) })));
        row.appendChild(el('td', null, '—'));
        return row;
    }

    function entryRow(entry) {
        const row = el('tr');
        if (entry.hidden || entry.system) row.classList.add('files-row--dim');

        const pickCell = el('td', 'files-col-pick');
        const pick = document.createElement('input');
        pick.type = 'checkbox';
        pick.checked = selection.has(entry.name);
        pick.setAttribute('aria-label', entry.name);
        pick.addEventListener('change', () => {
            if (pick.checked) selection.add(entry.name);
            else selection.delete(entry.name);
            selectAll.checked = selection.size === (listing.entries || []).length;
            renderActions();
        });
        pickCell.appendChild(pick);
        row.appendChild(pickCell);

        const nameCell = el('td');
        if (entry.directory) {
            const link = el('button', 'files-name files-name--dir', entry.name);
            link.type = 'button';
            link.addEventListener('click', () => navigate(entryPath(entry)));
            nameCell.appendChild(link);
        } else {
            nameCell.appendChild(el('span', 'files-name', entry.name));
        }
        // Said rather than implied. A junction copied is its target copied, and a read-only
        // file that will not delete is a question somebody would otherwise raise a ticket
        // about.
        if (entry.link) nameCell.appendChild(el('span', 'files-badge', t('files.badge.link')));
        if (entry.readonly) nameCell.appendChild(el('span', 'files-badge', t('files.badge.readonly')));
        if (entry.hidden) nameCell.appendChild(el('span', 'files-badge', t('files.badge.hidden')));
        row.appendChild(nameCell);

        row.appendChild(el('td', null, entry.directory ? '—' : formatSize(entry.size)));
        row.appendChild(el('td', null, formatTime(entry.modified)));
        return row;
    }

    function renderActions() {
        const picked = selectedEntries();
        const inFolder = !!path && !!listing;
        const idle = !busy && !tooOld;

        btn.up.disabled = !idle || !path;
        btn.refresh.disabled = !idle;
        btn.go.disabled = !idle;
        // One item, and a file: a folder download is offered too -- it arrives zipped -- so
        // the only thing ruled out here is downloading several things at once, which would
        // be several files with one Save dialog between them.
        btn.download.disabled = !idle || picked.length !== 1;
        btn.upload.disabled = !idle || !inFolder;
        btn.copy.disabled = !idle || picked.length === 0;
        btn.cut.disabled = !idle || picked.length === 0;
        btn.paste.disabled = !idle || !clipboard || !inFolder;
        btn.rename.disabled = !idle || picked.length !== 1;
        btn.newFolder.disabled = !idle || !inFolder;
        btn.remove.disabled = !idle || picked.length === 0;
        selectAll.disabled = !idle || !inFolder;
    }

    function renderClipboard() {
        if (!clipboard) {
            clipboardEl.hidden = true;
            return;
        }
        clipboardEl.hidden = false;
        clipboardEl.textContent = clipboard.op === 'copy'
            ? tPlural('files.clipboard_copy', clipboard.paths.length)
            : tPlural('files.clipboard_cut', clipboard.paths.length);
    }

    // ================================================================
    // operations
    // ================================================================
    /** Queue one file_operation and wait for the machine's verdict, then re-list. */
    async function runOperation(payload) {
        if (busy) return;
        const mine = ++generation;
        busy = true;
        showError('');
        showNote('');
        setStatus('muted', t('files.working'));
        renderActions();
        try {
            const queued = await post(`${base()}/operation`, payload);
            const result = await awaitCommand(queued.command_id, mine);
            if (result === null) return;
            if (!result.ok) {
                setStatus('warn', t('files.operation_failed'));
                showError(result.output || t('files.operation_failed'));
            } else if (result.output) {
                showNote(result.output);
            }
        } catch (e) {
            if (mine !== generation) return;
            setStatus('danger', t('files.operation_failed'));
            showError(e.message);
            busy = false;
            renderActions();
            return;
        }
        busy = false;
        // Always re-listed, success or failure: a half-completed multi-item operation is a
        // real state, and this panel does not guess what the disk looks like afterwards.
        const note = noteEl.hidden ? null : noteEl.textContent;
        const problem = errorEl.hidden ? null : errorEl.textContent;
        await navigate(path);
        if (note) showNote(note);
        if (problem) showError(problem);
    }

    /** Poll one command to completion. Returns {ok, output} or null if superseded. */
    async function awaitCommand(commandId, mine) {
        for (let attempt = 0; attempt < COMMAND_ATTEMPTS; attempt++) {
            await sleep(attempt === 0 ? 0.4 : pollSeconds);
            if (mine !== generation) return null;
            const payload = await api(
                `/api/fleet/commands/${encodeURIComponent(commandId)}/output?after_seq=-1`);
            if (payload.status === 'done' || payload.status === 'failed') {
                return {
                    ok: payload.status === 'done',
                    output: payload.result ? payload.result.output : null
                };
            }
            if (payload.status === 'expired') {
                return { ok: false, output: t('files.command_expired') };
            }
        }
        throw new Error(t('files.no_answer'));
    }

    // ================================================================
    // download
    // ================================================================
    async function startDownload() {
        const picked = selectedEntries();
        if (picked.length !== 1) return;
        const entry = picked[0];
        const mine = ++generation;
        busy = true;
        showError('');
        showNote('');
        setStatus('muted', t('files.fetching'));
        renderActions();

        try {
            const started = await post(`${base()}/download`, {
                path: entryPath(entry),
                kind: entry.directory ? 'folder' : 'file'
            });
            if (started.poll_interval) pollSeconds = started.poll_interval;
            const transfer = await awaitTransfer(started.transfer_id, mine);
            if (transfer === null) return;
            if (transfer.status !== 'ready') {
                setStatus('warn', t('files.download_failed'));
                showError(transfer.error || t('files.download_failed'));
                return;
            }
            setStatus('ok', t('files.ready'));
            offerDownload(started.transfer_id, started.name, transfer.size_bytes);
        } catch (e) {
            if (mine !== generation) return;
            setStatus('danger', t('files.download_failed'));
            showError(e.message);
        } finally {
            if (mine === generation) busy = false;
            renderActions();
        }
    }

    /**
     * Offer the finished download as a LINK the operator clicks, not as an automatic
     * navigation.
     *
     * Deliberate: by the time the bytes are on the hub, thirty seconds may have passed and
     * the operator may be reading something else, and a browser that suddenly starts saving
     * a file is a browser that looks compromised. The link also survives -- they can click
     * it again -- until the hub expires the transfer within the hour.
     */
    function offerDownload(transferId, name, size) {
        noteEl.hidden = false;
        noteEl.replaceChildren();
        const link = el('a', 'btn btn--primary', t('files.save_file', { name }));
        link.href = `${base()}/transfers/${encodeURIComponent(transferId)}/content`;
        // The hub serves it as an attachment with an octet-stream type regardless; this is
        // for the filename the browser suggests, which is the operator-facing half.
        link.setAttribute('download', name);
        noteEl.appendChild(link);
        noteEl.appendChild(el('span', 'stat-card__meta', ` ${formatSize(size)}`));
    }

    /** Poll one transfer until it stops being pending. Returns null if superseded. */
    async function awaitTransfer(transferId, mine) {
        for (let attempt = 0; attempt < TRANSFER_ATTEMPTS; attempt++) {
            await sleep(attempt === 0 ? 0.4 : pollSeconds);
            if (mine !== generation) return null;
            const payload = await api(
                `${base()}/transfers/${encodeURIComponent(transferId)}`);
            if (payload.poll_interval) pollSeconds = payload.poll_interval;
            if (payload.status !== 'pending') return payload;
        }
        throw new Error(t('files.no_answer'));
    }

    // ================================================================
    // upload
    // ================================================================
    async function sendUpload(file, name, overwrite) {
        const mine = ++generation;
        busy = true;
        showError('');
        showNote('');
        setStatus('muted', t('files.sending'));
        renderActions();

        try {
            // Step one: park the bytes. Multipart, and deliberately inert on the hub -- it
            // creates no command and touches no machine. See files_web.upload_file_to_spool.
            const form = new FormData();
            form.append('file', file, name);
            const spooled = await api(`${base()}/upload`, { method: 'POST', body: form });

            // Step two: aim them. JSON, which is the shape the hub's CSRF rule covers, and
            // the only one of the two that does anything.
            const queued = await post(`${base()}/push`, {
                transfer_id: spooled.transfer_id,
                destination: path,
                name,
                overwrite: !!overwrite
            });
            const result = await awaitCommand(queued.command_id, mine);
            if (result === null) return;
            if (!result.ok) {
                setStatus('warn', t('files.upload_failed'));
                showError(result.output || t('files.upload_failed'));
                busy = false;
                renderActions();
                return;
            }
            busy = false;
            await navigate(path);
            showNote(t('files.uploaded', { name }));
        } catch (e) {
            if (mine !== generation) return;
            setStatus('danger', t('files.upload_failed'));
            showError(e.message);
            busy = false;
            renderActions();
        }
    }

    // ================================================================
    // dialogs
    // ================================================================
    const nameTitle = document.getElementById('files-name-title');
    const nameHelp = document.getElementById('files-name-help');
    const nameValue = document.getElementById('files-name-value');
    const nameError = document.getElementById('files-name-error');
    let nameHandler = null;

    function askName(title, help, initial, handler) {
        nameTitle.textContent = title;
        nameHelp.textContent = help;
        nameValue.value = initial || '';
        nameError.hidden = true;
        nameHandler = handler;
        nameDialog.showModal();
        nameValue.focus();
        nameValue.select();
    }

    document.getElementById('files-name-cancel')
        .addEventListener('click', () => nameDialog.close());
    document.getElementById('files-name-ok').addEventListener('click', () => {
        const value = nameValue.value.trim();
        if (!value) {
            nameError.textContent = t('files.name_required');
            nameError.hidden = false;
            return;
        }
        nameDialog.close();
        if (nameHandler) nameHandler(value);
    });
    nameValue.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') document.getElementById('files-name-ok').click();
    });

    const deleteText = document.getElementById('files-delete-text');
    const deleteList = document.getElementById('files-delete-list');
    document.getElementById('files-delete-cancel')
        .addEventListener('click', () => deleteDialog.close());
    document.getElementById('files-delete-ok').addEventListener('click', () => {
        const paths = selectedEntries().map(entryPath);
        deleteDialog.close();
        if (paths.length) runOperation({ op: 'delete', paths });
    });

    const uploadHelp = document.getElementById('files-upload-help');
    const uploadName = document.getElementById('files-upload-name');
    const uploadOverwrite = document.getElementById('files-upload-overwrite');
    const uploadError = document.getElementById('files-upload-error');
    document.getElementById('files-upload-cancel').addEventListener('click', () => {
        uploadDialog.close();
        pendingFile = null;
    });
    document.getElementById('files-upload-ok').addEventListener('click', () => {
        const name = uploadName.value.trim();
        if (!name) {
            uploadError.textContent = t('files.name_required');
            uploadError.hidden = false;
            return;
        }
        const file = pendingFile;
        const overwrite = uploadOverwrite.checked;
        uploadDialog.close();
        pendingFile = null;
        if (file) sendUpload(file, name, overwrite);
    });

    // ================================================================
    // wiring
    // ================================================================
    btn.up.addEventListener('click', () => {
        if (listing && listing.parent) navigate(listing.parent);
        else navigate(null);
    });
    btn.refresh.addEventListener('click', () => navigate(path));
    btn.go.addEventListener('click', () => navigate(pathInput.value.trim() || null));
    pathInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') btn.go.click();
    });

    selectAll.addEventListener('change', () => {
        selection.clear();
        if (selectAll.checked) {
            (listing && listing.entries ? listing.entries : []).forEach(
                (entry) => selection.add(entry.name));
        }
        renderTable();
        renderActions();
    });

    btn.download.addEventListener('click', startDownload);

    btn.upload.addEventListener('click', () => {
        // Reset first: a picker that reopens holding last time's file would send the wrong
        // one on a second click that never touched it.
        fileInput.value = '';
        fileInput.click();
    });
    fileInput.addEventListener('change', () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        pendingFile = file;
        uploadHelp.textContent = t('files.upload_help', { path, size: formatSize(file.size) });
        uploadName.value = file.name;
        uploadOverwrite.checked = false;
        uploadError.hidden = true;
        uploadDialog.showModal();
        uploadName.focus();
    });

    btn.copy.addEventListener('click', () => {
        clipboard = { op: 'copy', paths: selectedEntries().map(entryPath) };
        renderClipboard();
        renderActions();
    });
    btn.cut.addEventListener('click', () => {
        clipboard = { op: 'move', paths: selectedEntries().map(entryPath) };
        renderClipboard();
        renderActions();
    });
    btn.paste.addEventListener('click', async () => {
        if (!clipboard || !path) return;
        const payload = { op: clipboard.op, paths: clipboard.paths, destination: path };
        // A cut is consumed by its paste; a copy is not. That is what the two words mean
        // everywhere else, and an operator pasting the same folder into three places should
        // not have to re-select between them.
        if (clipboard.op === 'move') { clipboard = null; renderClipboard(); }
        await runOperation(payload);
    });

    btn.rename.addEventListener('click', () => {
        const picked = selectedEntries();
        if (picked.length !== 1) return;
        askName(t('files.rename_title'), t('files.rename_help', { name: picked[0].name }),
                picked[0].name, (value) => runOperation({
                    op: 'rename', paths: [entryPath(picked[0])], new_name: value
                }));
    });

    btn.newFolder.addEventListener('click', () => {
        if (!path) return;
        askName(t('files.new_folder_title'), t('files.new_folder_help', { path }), '',
                (value) => runOperation({ op: 'new_folder', destination: path,
                                          new_name: value }));
    });

    btn.remove.addEventListener('click', () => {
        const picked = selectedEntries();
        if (!picked.length) return;
        deleteText.textContent = tPlural('files.delete_text', picked.length);
        deleteList.replaceChildren();
        // The list is spelled out rather than counted. "Delete 9 items?" is a question
        // nobody can answer correctly, and this is the panel's one irreversible verb.
        picked.forEach((entry) => deleteList.appendChild(el('li', null, entryPath(entry))));
        deleteDialog.showModal();
    });

    // ================================================================
    // panel lifecycle
    // ================================================================
    ToolPanels.register('files', {
        panelId: PANEL_ID,
        load: () => { reset(); navigate(null); },
        teardown: reset,
        requires: (machine) => !!machine
    });

    // The load-bearing half: `generation` orphans every poll loop in flight, so an answer
    // that arrives after the operator switched machines cannot write a listing of PC-3's
    // disk into a panel now showing PC-4 -- or, far worse, leave a selection from one
    // machine pointing at paths on another with the Delete button live.
    function reset() {
        generation++;
        clearTimers();
        busy = false;
        path = null;
        listing = null;
        selection = new Set();
        clipboard = null;
        pendingFile = null;
        tooOld = null;
        body.replaceChildren();
        crumbs.replaceChildren();
        pathInput.value = '';
        showError('');
        showNote('');
        renderClipboard();
        renderActions();
        setStatus('muted', t('common.loading'));
    }
})();
