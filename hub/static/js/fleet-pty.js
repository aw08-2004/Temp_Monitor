// Interactive terminals on the machine detail page: real Windows consoles (ConPTY) on the
// target, rendered by xterm.js, one per tab.
//
// This replaces the line-oriented terminal in fleet-terminal.js, which is kept as a fallback
// for agents too old to open a pseudoconsole. The difference is not cosmetic. The old one
// sent a whole SCRIPT and printed the text that came back, so anything a program did that
// wasn't "print a line" -- prompt without a newline, read a single key, redraw a progress
// bar, colour something -- either vanished or arrived mangled. The classic symptom was
// pressing Enter at an installer's `Read-Host` and nothing happening, because there was no
// console on the other end to press Enter AT.
//
// Here the operator's keystrokes go to a real console device verbatim, and what comes back
// is the VT byte stream that console produces. Enter is "\r". Ctrl-C is "\x03" and raises a
// real console control event. Nothing is interpreted on the way through -- see
// terminal.push_input on the hub and PtySession.Write on the agent, both of which say the
// same thing: pass the bytes through untouched.
//
// SEVERAL CONSOLES AT ONCE, and the tab strip that shows them. An operator watching a build
// in one shell and poking at services in another is ordinary work, and the hub has always
// allowed it (terminal.PTY_MAX_SESSIONS_PER_OPERATOR) -- it was only this file that could
// hold one session at a time, which is why "New console" had to END the old one to start a
// new one. Now each session gets its own tab, its own xterm and its own poll loop; "+"
// opens one and "x" ends one.
//
// THE STRIP IS THE HUB'S LIST, NOT THIS TAB'S MEMORY. Sessions live on the hub and are
// bound to the operator, not to a browser, so the strip is refreshed from
// GET /api/fleet/pty (see syncSessions): a console opened from another machine -- or from a
// second browser tab, or before a browser restart -- appears here with its scrollback
// intact, because attaching replays the hub's buffer. sessionStorage remembers only WHICH
// tab was in front.
//
// LATENCY. Neither end holds a request open (the hub is waitress with a fixed thread pool,
// so parked requests would starve it). Both sides poll fast while there is activity and back
// off hard when there isn't; the echo an operator feels is roughly this file's OUTPUT_POLL
// plus the agent's input poll, ~250-400ms in practice. Slower than a local shell, fast
// enough to type into. Only the FRONT console polls at typing speed -- see delayFor.
//
// IIFE-wrapped, classic script, `window.FleetPty` global: this codebase has no bundler.
(function () {
    'use strict';

    const stripEl = document.getElementById('terminal-tabs');
    const screensEl = document.getElementById('terminal-screens');
    if (!stripEl || !screensEl || !window.FleetApi) return;

    // A call, not a constant. On the Tools page the operator picks a different PC without
    // the page reloading, and a console opened after that has to be opened on the machine
    // chosen now. Note there is deliberately no "no machine yet" guard here: this module
    // does nothing until activate() is called, and by then one has been picked.
    const currentMachine = () => FleetApi.machine;
    const paneEl = document.getElementById('terminal-pty');
    const legacyEl = document.getElementById('terminal-legacy');
    const shellEl = document.getElementById('terminal-shell');
    const statusEl = document.getElementById('terminal-status');
    const hintEl = document.getElementById('terminal-hint');
    // The same panel fleet-terminal.js registers with ToolPanels; see its PANEL_ID.
    const panelEl = document.getElementById('tool-terminal');
    const addBtn = document.getElementById('terminal-add');
    const emptyEl = document.getElementById('terminal-empty');

    // Poll the output stream this fast while bytes are flowing, backing off when the shell
    // has been quiet -- a terminal sitting at an idle prompt shouldn't cost 7 requests a
    // second. QUIET_AFTER_MS is generous because "quiet" here includes the operator reading.
    const OUTPUT_POLL_MS = 150;
    const OUTPUT_POLL_IDLE_MS = 700;
    const OUTPUT_POLL_HIDDEN_MS = 3000;
    const QUIET_AFTER_MS = 10_000;
    // A console in a BACKGROUND tab is being watched by nobody, but it must still collect
    // its output (that build should be finished, not frozen, when you switch back) and its
    // polls are what tell the hub the operator hasn't abandoned it. So: kept alive, at a
    // rate that makes three idle background shells cost about one foreground one.
    const BACKGROUND_POLL_MS = 2500;
    const BACKGROUND_POLL_HIDDEN_MS = 10_000;
    // How often the strip is reconciled with the hub's list, which is what makes a console
    // opened elsewhere show up here (and one closed elsewhere stop pretending it is live).
    const SYNC_MS = 8000;
    // Coalesce keystrokes for a frame before posting. Fast typing produces one event per
    // character; batching turns a burst into one request without being perceptible.
    const INPUT_FLUSH_MS = 20;
    // terminal.PTY_MAX_INPUT_CHARS on the hub, which REFUSES an over-length body outright
    // rather than truncating it. Typing never comes close; a pasted script or a loaded
    // favorite does, and losing the whole thing to one oversized POST is not acceptable, so
    // input is split here instead.
    const MAX_INPUT_CHARS = 8000;
    // terminal.PTY_MAX_SESSIONS_PER_OPERATOR. Mirrored only to grey out "+" and say why
    // before the operator clicks it; the hub is still the one enforcing it.
    const MAX_CONSOLES = 4;

    /** id -> console record. Insertion order is not meaningful; tab order is by createdAt. */
    const consoles = new Map();
    let activeId = null;
    let syncTimer = null;
    let opening = false;
    // Arriving at an empty Terminal tab should land you at a prompt, so the first sync opens
    // a console. Exactly once, though: closing your last tab is a decision, and a strip that
    // instantly reopened what you just closed would be impossible to empty.
    let autoOpened = false;

    // ---------------- Which console is in front ----------------
    //
    // Per-BROWSER-TAB (sessionStorage), and only a HINT: the sessions themselves live on the
    // hub, so this decides which tab is selected on arrival, nothing more. A tab restored
    // after a browser restart, or a second browser tab on the same machine, finds every live
    // console either way.
    const rememberedKey = () => `fleethub:pty:${currentMachine()}`;

    function remember(id) {
        try {
            if (id) sessionStorage.setItem(rememberedKey(), id);
            else sessionStorage.removeItem(rememberedKey());
        } catch (e) { /* private mode: we fall back to the oldest console */ }
    }

    function remembered() {
        try { return sessionStorage.getItem(rememberedKey()); } catch (e) { return null; }
    }

    // ---------------- The strip ----------------
    function shellName(shell) {
        return shell === 'cmd' ? 'cmd' : 'PowerShell';
    }

    /** Consoles in tab order: oldest first, so opening one appends rather than reshuffling
     *  the strip under the operator's cursor. */
    function ordered() {
        return [...consoles.values()].sort((a, b) => a.createdAt - b.createdAt);
    }

    function makeTab(record) {
        const tab = document.createElement('div');
        tab.className = 'screen-tab';
        tab.dataset.session = record.id;

        const select = document.createElement('button');
        select.type = 'button';
        select.className = 'screen-tab__select';
        select.setAttribute('role', 'tab');
        select.addEventListener('click', () => activate(record.id));

        const label = document.createElement('span');
        label.className = 'screen-tab__label';
        select.appendChild(label);

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'screen-tab__close';
        close.setAttribute('aria-label', t('machine.pty.close_console'));
        close.title = t('machine.pty.close_console_title');
        close.textContent = '×';
        close.addEventListener('click', (e) => {
            e.stopPropagation();   // the strip's click would just re-select it
            closeConsole(record.id);
        });

        // Middle-click closes the tab, as it does in every browser and terminal app.
        // `auxclick` is the event that actually fires for a non-primary button (a plain
        // `click` listener never sees button 1 in Chrome or Firefox), and the mousedown
        // default has to be suppressed or the browser starts autoscroll instead.
        tab.addEventListener('mousedown', (e) => {
            if (e.button === 1) e.preventDefault();
        });
        tab.addEventListener('auxclick', (e) => {
            if (e.button !== 1) return;
            e.preventDefault();
            closeConsole(record.id);
        });

        tab.append(select, close);
        record.tabEl = tab;
        record.labelEl = label;
        record.selectEl = select;
        return tab;
    }

    /** Re-order the strip and re-label every tab. Labels are positional ("PowerShell 2"),
     *  so they have to be recomputed whenever the set changes -- closing the first of three
     *  PowerShells must not leave a "3" with no "1". */
    function renderStrip() {
        const list = ordered();
        // Number a shell's tabs only when there is more than one of it: a lone console is
        // "PowerShell", not "PowerShell 1".
        const totals = {};
        for (const record of list) {
            const name = shellName(record.shell);
            totals[name] = (totals[name] || 0) + 1;
            record.index = totals[name];
        }

        for (const record of list) {
            const name = shellName(record.shell);
            record.labelEl.textContent = totals[name] > 1
                ? t('machine.pty.tab_numbered', { name, index: record.index })
                : name;
            record.tabEl.classList.toggle('screen-tab--active', record.id === activeId);
            record.tabEl.classList.toggle('screen-tab--closed', record.status === 'closed');
            record.selectEl.setAttribute(
                'aria-selected', record.id === activeId ? 'true' : 'false');
            record.selectEl.title = record.status === 'closed'
                ? t('machine.pty.tab_ended')
                : t('machine.pty.tab_title', { name, machine: currentMachine() });
            // Append in order; appendChild on an existing child MOVES it, so this is also
            // the re-order.
            stripEl.insertBefore(record.tabEl, addBtn);
        }

        const live = list.filter((r) => r.status !== 'closed').length;
        if (addBtn) {
            addBtn.disabled = live >= MAX_CONSOLES;
            addBtn.title = addBtn.disabled
                ? t('machine.pty.max_consoles', { max: MAX_CONSOLES })
                : t('machine.terminal.add_console_title');
        }
        if (emptyEl) emptyEl.hidden = list.length > 0;
    }

    // ---------------- Console records ----------------
    /** Adopt a session (from the hub's list, or one we just opened) into the strip. The
     *  xterm behind it is NOT built here -- see build() for why that waits. */
    function adopt(info) {
        if (consoles.has(info.session_id)) return consoles.get(info.session_id);
        const record = {
            id: info.session_id,
            shell: info.shell || 'powershell',
            createdAt: (info.created_at || Math.floor(Date.now() / 1000)),
            knownAt: Date.now(),
            status: info.status || 'open',
            term: null,
            fit: null,
            screenEl: null,
            cursor: -1,
            lastChunkAt: Date.now(),
            pollTimer: null,
            inputTimer: null,
            pending: '',
            flushing: false,
        };
        consoles.set(record.id, record);
        makeTab(record);
        return record;
    }

    /** Build the xterm for a console. Deferred until the console is first SHOWN, because
     *  xterm measures its container on open and a hidden element has no size -- and because
     *  an adopted-but-never-viewed console costs nothing until then: its output is waiting
     *  in the hub's replay buffer, which the first poll (cursor -1) hands over whole. */
    function build(record) {
        if (record.term) return record.term;

        const screen = document.createElement('div');
        screen.className = 'terminal__pty-screen';
        screen.setAttribute('aria-label',
                            t('machine.pty.screen_label', { name: shellName(record.shell) }));
        screensEl.appendChild(screen);
        record.screenEl = screen;

        const term = new window.Terminal({
            cursorBlink: true,
            // The pty echoes typed characters itself, as any real console does. Echoing
            // locally as well would double every keystroke.
            fontFamily: 'Consolas, "Cascadia Mono", "SF Mono", Menlo, monospace',
            fontSize: 13,
            scrollback: 5000,
            // convertEol OFF: the Windows console emits proper CRLF and translating as well
            // would double-space everything.
            convertEol: false,
            theme: terminalTheme(),
        });
        const fit = new window.FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(screen);

        term.onData((data) => queueInput(record, data));
        term.onResize(({ cols, rows }) => postInput(record, { size: { cols, rows } }));
        term.attachCustomKeyEventHandler(handleKey);

        record.term = term;
        record.fit = fit;
        refit(record);
        return term;
    }

    // Ctrl-C must reach the shell as an interrupt -- that is the whole point of a real
    // console -- so copy/paste move to the Ctrl-SHIFT-C/V that every terminal emulator uses.
    // Returning false tells xterm we handled the key and it must not forward it, which for
    // both of these is what stops the shell being sent a stray 0x03 / 0x16.
    //
    // PASTE IS THE BROWSER'S JOB, not ours. It used to read the clipboard here and inject
    // the text itself, which DOUBLED every paste: returning false out of a custom key
    // handler stops xterm forwarding the KEY, but it does not preventDefault (see
    // `_customKeyEventHandler(e)===!1` in xterm.js -- it just returns), so the browser still
    // performed its own paste, xterm's own `paste` listener turned that into an onData event,
    // and the injected copy landed on top of it. Letting the default happen leaves exactly
    // one path -- and it is the better path anyway: it is the same one Ctrl-V, right-click
    // paste and middle-click use, it needs no clipboard-read permission, and it goes through
    // xterm's own handler, which honours bracketed-paste mode when the shell has asked for it.
    function handleKey(e) {
        if (e.type !== 'keydown') return true;
        const term = activeConsole() && activeConsole().term;
        if (e.ctrlKey && e.shiftKey && (e.key === 'C' || e.key === 'c')) {
            const selection = term ? term.getSelection() : '';
            if (selection) navigator.clipboard.writeText(selection).catch(() => {});
            return false;
        }
        if (e.ctrlKey && e.shiftKey && (e.key === 'V' || e.key === 'v')) return false;
        return true;
    }

    function terminalTheme() {
        // Follow the page's own surface colours rather than hard-coding a black box, so the
        // terminal doesn't look pasted in under the light theme.
        const styles = getComputedStyle(document.documentElement);
        const pick = (name, fallback) => (styles.getPropertyValue(name) || '').trim() || fallback;
        return {
            background: pick('--control-bg', '#0d1117'),
            foreground: pick('--text', '#d5dae2'),
            cursor: pick('--accent', '#58a6ff'),
            // Selection has to be translucent or it hides the text underneath it.
            selectionBackground: 'rgba(120, 150, 200, 0.35)',
        };
    }

    function refit(record) {
        if (!record || !record.fit) return;
        // fit() throws if the element is hidden (zero size), which it is for every console
        // that isn't in front -- not an error, just "not now".
        try { record.fit.fit(); } catch (e) { /* not visible */ }
    }

    function activeConsole() {
        return activeId ? consoles.get(activeId) || null : null;
    }

    /** Local, hub-generated text. Kept visually distinct from agent output, which is
     *  written raw -- the operator should be able to tell the console apart from the box. */
    function note(record, text) {
        if (record && record.term) record.term.write(`\x1b[2m${text}\x1b[0m`);
    }

    function setPill() {
        const record = activeConsole();
        if (!record) {
            setStatusPill(statusEl, 'muted', t('machine.pty.pill_none'));
        } else if (record.status === 'closed') {
            setStatusPill(statusEl, 'muted', t('machine.pty.pill_closed'));
        } else if (record.status === 'open') {
            setStatusPill(statusEl, 'warn', t('machine.pty.pill_connecting'));
        } else {
            setStatusPill(statusEl, 'ok', t('machine.pty.pill_connected'));
        }
    }

    // ---------------- Selecting ----------------
    function activate(id) {
        const record = consoles.get(id);
        if (!record) return;
        activeId = id;
        remember(id);

        for (const other of consoles.values()) {
            if (other.screenEl) other.screenEl.hidden = other.id !== id;
        }
        build(record);
        // The shell dropdown follows the front console (it says what you are looking at),
        // and is also what "+" reads for the NEXT one. Changing it no longer ends anything,
        // so this is a plain sync with no side effects.
        if (shellEl && record.shell) shellEl.value = record.shell;

        refit(record);
        if (record.term) {
            // A terminal written to while display:none can have stale geometry; a refresh
            // after the refit repaints what arrived while it was in the background.
            record.term.refresh(0, record.term.rows - 1);
            record.term.focus();
        }
        renderStrip();
        setPill();
        // Foreground now: poll at typing speed rather than waiting out the background delay.
        if (record.status !== 'closed') schedulePoll(record, 0);
    }

    // ---------------- Opening and closing ----------------
    async function openConsole() {
        if (opening) return;
        const live = [...consoles.values()].filter((r) => r.status !== 'closed').length;
        if (live >= MAX_CONSOLES) return;   // the strip already says why
        opening = true;
        if (addBtn) addBtn.disabled = true;
        try {
            // Size the new console from the one in front if there is one, so the shell
            // starts at the size it is about to be shown at rather than a default it has to
            // be resized away from on the first frame.
            const front = activeConsole();
            const body = await FleetApi.postJson('/api/fleet/pty', {
                machine: currentMachine(),
                shell: shellEl ? shellEl.value : 'powershell',
                cols: front && front.term ? front.term.cols : 120,
                rows: front && front.term ? front.term.rows : 30,
            });
            const record = adopt({
                session_id: body.session_id,
                shell: body.shell,
                status: 'open',
                created_at: Math.floor(Date.now() / 1000),
            });
            renderStrip();
            activate(record.id);
            note(record, t('machine.pty.opening',
                           { shell: body.shell, machine: currentMachine() }) + '\r\n');
        } catch (e) {
            const front = activeConsole();
            if (front) {
                note(front, '\r\n'
                     + t('machine.pty.open_failed_note', { error: e.message }) + '\r\n');
            } else if (emptyEl) {
                emptyEl.textContent = t('machine.pty.open_failed', { error: e.message });
            }
        } finally {
            opening = false;
            renderStrip();
        }
    }

    /** The X on a tab. Ends the shell on the machine (if it is still running) and drops the
     *  tab. No confirmation: a console is a working surface, the operator asked, and a modal
     *  in front of every close only trains people to click through it.
     *
     *  `local` drops the tab WITHOUT telling the hub -- for a session the hub has already
     *  told us is gone. */
    function closeConsole(id, { local = false } = {}) {
        const record = consoles.get(id);
        if (!record) return;
        stopPolling(record);
        // Work out the neighbour to fall back to while this tab is still in the strip;
        // afterwards there is no position left to be next to.
        const list = ordered();
        const at = list.findIndex((r) => r.id === id);
        const next = list[at + 1] || list[at - 1] || null;

        if (!local && record.status !== 'closed') {
            // keepalive so the request still goes out if this fires as the page unloads.
            fetch(`/api/fleet/pty/${encodeURIComponent(id)}/close`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
                keepalive: true,
            }).catch(() => {});
        }
        if (record.term) record.term.dispose();
        if (record.screenEl) record.screenEl.remove();
        record.tabEl.remove();
        consoles.delete(id);
        if (activeId !== id) {
            renderStrip();
        } else if (next) {
            activate(next.id);
        } else {
            activeId = null;
            setPill();
            renderStrip();
        }
        if (!consoles.size) remember(null);
    }

    /** The shell ended by itself (exit, crash, reaped, closed from another browser). The tab
     *  STAYS, showing its last screen, until the operator dismisses it -- coming back to a
     *  console that silently vanished tells you nothing about what happened to it. */
    function markClosed(record, reason) {
        if (record.status === 'closed') return;
        record.status = 'closed';
        stopPolling(record);
        if (record.term) {
            note(record, `\r\n[${reason || t('machine.pty.closed_default')}]\r\n`);
        }
        renderStrip();
        if (record.id === activeId) setPill();
    }

    // ---------------- Input ----------------
    function queueInput(record, data) {
        if (!data || record.status === 'closed') return;
        record.pending += data;
        if (record.inputTimer) return;
        record.inputTimer = setTimeout(() => flushInput(record), INPUT_FLUSH_MS);
    }

    async function flushInput(record) {
        record.inputTimer = null;
        // ORDER IS THE CONTRACT. Keystrokes must reach the shell in the order they were
        // typed, and a large paste now takes several POSTs -- so a second flush starting
        // while this one is mid-request would interleave two scripts into each other. One
        // drain runs at a time per console, and picks up whatever queued while it worked.
        if (record.flushing) return;
        record.flushing = true;
        try {
            while (record.pending && record.status !== 'closed') {
                let rest = record.pending;
                record.pending = '';
                while (rest && record.status !== 'closed') {
                    let take = Math.min(MAX_INPUT_CHARS, rest.length);
                    // Never split a surrogate pair: half of one is not a character, and the
                    // two halves would arrive at the shell as two replacement characters.
                    const lead = rest.charCodeAt(take - 1);
                    if (take < rest.length && lead >= 0xd800 && lead <= 0xdbff) take -= 1;
                    const chunk = rest.slice(0, take);
                    rest = rest.slice(take);
                    await postInput(record, { data: chunk });
                }
            }
        } finally {
            record.flushing = false;
        }
        // Typing makes the shell produce output; poll for it now rather than waiting out
        // whatever backoff the previous quiet period had set.
        schedulePoll(record, OUTPUT_POLL_MS);
    }

    async function postInput(record, body) {
        if (record.status === 'closed') return;
        try {
            await FleetApi.postJson(
                `/api/fleet/pty/${encodeURIComponent(record.id)}/input`, body);
        } catch (e) {
            // A 409 means the session ended underneath us; anything else is transient.
            note(record, '\r\n'
                 + t('machine.pty.input_failed', { error: e.message }) + '\r\n');
        }
    }

    // ---------------- Output ----------------
    function delayFor(record) {
        const hidden = document.visibilityState !== 'visible';
        if (record.id !== activeId) {
            return hidden ? BACKGROUND_POLL_HIDDEN_MS : BACKGROUND_POLL_MS;
        }
        if (hidden) return OUTPUT_POLL_HIDDEN_MS;
        return Date.now() - record.lastChunkAt > QUIET_AFTER_MS
            ? OUTPUT_POLL_IDLE_MS : OUTPUT_POLL_MS;
    }

    async function poll(record) {
        if (!consoles.has(record.id) || record.status === 'closed') return;

        let body;
        try {
            body = await FleetApi.getJson(
                `/api/fleet/pty/${encodeURIComponent(record.id)}/output` +
                `?after_seq=${encodeURIComponent(record.cursor)}`);
        } catch (e) {
            // Transient hub trouble must not kill a console the operator is working in --
            // the shell on the machine is fine, and the next poll usually succeeds. Say so
            // once and keep trying at the background rate.
            if (!record.hubWarned) {
                record.hubWarned = true;
                note(record, '\r\n'
                     + t('machine.pty.hub_lost', { error: e.message }) + '\r\n');
            }
            schedulePoll(record, BACKGROUND_POLL_MS);
            return;
        }
        if (!consoles.has(record.id)) return;   // closed while the request was in flight
        record.hubWarned = false;

        if (body.lost) {
            // Our cursor fell off the back of the hub's rolling window, so there is a hole
            // in the stream -- and a hole in a VT stream is not "some missing text", it is a
            // half-eaten escape sequence that corrupts everything after it. Reset the
            // emulator and carry on from here.
            record.term.reset();
            note(record, t('machine.pty.reconnected') + '\r\n');
        }
        if (body.replay_truncated) {
            // A re-attach whose history is older than the buffer holds. Nothing is corrupt;
            // say so at the top so the operator doesn't read a mid-command screen as the
            // start of their session.
            note(record, t('machine.pty.replay_truncated') + '\r\n');
        }
        if (body.chunks.length) {
            for (const chunk of body.chunks) record.term.write(chunk.text);
            record.cursor = body.next_seq - 1;
            record.lastChunkAt = Date.now();
        }
        if (body.status && body.status !== record.status && body.status !== 'closed') {
            record.status = body.status;
            if (record.id === activeId) setPill();
        }
        if (body.status === 'closed') {
            markClosed(record, body.close_reason);
            return;
        }

        schedulePoll(record, delayFor(record));
    }

    function schedulePoll(record, delay) {
        if (!record.term) return;   // never shown: its output waits in the hub's buffer
        if (record.pollTimer) clearTimeout(record.pollTimer);
        record.pollTimer = setTimeout(() => poll(record), delay);
    }

    function stopPolling(record) {
        if (record.pollTimer) { clearTimeout(record.pollTimer); record.pollTimer = null; }
        if (record.inputTimer) { clearTimeout(record.inputTimer); record.inputTimer = null; }
    }

    // ---------------- Syncing the strip with the hub ----------------
    //
    // The hub's list is the truth about what is open, so this both ADDS consoles we have
    // never seen (opened from another computer, another browser tab, or before this browser
    // was restarted -- attaching replays their scrollback) and RETIRES ones that are gone.
    async function syncSessions() {
        const startedAt = Date.now();
        let open;
        try {
            const body = await FleetApi.getJson(
                `/api/fleet/pty?machine=${encodeURIComponent(currentMachine())}`);
            open = body.sessions || [];
        } catch (e) {
            return false;   // leave the strip alone; the poll loops carry on regardless
        }

        const seen = new Set();
        for (const info of open) {
            seen.add(info.session_id);
            const record = consoles.get(info.session_id);
            if (record) record.shell = info.shell || record.shell;
            else adopt(info);
        }

        for (const record of [...consoles.values()]) {
            if (seen.has(record.id) || record.status === 'closed') continue;
            // Not in the hub's list. Ignore anything we learned about after this request
            // went out -- a console opened while it was in flight is legitimately missing
            // from a response that predates it, and retiring it would kill a live shell the
            // operator is already typing into.
            if (record.knownAt > startedAt) continue;
            if (record.term) markClosed(record, t('machine.pty.closed_by_you'));
            else closeConsole(record.id, { local: true });   // never shown, nothing to preserve
        }

        renderStrip();
        return true;
    }

    function scheduleSync(delay) {
        if (syncTimer) clearTimeout(syncTimer);
        syncTimer = setTimeout(runSync, delay);
    }

    async function runSync() {
        await syncSessions();
        if (!consoles.size && !opening && !autoOpened && panelEl && !panelEl.hidden) {
            autoOpened = true;
            await openConsole();
        } else if (!activeId && consoles.size) {
            const wanted = remembered();
            activate(consoles.has(wanted) ? wanted : ordered()[0].id);
        }
        scheduleSync(document.visibilityState === 'visible' ? SYNC_MS : SYNC_MS * 4);
    }

    // ---------------- Multi-line input ----------------
    //
    // A console takes one line at a time: a CR submits the line before it. So any block of
    // text with newlines in it -- a pasted snippet, a saved script -- necessarily RUNS as it
    // arrives, line by line, with the shell's own continuation prompt holding open blocks
    // together (`foreach (...) {` waits at `>>` for its closing brace, and PowerShell runs
    // the whole block when it gets one). That is how every terminal emulator behaves and
    // there is no way to make a real console behave otherwise.
    //
    // A clipboard paste is xterm's own business (see handleKey) and arrives through onData
    // like any other input. What is left here is the FAVORITE path, which has no browser
    // paste behind it and has to do the same line rewriting itself.

    /** Normalise CRLF/CR to \n so line counting and the CR rewrite below have one shape. */
    function normalizeEol(text) {
        return String(text == null ? '' : text).replace(/\r\n?/g, '\n');
    }

    // ---------------- Favorites ----------------
    // A favorite is TYPED INTO the front console rather than run: it may have come from a
    // teammate and is about to run as SYSTEM, so the operator reads it and presses Enter.
    // Same rule as the old terminal, for the same reason.
    //
    // MULTI-LINE FAVORITES used to be flattened -- every newline became a space -- which
    // silently corrupted every favorite longer than one statement. `foreach ($x in $y) {`
    // and its body joined with spaces is a different script, and usually not a valid one, so
    // the operator got a syntax error from something that ran fine in the old terminal.
    // They now go in as lines, with the trailing newline held back so the last one still
    // waits at the prompt.
    //
    // No confirmation for this: picking a favorite by name off a list that previews what it
    // contains IS the deliberate act, and a modal in front of it just trains people to click
    // through. The console note afterwards says what went in and what is still waiting.
    function usePick(favorite) {
        const record = activeConsole();
        if (favorite.command_type !== 'run_script') {
            if (record) {
                note(record, '\r\n' + t('machine.pty.favorite_wrong_kind',
                                        { type: favorite.command_type }) + '\r\n');
            }
            return;
        }
        if (!record || record.status === 'closed') {
            if (record) note(record, '\r\n' + t('machine.pty.console_ended') + '\r\n');
            return;
        }
        // Trailing whitespace off, so a script saved with a final newline doesn't submit its
        // own last line and take the review step with it.
        const script = normalizeEol(favorite.params && favorite.params.script).replace(/\s+$/, '');
        if (!script) {
            note(record, '\r\n'
                 + t('machine.pty.favorite_no_script', { name: favorite.name }) + '\r\n');
            return;
        }

        const lines = script.split('\n').length;

        // A cmd one-liner typed at PowerShell fails loudly, which is fine; silently
        // switching consoles under the operator would not be. Report the mismatch.
        const savedFor = favorite.params && favorite.params.shell;
        if (savedFor && savedFor !== record.shell) {
            note(record, '\r\n' + t('machine.pty.favorite_wrong_shell',
                                    { saved: savedFor, actual: record.shell }) + '\r\n');
        }

        queueInput(record, script.replace(/\n/g, '\r'));
        record.term.focus();
        note(record, '\r\n' + (lines > 1
            ? t('machine.pty.favorite_pasted',
                { name: favorite.name, lines, ran: lines - 1 })
            : t('machine.pty.favorite_loaded', { name: favorite.name })) + '\r\n');
    }

    /** "Save as favorite" in a pty console. There is no input box to read a script out of
     *  here, so the terminal SELECTION is the seed -- select the command you just got right
     *  and save it -- and the dialog's textarea is where it gets edited, newlines and all. */
    function saveFavorite() {
        const record = activeConsole();
        const selection = record && record.term ? record.term.getSelection() : '';
        FleetFavorites.openSave({
            type: 'run_script',
            params: {
                script: normalizeEol(selection).replace(/\s+$/, ''),
                shell: record ? record.shell : (shellEl ? shellEl.value : 'powershell'),
            },
        });
    }

    // Clear the hub's replay buffer too, not just the local view -- otherwise the
    // scrollback you just cleared comes back the next time you navigate away and return,
    // which reads as the button not having worked.
    function clearActive() {
        const record = activeConsole();
        if (!record) return;
        if (record.term) record.term.clear();
        FleetApi.postJson(`/api/fleet/pty/${encodeURIComponent(record.id)}/clear`, {})
            .catch(() => {});
    }

    // ---------------- Public surface ----------------
    // fleet-terminal.js owns the one agent-version lookup and calls activate() when the
    // machine can do a pseudoconsole. Keeping the decision in one place stops the two
    // terminals from both deciding they are in charge -- and it is also why the three
    // SHARED toolbar buttons (Clear, Favorites, Save-as-favorite) are bound over there and
    // delegate to the three handlers below rather than being rebound here.
    //
    // They used to be rebound here, by cloning the nodes to drop fleet-terminal.js's
    // listeners. That worked exactly once. Now that the machine can change under a live
    // page, activate() can run again -- against a PC whose agent is too old for a
    // pseudoconsole, the clones would still be in place and BOTH sets of handlers gone.
    let wired = false;

    window.FleetPty = {
        activate() {
            if (paneEl) paneEl.hidden = false;
            if (legacyEl) legacyEl.hidden = true;
            for (const id of ['terminal-timeout', 'terminal-timeout-label',
                              'terminal-stop', 'terminal-reset']) {
                const el = document.getElementById(id);
                if (el) el.hidden = true;
            }

            if (hintEl) {
                hintEl.className = 'terminal__hint';
                hintEl.textContent = t('machine.pty.hint', { max: MAX_CONSOLES });
            }

            const clearBtn = document.getElementById('terminal-clear');
            if (clearBtn) clearBtn.title = t('machine.pty.clear_title');
            const saveFavBtn = document.getElementById('terminal-save-fav');
            if (saveFavBtn) saveFavBtn.title = t('machine.pty.save_fav_title');
            // The shell dropdown picks what "+" opens next, and follows whichever console is
            // in front. It deliberately does NOT restart anything: a running powershell
            // cannot become cmd, and ending the session an operator is working in because
            // they touched a dropdown was never a good trade.
            if (shellEl) shellEl.title = t('machine.pty.shell_title');

            // Everything below binds a listener, so it happens once per document however
            // many times a machine is picked.
            if (!wired) {
                wired = true;
                if (addBtn) addBtn.addEventListener('click', openConsole);
                window.addEventListener('resize', () => refit(activeConsole()));

                // The panel starts hidden and xterm cannot measure a hidden element, so
                // consoles are only built and connected once the tab is actually shown.
                if (panelEl) {
                    panelEl.addEventListener('tab:shown', () => {
                        refit(activeConsole());
                        scheduleSync(0);
                        const record = activeConsole();
                        if (record && record.term) record.term.focus();
                    });
                }
                document.addEventListener('visibilitychange', () => {
                    if (document.visibilityState !== 'visible') return;
                    scheduleSync(0);
                    for (const record of consoles.values()) {
                        if (record.status !== 'closed') schedulePoll(record, 0);
                    }
                });
                // Leaving the page deliberately does NOT end any session -- that is the
                // whole point of persistence, and it is why there is no close-on-unload
                // here. The shells keep running (and keep printing into the hub's replay
                // buffers) so that coming back re-attaches to them. Abandonment is bounded
                // on the hub instead, by a clock that only the console's own polls refresh;
                // see PTY_ABANDONED_SECONDS.
                window.addEventListener('pagehide', () => {
                    if (syncTimer) clearTimeout(syncTimer);
                    for (const record of consoles.values()) stopPolling(record);
                });
            }

            if (panelEl && !panelEl.hidden) scheduleSync(0);
        },

        /**
         * Let go of this machine's consoles, WITHOUT closing them.
         *
         * That distinction is the whole feature: the sessions live on the hub, so dropping
         * the local xterms and re-selecting the machine later re-attaches to the same
         * shells with their scrollback intact (syncSessions replays it). Closing them here
         * would end a running build because somebody clicked a different row.
         *
         * What must not survive is anything that keeps TICKING: a poll loop left running
         * would write the previous PC's output into a strip now showing another one.
         */
        deactivate() {
            if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
            for (const record of consoles.values()) {
                stopPolling(record);
                if (record.term) record.term.dispose();
                if (record.screenEl) record.screenEl.remove();
                if (record.tabEl) record.tabEl.remove();
            }
            consoles.clear();
            activeId = null;
            opening = false;
            // Re-armed deliberately: arriving at the next machine's empty Terminal tab
            // should land you at a prompt, exactly as arriving at this one did.
            autoOpened = false;
            setPill();
            renderStrip();
            if (paneEl) paneEl.hidden = true;
            if (legacyEl) legacyEl.hidden = false;
        },

        /** The shared toolbar's three buttons, driven by fleet-terminal.js. */
        clearActive,
        openFavorites() { FleetFavorites.open({ onPick: usePick }); },
        saveFavorite,
    };
})();
