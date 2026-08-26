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
// **Every verb is on the right-click menu, and nowhere else.** They act on rows, so they
// live on the rows: the panel has no toolbar, and the location bar above the list carries
// only what acts on the VIEW -- Up, the path, Go, Refresh. Two consequences are load-bearing.
// The menu is built from the same `renderActions()` the toolbar used, so what it offers can
// never disagree with what the selection allows; and because it is now the only way to reach
// Delete, the keyboard path to it (roving tabindex, Enter, Space, the Menu key) is a
// requirement rather than a courtesy.
//
// **"Open" means open it HERE.** A file is fetched and shown in the preview dialog with
// nothing asked first, because that is what nine opens in ten are for: somebody wants to look
// at a log. Starting a process on the far machine is its own entry one line down, and keeps
// the account dialog -- that is the half that runs code on somebody's computer.
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
    // Opening arrived later, and is gated separately on purpose: an agent on 3.33.x browses,
    // copies and downloads perfectly well, and disabling the whole panel over the one verb it
    // cannot do would take four working tools away to explain a fifth.
    const MIN_OPEN_AGENT = '3.34.0';

    // A preview is pulled into the browser's memory whole, so this is a ceiling on what the
    // console will try rather than on what it can fetch: a 300 MB log downloads fine through
    // the Save link, and rendering it into a <pre> would hang the tab.
    const PREVIEW_MAX_BYTES = 10 * 1024 * 1024;

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

    // Navigation: acts on the view, always on screen, never on the menu.
    const nav = {
        up: document.getElementById('files-up'),
        go: document.getElementById('files-go'),
        refresh: document.getElementById('files-refresh')
    };

    // The verbs. Every one of them acts on the selection, and every one of them is here.
    const menu = document.getElementById('files-menu');
    const menuTargetEl = document.getElementById('files-menu-target');
    const item = {
        open: document.getElementById('files-menu-open'),
        openRemote: document.getElementById('files-menu-open-remote'),
        download: document.getElementById('files-menu-download'),
        upload: document.getElementById('files-menu-upload'),
        copy: document.getElementById('files-menu-copy'),
        cut: document.getElementById('files-menu-cut'),
        paste: document.getElementById('files-menu-paste'),
        rename: document.getElementById('files-menu-rename'),
        newFolder: document.getElementById('files-menu-new-folder'),
        remove: document.getElementById('files-menu-delete')
    };
    const tableScroll = pane.querySelector('.table-scroll');

    const nameDialog = document.getElementById('files-name-dialog');
    const deleteDialog = document.getElementById('files-delete-dialog');
    const uploadDialog = document.getElementById('files-upload-dialog');
    const openDialog = document.getElementById('files-open-dialog');
    const previewDialog = document.getElementById('files-preview-dialog');

    // ---- state ----
    let path = null;                // null means the drive list
    let listing = null;             // the last answer from the hub
    let selection = new Set();      // entry names selected in the current folder
    let clipboard = null;           // {op, paths, label}
    let busy = false;               // one request in flight at a time, panel-wide
    let generation = 0;             // bumped on teardown/navigate to orphan stale polls
    let timers = [];                // every setTimeout this panel owns
    let pendingFile = null;         // the File chosen for upload, awaiting its dialog
    let pendingOpen = null;         // the entry the Open dialog is asking about
    let previewUrl = null;          // the object URL the preview dialog is showing, if any
    let menuRow = null;             // the <tr> the action menu is open against, if any
    let focusName = null;           // which row holds the keyboard's place in the list

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

    // What the console will render, and nothing else. Two rules decide the membership:
    // the type must be safe to show from a blob URL that carries this hub's origin (so no
    // html, htm, svg, xhtml -- all of them script documents), and showing it must beat
    // saving it. Anything absent still downloads through the Save link, which is the honest
    // answer for a .docx: this is a console, not Word.
    const PREVIEW_TEXT = new Set([
        'txt', 'log', 'ini', 'cfg', 'conf', 'csv', 'json', 'xml', 'yml', 'yaml', 'md',
        'ps1', 'bat', 'cmd', 'reg', 'inf', 'sql', 'py', 'js', 'css'
    ]);
    const PREVIEW_IMAGE = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico']);

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
    let openTooOld = null;      // the same question asked against MIN_OPEN_AGENT

    async function checkAgentVersion() {
        if (tooOld !== null) return;
        try {
            const info = await api(`/api/machines/${encodeURIComponent(currentMachine())}`);
            const version = info && info.companion_version;
            tooOld = (version && versionLess(version, MIN_FILES_AGENT)) ? version : false;
            openTooOld = (version && versionLess(version, MIN_OPEN_AGENT)) ? version : false;
        } catch (e) {
            tooOld = false;      // unknown is not "too old"; let the command answer for itself
            openTooOld = false;
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
        // Somebody has to be in the tab order or the list cannot be reached from the keyboard
        // at all -- and with no toolbar, unreachable from the keyboard means unusable.
        if (!body.querySelector('tr[tabindex="0"]')) {
            const first = body.querySelector('tr[data-name]');
            if (first) { first.tabIndex = 0; focusName = first.dataset.name; }
        }
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
        // The name is the row's identity everywhere else in this file (the selection is a set
        // of names), so it is what the menu and the keyboard look rows up by too.
        row.dataset.name = entry.name;
        // Roving tabindex: exactly one row is in the tab order at a time, and the arrows move
        // it. A list of two hundred files that put every row in the tab order would be a list
        // an operator tabs THROUGH rather than to.
        row.tabIndex = entry.name === focusName ? 0 : -1;
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

        // What a double-click has meant in every file manager the operator has ever used:
        // a folder is entered, a file is opened -- and here "opened" is the console's own
        // preview, with nothing asked first. Deliberately acts on the row under the pointer
        // rather than on the selection, because that is the row they double-clicked.
        row.addEventListener('dblclick', () => openEntry(entry));
        return row;
    }

    /**
     * What the selection currently allows, written onto the menu.
     *
     * One function for both the menu and the location bar, called on every change to the
     * selection, the clipboard or the busy flag -- so a menu that is already open when a
     * request finishes is corrected in place rather than left offering something that has
     * stopped being possible.
     */
    function renderActions() {
        const picked = selectedEntries();
        const inFolder = !!path && !!listing;
        const idle = !busy && !tooOld;

        nav.up.disabled = !idle || !path;
        nav.refresh.disabled = !idle;
        nav.go.disabled = !idle;

        // Open, here: a folder is entered, a file is fetched and shown. Enabled for both.
        item.open.disabled = !idle || picked.length !== 1;
        // Open over there. Deliberately NOT disabled on an agent too old for it -- a disabled
        // entry explains nothing and shows no tooltip either, so the click is allowed through
        // and answered with the version it is waiting for. See MIN_OPEN_AGENT.
        item.openRemote.disabled = !idle || picked.length !== 1;
        // One item, and a file: a folder download is offered too -- it arrives zipped -- so
        // the only thing ruled out here is downloading several things at once, which would
        // be several files with one Save dialog between them.
        item.download.disabled = !idle || picked.length !== 1;
        item.upload.disabled = !idle || !inFolder;
        item.copy.disabled = !idle || picked.length === 0;
        item.cut.disabled = !idle || picked.length === 0;
        item.paste.disabled = !idle || !clipboard || !inFolder;
        item.rename.disabled = !idle || picked.length !== 1;
        item.newFolder.disabled = !idle || !inFolder;
        item.remove.disabled = !idle || picked.length === 0;
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
    // open
    // ================================================================
    /**
     * Open it HERE, and ask nothing.
     *
     * The default gesture -- the menu's first entry, a double-click, Enter on a focused row --
     * because nine opens in ten are somebody wanting to LOOK at something: a log, a
     * screenshot, the error dialog a caller photographed. A folder is entered, which is what
     * opening a folder has always meant; a file comes down the ordinary transfer path and is
     * rendered in the preview dialog.
     *
     * Nothing here reaches MIN_OPEN_AGENT: this is a fetch_file, which every agent that can
     * browse at all already answers. A machine halfway through a rollout can still be looked
     * at, and only the entry below reports a version.
     */
    function openEntry(entry) {
        if (busy) return;
        if (entry.directory) { navigate(entryPath(entry)); return; }
        openInBrowser(entry);
    }

    /**
     * Open it over THERE: ask which account, then start it on the machine.
     *
     * One question, because the menu entry that got here already answered the other one. The
     * account is re-set to the signed-in user every time this opens rather than remembered --
     * "as the system account", chosen twenty minutes ago for an installer and silently
     * reapplied to somebody's spreadsheet, is the exact surprise the two accounts exist to
     * prevent.
     */
    function askOpenRemote(entry) {
        if (openTooOld) {
            // The status pill is left alone: the panel is idle and working, and only this one
            // entry is waiting for a newer agent -- everything else, opening here included,
            // still works against this machine.
            showError(t('files.open_agent_too_old', { version: openTooOld,
                                                      needed: MIN_OPEN_AGENT }));
            return;
        }
        pendingOpen = entry;
        openHelp.textContent = t('files.open_help', { name: entry.name });
        openError.hidden = true;
        openRunAs.forEach((radio) => { radio.checked = radio.value === 'user'; });
        openDialog.showModal();
    }

    /** Start it over there. Nothing is re-listed: opening changes nothing on the disk. */
    async function openOnMachine(entry, runAs) {
        if (busy) return;
        const mine = ++generation;
        busy = true;
        showError('');
        showNote('');
        setStatus('muted', t('files.working'));
        renderActions();
        try {
            const queued = await post(`${base()}/open`, {
                path: entryPath(entry), run_as: runAs
            });
            const result = await awaitCommand(queued.command_id, mine);
            if (result === null) return;
            if (!result.ok) {
                setStatus('warn', t('files.open_failed'));
                showError(result.output || t('files.open_failed'));
                return;
            }
            // The agent's own sentence when it wrote one -- which account, which session,
            // which pid. That is the whole answer to "did it actually open", and a generic
            // "done" would throw it away.
            setStatus('ok', t('files.ready'));
            showNote(result.output || t('files.opened'));
        } catch (e) {
            if (mine !== generation) return;
            setStatus('danger', t('files.open_failed'));
            showError(e.message);
        } finally {
            if (mine === generation) busy = false;
            renderActions();
        }
    }

    /** Open it here instead: the ordinary transfer, rendered in the preview dialog. */
    async function openInBrowser(entry) {
        if (entry.size !== null && entry.size !== undefined && entry.size > PREVIEW_MAX_BYTES) {
            // Refused before the transfer starts rather than after it lands: pulling 300 MB
            // across the link to then say it cannot be shown is the worst order to do this in.
            showError(t('files.preview_too_big', { name: entry.name,
                                                   size: formatSize(entry.size) }));
            return;
        }
        await pull(entry, (transferId, name, size) => showPreview(transferId, name, size));
    }

    /**
     * Render one downloaded file in the dialog, by an ALLOWLIST of types.
     *
     * The list is a security boundary, not a convenience. A blob URL inherits the origin of
     * the page that made it, so an HTML or SVG file pulled off somebody's PC and opened in a
     * tab would run its script as the signed-in operator, against this hub, with their
     * session. Text is written with textContent, images go in an <img> (which never executes
     * anything), and a PDF goes in a sandboxed iframe so its own scripting is off. Everything
     * else keeps the Save link and says so.
     */
    async function showPreview(transferId, name, size) {
        const url = `${base()}/transfers/${encodeURIComponent(transferId)}/content`;
        const extension = (name.split('.').pop() || '').toLowerCase();
        const kind = PREVIEW_TEXT.has(extension) ? 'text'
            : PREVIEW_IMAGE.has(extension) ? 'image'
            : extension === 'pdf' ? 'pdf' : null;

        previewTitle.textContent = name;
        previewBody.replaceChildren();
        previewNote.hidden = true;
        // The Save link is offered next to every preview, including the ones that render:
        // having looked at a log is usually the moment somebody wants to keep it.
        previewSave.replaceChildren(saveLink(url, name));
        releasePreviewUrl();

        if (kind === null) {
            previewNote.hidden = false;
            previewNote.textContent = t('files.preview_unsupported',
                                        { kind: extension || '?' });
            previewDialog.showModal();
            return;
        }

        const response = await fetch(url);
        if (!response.ok) throw new Error(t('files.preview_failed'));
        const blob = await response.blob();

        if (kind === 'text') {
            previewBody.appendChild(el('pre', 'files-preview__text', await blob.text()));
        } else if (kind === 'image') {
            previewUrl = URL.createObjectURL(blob);
            const image = el('img', 'files-preview__image');
            image.src = previewUrl;
            image.alt = name;
            previewBody.appendChild(image);
        } else {
            previewUrl = URL.createObjectURL(blob);
            const frame = el('iframe', 'files-preview__frame');
            frame.setAttribute('sandbox', '');
            frame.setAttribute('title', name);
            frame.src = previewUrl;
            previewBody.appendChild(frame);
        }
        previewDialog.showModal();
    }

    function releasePreviewUrl() {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null;
    }

    // ================================================================
    // download
    // ================================================================
    /**
     * Fetch one entry off the machine and hand the finished transfer to `onReady`.
     *
     * Shared by the menu's Save entry and by "open here", because they are the same three round
     * trips -- queue a fetch_file, poll the transfer, collect it from the spool -- and differ
     * only in what becomes of the bytes at the end.
     */
    async function pull(entry, onReady) {
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
            await onReady(started.transfer_id, started.name, transfer.size_bytes);
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
        const url = `${base()}/transfers/${encodeURIComponent(transferId)}/content`;
        noteEl.appendChild(saveLink(url, name));
        noteEl.appendChild(el('span', 'stat-card__meta', ` ${formatSize(size)}`));
    }

    /** The Save link itself, shared with the preview dialog's foot. */
    function saveLink(url, name) {
        const link = el('a', 'btn btn--primary', t('files.save_file', { name }));
        link.href = url;
        // The hub serves it as an attachment with an octet-stream type regardless; this is
        // for the filename the browser suggests, which is the operator-facing half.
        link.setAttribute('download', name);
        return link;
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

    // ---- the open dialog ----
    const openHelp = document.getElementById('files-open-item');
    const openError = document.getElementById('files-open-error');
    // An array rather than a live NodeList, so `.find` and `.forEach` read the same as they
    // do everywhere else in this file.
    const openRunAs = Array.from(
        document.querySelectorAll('input[name="files-open-runas"]'));

    document.getElementById('files-open-cancel').addEventListener('click', () => {
        openDialog.close();
        pendingOpen = null;
    });
    document.getElementById('files-open-ok').addEventListener('click', () => {
        const entry = pendingOpen;
        if (!entry) return;
        const runAs = openRunAs.find((radio) => radio.checked);
        openDialog.close();
        pendingOpen = null;
        openOnMachine(entry, runAs ? runAs.value : 'user');
    });

    // ---- the preview dialog ----
    const previewTitle = document.getElementById('files-preview-title');
    const previewBody = document.getElementById('files-preview-body');
    const previewNote = document.getElementById('files-preview-note');
    const previewSave = document.getElementById('files-preview-save');
    document.getElementById('files-preview-close').addEventListener('click',
                                                                    () => previewDialog.close());
    // On close, not on the button: Escape closes a <dialog> without going anywhere near the
    // click handler, and a blob URL left alive holds the whole file in memory until the tab
    // is closed.
    previewDialog.addEventListener('close', () => {
        previewBody.replaceChildren();
        previewSave.replaceChildren();
        releasePreviewUrl();
    });

    // ================================================================
    // the action menu
    // ================================================================
    /** The row for one entry name in the table as it stands now. Looked up by scanning
     *  rather than by selector, because a filename may contain quotes and brackets. */
    function rowFor(name) {
        return Array.from(body.querySelectorAll('tr[data-name]'))
            .find((row) => row.dataset.name === name) || null;
    }

    function entryFor(name) {
        return (listing && listing.entries ? listing.entries : [])
            .find((entry) => entry.name === name) || null;
    }

    /** Where a menu opened from the keyboard should appear: under the row it is about, or
     *  inside the list when it is about the folder itself. */
    function anchorFor(row) {
        const box = (row || tableScroll).getBoundingClientRect();
        return row ? { x: box.left + 16, y: box.bottom }
                   : { x: box.left + 16, y: box.top + 16 };
    }

    /**
     * Open the menu about `name` -- or about the folder itself when that is null.
     *
     * The selection rule is Explorer's, and it is the one that matters: right-clicking a row
     * that is ALREADY selected leaves the selection alone, so nine files stay nine files on
     * the way to Delete, while right-clicking outside it replaces the selection with that one
     * row. Anything else would silently drop a selection the operator had just built, or
     * silently act on rows they had stopped meaning.
     */
    function showMenu(name, point) {
        if (name === null) selection.clear();
        else if (!selection.has(name)) { selection.clear(); selection.add(name); }
        if (name !== null) focusName = name;
        // The checkboxes have to agree with what the menu is about before it is shown, and
        // this rebuilds the rows -- so any <tr> held from before this line is stale.
        renderTable();
        const row = name === null ? null : rowFor(name);
        openMenu(row, point || anchorFor(row));
    }

    function openMenu(row, at) {
        renderActions();
        menuRow = row;
        menuTargetEl.textContent = menuLabel();

        // Shown before it is measured, because a hidden element has no size to position by.
        menu.hidden = false;
        const box = menu.getBoundingClientRect();
        const pad = 8;
        // Flipped rather than clamped at the near edge: a menu that ran off the bottom of the
        // window would otherwise open with its entries shifted up under the cursor, and the
        // last of them is Delete.
        const left = at.x + box.width + pad > window.innerWidth
            ? Math.max(pad, at.x - box.width) : at.x;
        const top = at.y + box.height + pad > window.innerHeight
            ? Math.max(pad, at.y - box.height) : at.y;
        menu.style.left = Math.round(left) + 'px';
        menu.style.top = Math.round(top) + 'px';

        if (menuRow) menuRow.classList.add('files-row--targeted');
        // Focus the first entry that can actually be used, so Enter does something sensible
        // and Escape has somewhere to come back from.
        const usable = Object.keys(item).map((key) => item[key])
            .find((entry) => !entry.disabled);
        (usable || menu).focus({ preventScroll: true });
    }

    /** What the menu says it is about, at the top. "Delete" over an unnamed menu is how the
     *  wrong nine files get deleted. */
    function menuLabel() {
        const picked = selectedEntries();
        if (picked.length === 1) return picked[0].name;
        if (picked.length > 1) return tPlural('files.menu.target', picked.length);
        return path || t('files.this_pc');
    }

    function closeMenu(restoreFocus) {
        if (menu.hidden) return;
        const row = menuRow;
        menuRow = null;
        menu.hidden = true;
        if (row) {
            row.classList.remove('files-row--targeted');
            if (restoreFocus && row.isConnected) row.focus({ preventScroll: true });
        }
    }

    // Right-click anywhere in the list, rows and empty space alike -- the space below the
    // rows is still the folder, and Paste, New folder and Upload are about the folder.
    // Delegated, because the rows are rebuilt on every listing.
    tableScroll.addEventListener('contextmenu', (event) => {
        // The checkbox and the folder-name button inside a row are ordinary controls; a
        // right-click on either of them means the row, as it does everywhere else.
        const row = event.target.closest('tr[data-name]');
        closeMenu(false);
        // The Menu key fires this with no coordinates (0,0 in Chrome, -1 in others), so an
        // absent point means "anchor it to the row" rather than "open it in the corner".
        const point = (event.clientX > 0 && event.clientY > 0)
            ? { x: event.clientX, y: event.clientY } : null;
        showMenu(row ? row.dataset.name : null, point);
        event.preventDefault();
    });

    // The keyboard's half of the same thing. With no toolbar left, this IS the non-mouse path
    // to every verb in the panel, so it is built rather than assumed: the arrows move a
    // roving tabindex through the rows, Enter opens, Space opens the menu.
    body.addEventListener('keydown', (event) => {
        const row = event.target.closest ? event.target.closest('tr[data-name]') : null;
        if (!row || event.target !== row) return;

        if (event.key === 'Enter') {
            event.preventDefault();
            const entry = entryFor(row.dataset.name);
            if (entry) openEntry(entry);
            return;
        }
        if (event.key === ' ') {
            event.preventDefault();       // and not the page scrolling underneath it
            closeMenu(false);
            showMenu(row.dataset.name, null);
            return;
        }
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
        const rows = Array.from(body.querySelectorAll('tr[data-name]'));
        const next = rows[rows.indexOf(row) + (event.key === 'ArrowDown' ? 1 : -1)];
        if (!next) return;
        event.preventDefault();
        row.tabIndex = -1;
        next.tabIndex = 0;
        focusName = next.dataset.name;
        next.focus({ preventScroll: false });
    });

    // Everything that ends a menu. `pointerdown` rather than `click` so it closes on the
    // press, like every other menu on the platform, and capture so a press on a row's own
    // checkbox closes it too. Scrolling is included because the menu is positioned against
    // the viewport: the row would slide out from under it.
    document.addEventListener('pointerdown', (event) => {
        if (!menu.hidden && !menu.contains(event.target)) closeMenu(false);
    }, true);
    document.addEventListener('keydown', (event) => {
        if (!menu.hidden && event.key === 'Escape') {
            // Stopped, so the same Escape does not also close a dialog underneath.
            event.stopPropagation();
            closeMenu(true);
        }
    }, true);
    window.addEventListener('scroll', () => closeMenu(false), true);
    window.addEventListener('resize', () => closeMenu(false));
    window.addEventListener('blur', () => closeMenu(false));

    // ================================================================
    // wiring
    // ================================================================
    /** Every menu entry does the same two things first: read the selection it was opened
     *  about, and shut the menu. In that order, so the handler cannot be handed a selection
     *  something else has already changed. */
    function onMenu(element, handler) {
        element.addEventListener('click', () => {
            const picked = selectedEntries();
            closeMenu(false);
            handler(picked);
        });
    }

    onMenu(item.open, (picked) => {
        if (picked.length === 1) openEntry(picked[0]);
    });
    onMenu(item.openRemote, (picked) => {
        if (picked.length === 1) askOpenRemote(picked[0]);
    });
    onMenu(item.download, (picked) => {
        if (picked.length === 1) pull(picked[0], offerDownload);
    });

    onMenu(item.upload, () => {
        // Reset first: a picker that reopens holding last time's file would send the wrong
        // one on a second click that never touched it.
        fileInput.value = '';
        fileInput.click();
    });

    onMenu(item.copy, (picked) => {
        clipboard = { op: 'copy', paths: picked.map(entryPath) };
        renderClipboard();
        renderActions();
    });
    onMenu(item.cut, (picked) => {
        clipboard = { op: 'move', paths: picked.map(entryPath) };
        renderClipboard();
        renderActions();
    });
    onMenu(item.paste, () => {
        if (!clipboard || !path) return;
        const payload = { op: clipboard.op, paths: clipboard.paths, destination: path };
        // A cut is consumed by its paste; a copy is not. That is what the two words mean
        // everywhere else, and an operator pasting the same folder into three places should
        // not have to re-select between them.
        if (clipboard.op === 'move') { clipboard = null; renderClipboard(); }
        runOperation(payload);
    });

    onMenu(item.rename, (picked) => {
        if (picked.length !== 1) return;
        askName(t('files.rename_title'), t('files.rename_help', { name: picked[0].name }),
                picked[0].name, (value) => runOperation({
                    op: 'rename', paths: [entryPath(picked[0])], new_name: value
                }));
    });

    onMenu(item.newFolder, () => {
        if (!path) return;
        askName(t('files.new_folder_title'), t('files.new_folder_help', { path }), '',
                (value) => runOperation({ op: 'new_folder', destination: path,
                                          new_name: value }));
    });

    onMenu(item.remove, (picked) => {
        if (!picked.length) return;
        deleteText.textContent = tPlural('files.delete_text', picked.length);
        deleteList.replaceChildren();
        // The list is spelled out rather than counted. "Delete 9 items?" is a question
        // nobody can answer correctly, and this is the panel's one irreversible verb.
        picked.forEach((entry) => deleteList.appendChild(el('li', null, entryPath(entry))));
        deleteDialog.showModal();
    });

    nav.up.addEventListener('click', () => {
        if (listing && listing.parent) navigate(listing.parent);
        else navigate(null);
    });
    nav.refresh.addEventListener('click', () => navigate(path));
    nav.go.addEventListener('click', () => navigate(pathInput.value.trim() || null));
    pathInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') nav.go.click();
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
        pendingOpen = null;
        focusName = null;
        // A menu left standing over PC-3's file list while PC-4's listing arrives under it.
        closeMenu(false);
        // The dialogs are page-level, so a machine switch with one open would leave it asking
        // about a path on the PC the operator just left.
        if (openDialog.open) openDialog.close();
        if (previewDialog.open) previewDialog.close();
        releasePreviewUrl();
        tooOld = null;
        openTooOld = null;
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
