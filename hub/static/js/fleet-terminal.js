// Remote terminal on the machine detail page.
//
// Type a script, hit Enter, watch it run as SYSTEM on the target. Issues run_script via
// FleetApi and polls /api/fleet/commands/<id>/output, appending chunks as the agent
// streams them.
//
// Two rules that are easy to break and expensive to get wrong:
//
//   1. Agent output is UNTRUSTED. Every line goes in via textContent / createTextNode,
//      never innerHTML. (setStatusPill in common.js does use innerHTML, but only ever on
//      trusted literals -- do not route agent text through it.)
//   2. A pre-3.1 agent doesn't stream, so it reports its whole output once at the end.
//      next_seq tells the two apart: 0 means "nothing was ever streamed" -> render
//      result.output as one block; >0 means we already printed it live -> print only a
//      completion line, or the operator sees everything twice.
//
// IIFE-wrapped: machine.js is a classic script sharing the global lexical scope.
(function () {
    'use strict';

    const scrollbackEl = document.getElementById('terminal-scrollback');
    if (!scrollbackEl || !window.FleetApi || !FleetApi.machine) return;

    const MACHINE = FleetApi.machine;
    const inputEl = document.getElementById('terminal-input');
    const runBtn = document.getElementById('terminal-run');
    const clearBtn = document.getElementById('terminal-clear');
    const favoritesBtn = document.getElementById('terminal-favorites');
    const saveFavBtn = document.getElementById('terminal-save-fav');
    const shellEl = document.getElementById('terminal-shell');
    const statusEl = document.getElementById('terminal-status');
    const hintEl = document.getElementById('terminal-hint');
    const panelEl = document.getElementById('tab-terminal');
    const stopBtn = document.getElementById('terminal-stop');
    const resetBtn = document.getElementById('terminal-reset');
    const timeoutEl = document.getElementById('terminal-timeout');
    const psEl = document.getElementById('terminal-ps');

    const HISTORY_KEY = `tempmonitor:termhist:${MACHINE}`;
    const HISTORY_MAX = 100;
    const POLL_FAST_MS = 1000;
    const POLL_SLOW_MS = 2500;
    // After this long with no new output, ease off -- a 10-minute silent script shouldn't
    // cost 600 requests.
    const QUIET_BACKOFF_MS = 60_000;
    // A submission may legitimately run a very long time (a persistent shell has no fixed
    // ceiling; the operator sets a per-run timeout). Give up watching only well past the
    // largest timeout we'd send, so a genuinely long run isn't abandoned.
    const GIVE_UP_MS = 24 * 60 * 60 * 1000 + 120_000;
    // Interactive terminal (persistent shell, stdin, cd persistence) needs a 3.2.0 agent.
    // Below it, run_script still works one-shot -- we fall back to that (no stdin, no cd
    // persistence) so the tab is still useful during a rollout, and warn why.
    const MIN_INTERACTIVE_AGENT = '3.2.0';
    // Older still: pre-3.1 agents don't stream at all.
    const MIN_STREAMING_AGENT = '3.1.0';

    let history = loadHistory();
    let historyIndex = history.length;   // one past the end == "typing a new command"
    let draft = '';
    let pollTimer = null;
    let active = null;   // { commandId, cursor, startedAt, lastChunkAt }
    let interactive = true;   // set false by refreshHint() against a pre-3.2 agent
    let cwd = null;           // last known shell cwd, for the prompt

    // ---------------- Scrollback ----------------
    function atBottom() {
        return scrollbackEl.scrollHeight - scrollbackEl.scrollTop - scrollbackEl.clientHeight < 40;
    }

    /** Append text. `kind` picks a colour class; omit for plain agent output. */
    function append(text, kind) {
        // Preserve the reader's position if they've scrolled up to read something --
        // yanking them to the bottom mid-read is worse than missing the newest line.
        const pinned = atBottom();
        const line = document.createElement('span');
        line.className = kind ? `terminal__line terminal__line--${kind}` : 'terminal__line';
        line.textContent = text;   // untrusted agent output
        scrollbackEl.appendChild(line);
        if (pinned) scrollbackEl.scrollTop = scrollbackEl.scrollHeight;
    }

    function clearScrollback() {
        scrollbackEl.textContent = '';
        const how = interactive
            ? 'One persistent shell per operator; cd and variables persist. Commands run as SYSTEM.'
            : 'Commands run as SYSTEM.';
        append(`Connected to ${MACHINE}. ${how}\n`, 'meta');
    }

    // The prompt reflects the shell's real working directory once we've heard one back;
    // until then (and for a pre-3.2 agent that reports none) it falls back to the machine.
    function promptText() {
        const where = cwd || MACHINE;
        return shellEl.value === 'cmd' ? `${where}>` : `PS ${where}>`;
    }

    function updatePrompt() {
        if (psEl) psEl.textContent = promptText();
    }

    // ---------------- Command history ----------------
    function loadHistory() {
        try {
            const raw = localStorage.getItem(HISTORY_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string') : [];
        } catch (e) {
            return [];   // private mode, quota, or corrupt JSON -- history is a nicety
        }
    }

    function pushHistory(script) {
        if (history[history.length - 1] === script) return;   // don't stack repeats
        history.push(script);
        if (history.length > HISTORY_MAX) history = history.slice(-HISTORY_MAX);
        try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch (e) { /* ignore */ }
    }

    function recallHistory(delta) {
        if (!history.length) return;
        if (historyIndex === history.length) draft = inputEl.value;
        historyIndex = Math.min(history.length, Math.max(0, historyIndex + delta));
        inputEl.value = historyIndex === history.length ? draft : history[historyIndex];
        autoGrow();
        // Caret to the end, so ↑ then typing appends rather than inserting mid-line.
        requestAnimationFrame(() => inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length));
    }

    // ---------------- Prompt ----------------
    function autoGrow() {
        inputEl.style.height = 'auto';
        inputEl.style.height = `${Math.min(inputEl.scrollHeight, 240)}px`;
    }

    function setBusy(busy) {
        // In interactive mode the input stays live while a submission runs -- that's how the
        // operator answers a prompt (types stdin). Only a pre-3.2 (one-shot) agent disables it.
        inputEl.disabled = busy && !interactive;
        runBtn.disabled = busy && !interactive;
        if (stopBtn) stopBtn.disabled = !busy;
        if (!busy || interactive) inputEl.focus();
    }

    function caretOnFirstLine() {
        return inputEl.value.slice(0, inputEl.selectionStart).indexOf('\n') === -1;
    }

    function caretOnLastLine() {
        return inputEl.value.slice(inputEl.selectionEnd).indexOf('\n') === -1;
    }

    inputEl.addEventListener('input', autoGrow);

    inputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            // With a submission already running (interactive agent), Enter pipes the line to
            // its stdin -- answering a prompt -- rather than starting a new command.
            if (interactive && active) sendInput();
            else run();
            return;
        }
        if (e.key === 'l' && e.ctrlKey) {
            e.preventDefault();
            clearScrollback();
            return;
        }
        // Only hijack the arrows at the edges of the text, so they still navigate a
        // multi-line script normally.
        if (e.key === 'ArrowUp' && caretOnFirstLine()) {
            e.preventDefault();
            recallHistory(-1);
        } else if (e.key === 'ArrowDown' && caretOnLastLine()) {
            e.preventDefault();
            recallHistory(1);
        } else if (e.key === 'Escape') {
            historyIndex = history.length;
            inputEl.value = draft;
            autoGrow();
        }
    });

    // ---------------- Running ----------------
    function readTimeout() {
        const n = Number(timeoutEl && timeoutEl.value);
        return Number.isFinite(n) && n > 0 ? Math.floor(n) : undefined;
    }

    async function run() {
        const script = inputEl.value.trim();
        if (!script || active) return;

        const shell = shellEl.value;
        append(`\n${promptText()} ${script}\n`, 'echo');
        pushHistory(script);
        historyIndex = history.length;
        draft = '';
        inputEl.value = '';
        autoGrow();
        setBusy(true);
        setStatusPill(statusEl, 'warn', 'Running');

        const params = { script, shell };
        const timeout = readTimeout();
        if (timeout) params.timeout_seconds = timeout;
        try {
            const commandId = await FleetApi.issueCommand('run_script', params);
            active = { commandId, cursor: -1, startedAt: Date.now(), lastChunkAt: Date.now() };
            schedulePoll(POLL_FAST_MS);
        } catch (e) {
            append(`${e.message}\n`, 'err');
            setStatusPill(statusEl, 'danger', 'Failed');
            setBusy(false);
        }
    }

    // Pipe the current line to the running submission's stdin (answering a prompt). The
    // program's response streams back on the run_script command we're already polling.
    async function sendInput() {
        const data = inputEl.value;
        append(`${data}\n`, 'input');   // local echo -- redirected stdin isn't echoed by the shell
        inputEl.value = '';
        autoGrow();
        try {
            await FleetApi.issueCommand('shell_input', { data, shell: shellEl.value });
        } catch (e) {
            append(`[could not send input: ${e.message}]\n`, 'err');
        }
    }

    async function sendSignal() {
        if (!active) return;
        append('\n^C\n', 'meta');
        try { await FleetApi.issueCommand('shell_signal', { shell: shellEl.value }); }
        catch (e) { append(`[stop failed: ${e.message}]\n`, 'err'); }
    }

    async function resetSession() {
        try {
            await FleetApi.issueCommand('shell_reset', { shell: shellEl.value });
            cwd = null;
            updatePrompt();
            append('\n[session reset — a fresh shell will start on your next command]\n', 'meta');
        } catch (e) {
            append(`[reset failed: ${e.message}]\n`, 'err');
        }
    }

    function finish(state, label) {
        active = null;
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        setStatusPill(statusEl, state, label);
        setBusy(false);
    }

    async function poll() {
        if (!active) return;
        // Don't poll a background tab; visibilitychange kicks us on return.
        if (document.visibilityState !== 'visible') {
            schedulePoll(POLL_SLOW_MS);
            return;
        }

        let body;
        try {
            body = await FleetApi.fetchOutput(active.commandId, active.cursor);
        } catch (e) {
            append(`Lost contact with the hub: ${e.message}\n`, 'err');
            finish('danger', 'Error');
            return;
        }
        if (!active) return;   // cleared while the request was in flight

        if (body.chunks.length) {
            for (const chunk of body.chunks) append(chunk.text);
            active.cursor = body.next_seq - 1;
            active.lastChunkAt = Date.now();
        }

        const terminal = body.status === 'done' || body.status === 'failed' || body.status === 'expired';
        // Wait for an empty batch before stopping, so a result that beat its final chunk
        // through can't cut off the tail.
        if (terminal && !body.chunks.length) {
            if (body.truncated) {
                append('\n(output truncated — the command produced more than the hub stores)\n', 'meta');
            }
            if (body.next_seq === 0 && body.result) {
                // Non-streaming agent: nothing was printed live, so print it all now.
                append(`${body.result.output || '(no output)'}\n`);
            }
            if (body.status === 'expired') {
                append('\nCommand expired — the agent never picked it up.\n', 'err');
                finish('muted', 'Expired');
                return;
            }
            // Adopt the shell's real working directory for the prompt, if the agent reported one.
            if (body.result && body.result.cwd) {
                cwd = body.result.cwd;
                updatePrompt();
            }
            const ok = body.status === 'done';
            append(`\n[${ok ? 'completed' : 'failed'} at ${FleetApi.formatTime(
                body.result ? body.result.completed_at : Date.now() / 1000)}]\n`, ok ? 'meta' : 'err');
            finish(ok ? 'ok' : 'danger', ok ? 'Done' : 'Failed');
            return;
        }

        // The hub only expires PENDING commands, so one claimed by an agent that then
        // died stays "claimed" forever. Give up client-side rather than poll until the
        // tab closes.
        if (Date.now() - active.startedAt > GIVE_UP_MS) {
            append('\nNo response from the agent — giving up watching. The command may still ' +
                   'be running on the machine.\n', 'err');
            finish('muted', 'Unknown');
            return;
        }

        const quiet = Date.now() - active.lastChunkAt > QUIET_BACKOFF_MS;
        schedulePoll(quiet ? POLL_SLOW_MS : POLL_FAST_MS);
    }

    function schedulePoll(delay) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(poll, delay);
    }

    // ---------------- Agent capability hint ----------------
    // A machine still on 3.0.x refuses run_script (its own empty-key signature gate) and
    // can't stream. That's not a regression -- it refused before this change too -- but
    // failing with "signature verification failed" right after being told signing is gone
    // is baffling. Say so up front instead.
    function versionLess(a, b) {
        const pa = String(a).split('.').map(Number);
        const pb = String(b).split('.').map(Number);
        for (let i = 0; i < 3; i++) {
            const x = pa[i] || 0, y = pb[i] || 0;
            if (x !== y) return x < y;
        }
        return false;
    }

    async function refreshHint() {
        const base = `Enter runs · Shift+Enter for a new line · ↑/↓ history · Ctrl+L clears. ` +
                     `Scripts run as SYSTEM. cd and variables persist; set a per-run timeout above.`;
        hintEl.className = 'terminal__hint';
        hintEl.textContent = base;
        try {
            const info = await FleetApi.getJson(`/api/machines/${encodeURIComponent(MACHINE)}`);
            const version = info && info.companion_version;
            if (version && versionLess(version, MIN_STREAMING_AGENT)) {
                // Pre-3.1: refuses run_script outright.
                setInteractive(false);
                hintEl.className = 'terminal__hint terminal__hint--warn';
                hintEl.textContent =
                    `This machine reports agent v${version}. Live output and run_script need ` +
                    `v${MIN_STREAMING_AGENT} — it will refuse scripts until it self-updates. ` + base;
            } else if (version && versionLess(version, MIN_INTERACTIVE_AGENT)) {
                // 3.1.x: streams, but each command is a fresh process (no cd persistence, no
                // stdin). Fall back to one-shot behavior and say so.
                setInteractive(false);
                hintEl.className = 'terminal__hint terminal__hint--warn';
                hintEl.textContent =
                    `This machine reports agent v${version}. A persistent shell (cd persistence, ` +
                    `answering prompts) needs v${MIN_INTERACTIVE_AGENT}; until it self-updates, each ` +
                    `command runs in a fresh process. ` + base;
            } else {
                setInteractive(true);
            }
        } catch (e) {
            /* hint only; the terminal works regardless */
        }
    }

    // Toggle the interactive affordances (stdin, Stop, Reset, live input during a run) to
    // match the agent's capability. A one-shot agent hides them and disables input mid-run.
    function setInteractive(on) {
        interactive = on;
        for (const el of [stopBtn, resetBtn, timeoutEl]) {
            if (el) el.hidden = !on;
        }
        if (timeoutEl && timeoutEl.previousElementSibling) {
            timeoutEl.previousElementSibling.hidden = !on;   // its label
        }
    }

    // ---------------- Favorites ----------------
    // Picking a favorite loads it into the prompt rather than firing it immediately: it
    // may have come from a teammate and is about to run as SYSTEM, so the operator gets
    // to read it first. Non-run_script favorites can't be typed at a shell, so those are
    // issued directly.
    function usePick(favorite) {
        if (favorite.command_type !== 'run_script') {
            append(`\n[running favorite "${favorite.name}" (${favorite.command_type})]\n`, 'meta');
            FleetApi.issueCommand(favorite.command_type, favorite.params)
                .then(() => append('Queued — the agent picks it up within ~10s. This kind of ' +
                                   'command reports no output here.\n', 'meta'))
                .catch((e) => append(`${e.message}\n`, 'err'));
            return;
        }
        inputEl.value = favorite.params.script || '';
        if (favorite.params.shell) shellEl.value = favorite.params.shell;
        autoGrow();
        inputEl.focus();
        append(`\n[loaded favorite "${favorite.name}" — review it, then press Enter]\n`, 'meta');
    }

    function saveCurrent() {
        const script = inputEl.value.trim();
        if (!script) {
            append('\nNothing to save — type a script first.\n', 'meta');
            inputEl.focus();
            return;
        }
        FleetFavorites.openSave({
            type: 'run_script',
            params: { script, shell: shellEl.value }
        });
    }

    // ---------------- Init ----------------
    clearBtn.addEventListener('click', clearScrollback);
    runBtn.addEventListener('click', () => { if (interactive && active) sendInput(); else run(); });
    favoritesBtn.addEventListener('click', () => FleetFavorites.open({ onPick: usePick }));
    saveFavBtn.addEventListener('click', saveCurrent);
    if (stopBtn) stopBtn.addEventListener('click', sendSignal);
    if (resetBtn) resetBtn.addEventListener('click', resetSession);
    if (shellEl) shellEl.addEventListener('change', updatePrompt);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && active) schedulePoll(0);
    });
    // The panel starts hidden, so focus only once it's actually shown (tabs.js fires this).
    if (panelEl) panelEl.addEventListener('tab:shown', () => inputEl.focus());

    updatePrompt();
    clearScrollback();
    autoGrow();
    refreshHint();
})();
