// Interactive terminal on the machine detail page: a real Windows console (ConPTY) on the
// target, rendered by xterm.js.
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
// LATENCY. Neither end holds a request open (the hub is waitress with a fixed thread pool,
// so parked requests would starve it). Both sides poll fast while there is activity and back
// off hard when there isn't; the echo an operator feels is roughly this file's OUTPUT_POLL
// plus the agent's input poll, ~250-400ms in practice. Slower than a local shell, fast
// enough to type into.
//
// IIFE-wrapped, classic script, `window.FleetPty` global: this codebase has no bundler.
(function () {
    'use strict';

    const screenEl = document.getElementById('terminal-pty-screen');
    if (!screenEl || !window.FleetApi || !FleetApi.machine) return;

    const MACHINE = FleetApi.machine;
    const paneEl = document.getElementById('terminal-pty');
    const legacyEl = document.getElementById('terminal-legacy');
    const shellEl = document.getElementById('terminal-shell');
    const statusEl = document.getElementById('terminal-status');
    const hintEl = document.getElementById('terminal-hint');
    const panelEl = document.getElementById('tab-terminal');
    const reconnectBtn = document.getElementById('terminal-reconnect');
    const newBtn = document.getElementById('terminal-new');

    // Poll the output stream this fast while bytes are flowing, backing off when the shell
    // has been quiet -- a terminal sitting at an idle prompt shouldn't cost 7 requests a
    // second. QUIET_AFTER_MS is generous because "quiet" here includes the operator reading.
    const OUTPUT_POLL_MS = 150;
    const OUTPUT_POLL_IDLE_MS = 700;
    const OUTPUT_POLL_HIDDEN_MS = 3000;
    const QUIET_AFTER_MS = 10_000;
    // Coalesce keystrokes for a frame before posting. Fast typing produces one event per
    // character; batching turns a burst into one request without being perceptible.
    const INPUT_FLUSH_MS = 20;
    // terminal.PTY_MAX_INPUT_CHARS on the hub, which REFUSES an over-length body outright
    // rather than truncating it. Typing never comes close; a pasted script or a loaded
    // favorite does, and losing the whole thing to one oversized POST is not acceptable, so
    // input is split here instead.
    const MAX_INPUT_CHARS = 8000;

    let term = null;
    let fit = null;
    let session = null;       // { id, cursor, lastChunkAt }
    let pollTimer = null;
    let inputTimer = null;
    let pending = '';
    let flushing = false;
    let connecting = false;

    // ---------------- Terminal ----------------
    function buildTerminal() {
        if (term) return term;
        term = new window.Terminal({
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
        fit = new window.FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(screenEl);
        refit();

        term.onData(queueInput);
        term.onResize(({ cols, rows }) => postInput({ size: { cols, rows } }));
        term.attachCustomKeyEventHandler(handleKey);

        window.addEventListener('resize', refit);
        return term;
    }

    // Ctrl-C must reach the shell as an interrupt -- that is the whole point of a real
    // console -- so copy/paste move to the Ctrl-SHIFT-C/V that every terminal emulator uses.
    // Returning false tells xterm we handled the key and it must not forward it.
    function handleKey(e) {
        if (e.type !== 'keydown') return true;
        if (e.ctrlKey && e.shiftKey && (e.key === 'C' || e.key === 'c')) {
            const selection = term.getSelection();
            if (selection) navigator.clipboard.writeText(selection).catch(() => {});
            return false;
        }
        if (e.ctrlKey && e.shiftKey && (e.key === 'V' || e.key === 'v')) {
            navigator.clipboard.readText().then(paste).catch(() => {});
            return false;
        }
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

    function refit() {
        if (!fit) return;
        // fit() throws if the element is hidden (zero size), which it is until the tab is
        // shown -- not an error, just "not yet".
        try { fit.fit(); } catch (e) { /* not visible yet */ }
    }

    // ---------------- Session ----------------
    //
    // A terminal SURVIVES leaving the page. Navigating to Packages and back re-attaches to
    // the same shell -- same working directory, same variables, same scrollback, plus
    // whatever the download you left running printed while you were gone. Only "New
    // console", the shell dropdown, or the shell itself exiting ends a session.
    //
    // The remembered id is per-BROWSER-TAB (sessionStorage), which matches how an operator
    // thinks about it: this tab's terminal. It is only a hint, though -- the hub is asked
    // what is actually still open, so a tab restored after a browser restart, or a second
    // tab on the same machine, still finds the live session rather than silently opening a
    // duplicate SYSTEM shell.
    const REMEMBERED_KEY = `fleethub:pty:${MACHINE}`;

    function remember(id) {
        try {
            if (id) sessionStorage.setItem(REMEMBERED_KEY, id);
            else sessionStorage.removeItem(REMEMBERED_KEY);
        } catch (e) { /* private mode: we fall back to "newest open session" */ }
    }

    function remembered() {
        try { return sessionStorage.getItem(REMEMBERED_KEY); } catch (e) { return null; }
    }

    /** Re-attach to this operator's existing shell on this machine, or start one. */
    async function connect() {
        if (connecting || session) return;
        connecting = true;
        setStatusPill(statusEl, 'warn', 'Connecting');
        buildTerminal();
        refit();

        try {
            const existing = await findExistingSession();
            if (existing) await attach(existing);
            else await openNew();
        } catch (e) {
            setStatusPill(statusEl, 'danger', 'Failed');
            note(`\r\nCould not open a terminal: ${e.message}\r\n`);
            if (reconnectBtn) reconnectBtn.hidden = false;
        } finally {
            connecting = false;
        }
    }

    async function findExistingSession() {
        let open;
        try {
            const body = await FleetApi.getJson(
                `/api/fleet/pty?machine=${encodeURIComponent(MACHINE)}`);
            open = body.sessions || [];
        } catch (e) {
            return null;   // fall through to opening a fresh one
        }
        if (!open.length) return null;
        const mine = remembered();
        // Prefer this tab's own; otherwise the newest, which is what a restored tab wants.
        return open.find((s) => s.session_id === mine) || open[0];
    }

    async function attach(existing) {
        session = { id: existing.session_id, cursor: -1, lastChunkAt: Date.now() };
        remember(session.id);
        if (shellEl && existing.shell) shellEl.value = existing.shell;
        setStatusPill(statusEl, 'ok', 'Connected');
        // cursor -1 asks the hub for the whole retained buffer, which the first poll writes
        // into the fresh terminal -- that replay IS the restored scrollback.
        schedulePoll(0);
    }

    async function openNew() {
        const body = await FleetApi.postJson('/api/fleet/pty', {
            machine: MACHINE,
            shell: shellEl ? shellEl.value : 'powershell',
            cols: term.cols,
            rows: term.rows,
        });
        session = { id: body.session_id, cursor: -1, lastChunkAt: Date.now() };
        remember(session.id);
        setStatusPill(statusEl, 'ok', 'Connected');
        note(`Opening a ${body.shell} console on ${MACHINE}…\r\n`);
        schedulePoll(OUTPUT_POLL_MS);
    }

    /** Local, hub-generated text. Kept visually distinct from agent output, which is
     *  written raw -- the operator should be able to tell the console apart from the box. */
    function note(text) {
        if (term) term.write(`\x1b[2m${text}\x1b[0m`);
    }

    /** Stop watching, but LEAVE THE SHELL RUNNING. This is what leaving the page does. */
    function detach() {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        session = null;
    }

    /** End the shell for good. Only ever from an explicit operator action, or because the
     *  hub told us the session is already over. */
    function disconnect(reason, { keepAlive = false } = {}) {
        const closing = session;
        detach();
        if (closing && !keepAlive) {
            // keepalive:true so the request still goes out if the page is unloading.
            fetch(`/api/fleet/pty/${encodeURIComponent(closing.id)}/close`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
                keepalive: true,
            }).catch(() => {});
            remember(null);
        }
        if (reason) {
            note(`\r\n[${reason}]\r\n`);
            setStatusPill(statusEl, 'muted', 'Disconnected');
        }
        if (reconnectBtn) reconnectBtn.hidden = false;
    }

    /** The operator explicitly wants a clean slate: end the old shell, start a new one. */
    async function newConsole() {
        disconnect('');
        remember(null);
        if (term) term.reset();
        if (reconnectBtn) reconnectBtn.hidden = true;
        // Skip the re-attach lookup -- we just closed the session it would find, and the
        // hub may not have processed that yet.
        connecting = true;
        try {
            buildTerminal();
            await openNew();
        } catch (e) {
            setStatusPill(statusEl, 'danger', 'Failed');
            note(`\r\nCould not open a terminal: ${e.message}\r\n`);
            if (reconnectBtn) reconnectBtn.hidden = false;
        } finally {
            connecting = false;
        }
    }

    // ---------------- Input ----------------
    function queueInput(data) {
        if (!data) return;
        if (!session) return;
        pending += data;
        if (inputTimer) return;
        inputTimer = setTimeout(flushInput, INPUT_FLUSH_MS);
    }

    async function flushInput() {
        inputTimer = null;
        // ORDER IS THE CONTRACT. Keystrokes must reach the shell in the order they were
        // typed, and a large paste now takes several POSTs -- so a second flush starting
        // while this one is mid-request would interleave two scripts into each other. One
        // drain runs at a time and picks up whatever queued while it was working.
        if (flushing) return;
        flushing = true;
        try {
            while (session && pending) {
                let rest = pending;
                pending = '';
                while (rest && session) {
                    let take = Math.min(MAX_INPUT_CHARS, rest.length);
                    // Never split a surrogate pair: half of one is not a character, and the
                    // two halves would arrive at the shell as two replacement characters.
                    const lead = rest.charCodeAt(take - 1);
                    if (take < rest.length && lead >= 0xd800 && lead <= 0xdbff) take -= 1;
                    const chunk = rest.slice(0, take);
                    rest = rest.slice(take);
                    await postInput({ data: chunk });
                }
            }
        } finally {
            flushing = false;
        }
        // Typing makes the shell produce output; poll for it now rather than waiting out
        // whatever backoff the previous quiet period had set.
        schedulePoll(OUTPUT_POLL_MS);
    }

    async function postInput(body) {
        if (!session) return;
        try {
            await FleetApi.postJson(
                `/api/fleet/pty/${encodeURIComponent(session.id)}/input`, body);
        } catch (e) {
            // A 409 means the session ended underneath us; anything else is transient.
            note(`\r\n[input not delivered: ${e.message}]\r\n`);
        }
    }

    // ---------------- Output ----------------
    async function poll() {
        if (!session) return;
        if (document.visibilityState !== 'visible') {
            schedulePoll(OUTPUT_POLL_HIDDEN_MS);
            return;
        }

        let body;
        try {
            body = await FleetApi.getJson(
                `/api/fleet/pty/${encodeURIComponent(session.id)}/output` +
                `?after_seq=${encodeURIComponent(session.cursor)}`);
        } catch (e) {
            disconnect(`lost contact with the hub: ${e.message}`);
            return;
        }
        if (!session) return;   // disconnected while the request was in flight

        if (body.lost) {
            // Our cursor fell off the back of the hub's rolling window, so there is a hole
            // in the stream -- and a hole in a VT stream is not "some missing text", it is a
            // half-eaten escape sequence that corrupts everything after it. Reset the
            // emulator and carry on from here.
            term.reset();
            note('[reconnected — some output was skipped]\r\n');
        }
        if (body.replay_truncated) {
            // A re-attach whose history is older than the buffer holds. Nothing is corrupt;
            // say so at the top so the operator doesn't read a mid-command screen as the
            // start of their session.
            note('[earlier output from this session is no longer buffered]\r\n');
        }
        if (body.chunks.length) {
            for (const chunk of body.chunks) term.write(chunk.text);
            session.cursor = body.next_seq - 1;
            session.lastChunkAt = Date.now();
        }

        if (body.status === 'closed') {
            // Already over on the hub's side, so there is nothing to close -- just stop
            // watching and let the operator start a new one.
            remember(null);
            disconnect(body.close_reason || 'the terminal closed', { keepAlive: true });
            return;
        }

        const quiet = Date.now() - session.lastChunkAt > QUIET_AFTER_MS;
        schedulePoll(quiet ? OUTPUT_POLL_IDLE_MS : OUTPUT_POLL_MS);
    }

    function schedulePoll(delay) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(poll, delay);
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
    // What we can do is not let it happen by surprise, which is what the confirmation below
    // is for -- the same warning Windows Terminal shows on a multi-line paste, for the same
    // reason: these lines are about to run as SYSTEM on somebody else's machine.

    /** Normalise CRLF/CR to \n so line counting and the CR rewrite below have one shape. */
    function normalizeEol(text) {
        return String(text == null ? '' : text).replace(/\r\n?/g, '\n');
    }

    /** Send text to the shell as keystrokes, one CR per line break. */
    function sendLines(text) {
        queueInput(text.replace(/\n/g, '\r'));
    }

    function confirmMultiline(lines, what, lastLineHeld) {
        return window.confirm(
            `${what} is ${lines} lines long.\n\n` +
            'It goes into the console as if you typed it, so every line but the last runs ' +
            'as it arrives — a console has no way to hold a block back.\n\n' +
            (lastLineHeld
                ? 'The last line is left at the prompt so you get a final look before Enter.'
                : 'If it ends with a line break, the last line runs too.') +
            '\n\nSend it?');
    }

    /** Ctrl-Shift-V. A paste keeps its trailing newline -- copying a whole line and pasting
     *  it is meant to run it -- so the confirmation says so. */
    function paste(text) {
        const body = normalizeEol(text);
        if (!body || !session) return;
        const lines = body.replace(/\n$/, '').split('\n').length;
        if (lines > 1 && !confirmMultiline(lines, 'What you are pasting', false)) return;
        sendLines(body);
    }

    // ---------------- Favorites ----------------
    // A favorite is TYPED INTO the terminal rather than run: it may have come from a
    // teammate and is about to run as SYSTEM, so the operator reads it and presses Enter.
    // Same rule as the old terminal, for the same reason.
    //
    // MULTI-LINE FAVORITES used to be flattened -- every newline became a space -- which
    // silently corrupted every favorite longer than one statement. `foreach ($x in $y) {`
    // and its body joined with spaces is a different script, and usually not a valid one, so
    // the operator got a syntax error from something that ran fine in the old terminal.
    // They now go in as lines, with the trailing newline held back so the last one still
    // waits at the prompt.
    function usePick(favorite) {
        if (favorite.command_type !== 'run_script') {
            note(`\r\n[${favorite.command_type} favorites are issued from the command ` +
                 `channel, not typed at a shell]\r\n`);
            return;
        }
        if (!session) {
            note('\r\n[no console is open on this machine — reconnect first]\r\n');
            return;
        }
        // Trailing whitespace off, so a script saved with a final newline doesn't submit its
        // own last line and take the review step with it.
        const script = normalizeEol(favorite.params && favorite.params.script).replace(/\s+$/, '');
        if (!script) {
            note(`\r\n[favorite "${favorite.name}" has no script in it]\r\n`);
            return;
        }

        const lines = script.split('\n').length;
        if (lines > 1 && !confirmMultiline(lines, `The favorite "${favorite.name}"`, true)) {
            note(`\r\n[favorite "${favorite.name}" not sent]\r\n`);
            return;
        }

        // Switching the dropdown would END this console (a running powershell cannot become
        // cmd), so a mismatch is reported rather than acted on -- losing the session an
        // operator is working in to "helpfully" match a saved field would be worse than a
        // cmd one-liner failing loudly in PowerShell.
        const savedFor = favorite.params && favorite.params.shell;
        if (savedFor && shellEl && savedFor !== shellEl.value) {
            note(`\r\n[this favorite was saved for ${savedFor}; this console is ` +
                 `${shellEl.value}]\r\n`);
        }

        sendLines(script);
        term.focus();
        note(lines > 1
            ? `\r\n[loaded favorite "${favorite.name}" (${lines} lines) — the last line is ` +
              'waiting for Enter]\r\n'
            : `\r\n[loaded favorite "${favorite.name}" — review it, then press Enter]\r\n`);
    }

    /** "Save as favorite" in a pty console. There is no input box to read a script out of
     *  here, so the terminal SELECTION is the seed -- select the command you just got right
     *  and save it -- and the dialog's textarea is where it gets edited, newlines and all. */
    function saveFavorite() {
        const selection = term ? term.getSelection() : '';
        FleetFavorites.openSave({
            type: 'run_script',
            params: {
                script: normalizeEol(selection).replace(/\s+$/, ''),
                shell: shellEl ? shellEl.value : 'powershell',
            },
        });
    }

    // ---------------- Public surface ----------------
    // fleet-terminal.js owns the one agent-version lookup and calls activate() when the
    // machine can do a pseudoconsole. Keeping the decision in one place stops the two
    // terminals from both deciding they are in charge.
    window.FleetPty = {
        activate() {
            if (paneEl) paneEl.hidden = false;
            if (legacyEl) legacyEl.hidden = true;
            for (const id of ['terminal-timeout', 'terminal-timeout-label',
                              'terminal-stop', 'terminal-reset']) {
                const el = document.getElementById(id);
                if (el) el.hidden = true;
            }
            if (reconnectBtn) reconnectBtn.hidden = true;
            if (newBtn) newBtn.hidden = false;

            // Clear, Favorites and Save-as-favorite live in the SHARED toolbar above both
            // terminals, and fleet-terminal.js has already bound its own handlers to them by
            // the time we get here (it binds synchronously; this runs after its async version
            // lookup). Leaving those in place would open two favorites dialogs on one click,
            // and its save handler reads the LEGACY textarea -- which is hidden and empty in
            // this mode, so it would refuse with "nothing to save".
            // Replacing the nodes drops every listener registered on them -- so this has to
            // happen BEFORE we bind ours, and we have to re-read the references afterwards.
            const shared = {};
            for (const id of ['terminal-clear', 'terminal-favorites', 'terminal-save-fav']) {
                const stale = document.getElementById(id);
                if (!stale) continue;
                const fresh = stale.cloneNode(true);
                stale.replaceWith(fresh);
                shared[id] = fresh;
            }

            if (hintEl) {
                hintEl.className = 'terminal__hint';
                hintEl.textContent =
                    'A real console on the machine, running as SYSTEM. Enter, Ctrl-C, Tab ' +
                    'completion and interactive prompts all work. Ctrl-Shift-C / Ctrl-Shift-V ' +
                    'copy and paste (Ctrl-C is passed through to the shell). The session ' +
                    'survives leaving this page — come back and your shell, working ' +
                    'directory and scrollback are still there. "New console" starts over.';
            }

            if (reconnectBtn) {
                reconnectBtn.addEventListener('click', () => {
                    reconnectBtn.hidden = true;
                    if (term) term.reset();
                    connect();
                });
            }
            if (shared['terminal-clear']) {
                // Clear the hub's replay buffer too, not just the local view -- otherwise
                // the scrollback you just cleared comes back the next time you navigate
                // away and return, which reads as the button not having worked.
                shared['terminal-clear'].addEventListener('click', () => {
                    if (term) term.clear();
                    if (session) {
                        FleetApi.postJson(
                            `/api/fleet/pty/${encodeURIComponent(session.id)}/clear`, {})
                            .catch(() => {});
                    }
                });
            }
            if (shared['terminal-favorites']) {
                shared['terminal-favorites'].addEventListener(
                    'click', () => FleetFavorites.open({ onPick: usePick }));
            }
            if (shared['terminal-save-fav']) {
                shared['terminal-save-fav'].title =
                    'Save a script as a favorite (any selected text is used as a starting point)';
                shared['terminal-save-fav'].addEventListener('click', saveFavorite);
            }
            if (newBtn) newBtn.addEventListener('click', newConsole);
            // Switching shell can only mean a new console: a running powershell cannot
            // become cmd.
            if (shellEl) shellEl.addEventListener('change', newConsole);

            // The panel starts hidden and xterm cannot measure a hidden element, so the
            // terminal is only built and connected once the tab is actually shown.
            if (panelEl) {
                panelEl.addEventListener('tab:shown', () => {
                    refit();
                    if (!session && !connecting) connect();
                    if (term) term.focus();
                });
                if (!panelEl.hidden) connect();
            }
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible' && session) schedulePoll(0);
            });
            // Leaving the page deliberately does NOT end the session -- that is the whole
            // point of persistence, and it is why there is no close-on-unload here. The
            // shell keeps running (and keeps printing into the hub's replay buffer) so that
            // coming back re-attaches to it. Abandonment is bounded on the hub instead, by a
            // clock that only the console's own polls refresh; see PTY_ABANDONED_SECONDS.
            window.addEventListener('pagehide', detach);
        },
    };
})();
