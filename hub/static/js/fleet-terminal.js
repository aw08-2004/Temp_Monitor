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
    if (!scrollbackEl || !window.FleetApi) return;

    // A call, not a constant: on the Tools page the machine changes without the page
    // reloading. Null until one is picked, which is why load() below is what starts things.
    const currentMachine = () => FleetApi.machine;
    const inputEl = document.getElementById('terminal-input');
    const runBtn = document.getElementById('terminal-run');
    const clearBtn = document.getElementById('terminal-clear');
    const favoritesBtn = document.getElementById('terminal-favorites');
    const saveFavBtn = document.getElementById('terminal-save-fav');
    const shellEl = document.getElementById('terminal-shell');
    const statusEl = document.getElementById('terminal-status');
    const hintEl = document.getElementById('terminal-hint');
    const PANEL_ID = 'tab-terminal';
    const panelEl = document.getElementById(PANEL_ID);
    // Which of the two terminals is in front, and therefore which one the shared toolbar
    // buttons are talking to. 'pty' from the moment refreshHint() hands off to fleet-pty.js.
    let owner = 'legacy';
    const stopBtn = document.getElementById('terminal-stop');
    const resetBtn = document.getElementById('terminal-reset');
    const timeoutEl = document.getElementById('terminal-timeout');
    const psEl = document.getElementById('terminal-ps');

    const historyKey = () => `tempmonitor:termhist:${currentMachine()}`;
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
    // From here up the agent can open a real pseudoconsole, and this whole file becomes a
    // fallback for the tail of a rollout — fleet-pty.js takes the tab instead. Everything
    // below this version is stuck sending scripts and reading text back, which is why a
    // `Read-Host` prompt never appeared and a bare Enter did nothing.
    const MIN_PTY_AGENT = '3.15.0';

    let history = [];
    let historyIndex = 0;   // one past the end == "typing a new command"
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
        const how = interactive ? t('machine.terminal.how_interactive')
                                : t('machine.terminal.how_oneshot');
        append(t('machine.terminal.connected', { machine: currentMachine(), how }) + '\n', 'meta');
    }

    // The prompt reflects the shell's real working directory once we've heard one back;
    // until then (and for a pre-3.2 agent that reports none) it falls back to the machine.
    function promptText() {
        const where = cwd || currentMachine();
        return shellEl.value === 'cmd' ? `${where}>` : `PS ${where}>`;
    }

    function updatePrompt() {
        if (psEl) psEl.textContent = promptText();
    }

    // ---------------- Command history ----------------
    function loadHistory() {
        try {
            const raw = localStorage.getItem(historyKey());
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
        try { localStorage.setItem(historyKey(), JSON.stringify(history)); } catch (e) { /* ignore */ }
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
        setStatusPill(statusEl, 'warn', t('machine.terminal.running'));

        const params = { script, shell };
        const timeout = readTimeout();
        if (timeout) params.timeout_seconds = timeout;
        try {
            const commandId = await FleetApi.issueCommand('run_script', params);
            active = { commandId, cursor: -1, startedAt: Date.now(), lastChunkAt: Date.now() };
            schedulePoll(POLL_FAST_MS);
        } catch (e) {
            append(`${e.message}\n`, 'err');
            setStatusPill(statusEl, 'danger', t('machine.terminal.failed'));
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
            append(t('machine.terminal.send_failed', { error: e.message }) + '\n', 'err');
        }
    }

    async function sendSignal() {
        if (!active) return;
        append('\n^C\n', 'meta');
        try { await FleetApi.issueCommand('shell_signal', { shell: shellEl.value }); }
        catch (e) {
            append(t('machine.terminal.stop_failed', { error: e.message }) + '\n', 'err');
        }
    }

    async function resetSession() {
        try {
            await FleetApi.issueCommand('shell_reset', { shell: shellEl.value });
            cwd = null;
            updatePrompt();
            append('\n' + t('machine.terminal.session_reset') + '\n', 'meta');
        } catch (e) {
            append(t('machine.terminal.reset_failed', { error: e.message }) + '\n', 'err');
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
            append(t('machine.terminal.lost_hub', { error: e.message }) + '\n', 'err');
            finish('danger', t('machine.terminal.error'));
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
                append('\n' + t('machine.terminal.truncated') + '\n', 'meta');
            }
            if (body.next_seq === 0 && body.result) {
                // Non-streaming agent: nothing was printed live, so print it all now.
                append(`${body.result.output || t('machine.terminal.no_output')}\n`);
            }
            if (body.status === 'expired') {
                append('\n' + t('machine.terminal.command_expired') + '\n', 'err');
                finish('muted', t('machine.terminal.expired'));
                return;
            }
            // Adopt the shell's real working directory for the prompt, if the agent reported one.
            if (body.result && body.result.cwd) {
                cwd = body.result.cwd;
                updatePrompt();
            }
            const ok = body.status === 'done';
            // Two whole sentences rather than one with 'completed'/'failed' spliced in:
            // the verb agrees with the rest of the line in most languages.
            const when = FleetApi.formatTime(
                body.result ? body.result.completed_at : Date.now() / 1000);
            append('\n' + (ok ? t('machine.terminal.completed_at', { when })
                               : t('machine.terminal.failed_at', { when })) + '\n',
                   ok ? 'meta' : 'err');
            finish(ok ? 'ok' : 'danger',
                   ok ? t('machine.terminal.done') : t('machine.terminal.failed'));
            return;
        }

        // The hub only expires PENDING commands, so one claimed by an agent that then
        // died stays "claimed" forever. Give up client-side rather than poll until the
        // tab closes.
        if (Date.now() - active.startedAt > GIVE_UP_MS) {
            append('\n' + t('machine.terminal.gave_up') + '\n', 'err');
            finish('muted', t('machine.terminal.unknown'));
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
        const base = t('machine.terminal.hint');
        hintEl.className = 'terminal__hint';
        hintEl.textContent = base;
        try {
            const info = await FleetApi.getJson(`/api/machines/${encodeURIComponent(currentMachine())}`);
            const version = info && info.companion_version;
            // A modern agent gets a REAL console (ConPTY + xterm.js) instead of any of this.
            // The decision lives here because this is the one place that looks the agent
            // version up -- two terminals both deciding they are in charge would be a mess.
            if (version && !versionLess(version, MIN_PTY_AGENT) && window.FleetPty) {
                owner = 'pty';
                FleetPty.activate();
                return;
            }
            if (version && versionLess(version, MIN_STREAMING_AGENT)) {
                // Pre-3.1: refuses run_script outright.
                setInteractive(false);
                hintEl.className = 'terminal__hint terminal__hint--warn';
                hintEl.textContent = t('machine.terminal.hint_old_streaming',
                    { version, required: MIN_STREAMING_AGENT, base });
            } else if (version && versionLess(version, MIN_INTERACTIVE_AGENT)) {
                // 3.1.x: streams, but each command is a fresh process (no cd persistence, no
                // stdin). Fall back to one-shot behavior and say so.
                setInteractive(false);
                hintEl.className = 'terminal__hint terminal__hint--warn';
                hintEl.textContent = t('machine.terminal.hint_old_interactive',
                    { version, required: MIN_INTERACTIVE_AGENT, base });
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
            append('\n' + t('machine.terminal.running_favorite',
                             { name: favorite.name, type: favorite.command_type })
                   + '\n', 'meta');
            FleetApi.issueCommand(favorite.command_type, favorite.params)
                .then(() => append(t('machine.terminal.favorite_queued') + '\n', 'meta'))
                .catch((e) => append(`${e.message}\n`, 'err'));
            return;
        }
        inputEl.value = favorite.params.script || '';
        if (favorite.params.shell) shellEl.value = favorite.params.shell;
        autoGrow();
        inputEl.focus();
        append('\n' + t('machine.terminal.loaded_favorite', { name: favorite.name })
               + '\n', 'meta');
    }

    function saveCurrent() {
        const script = inputEl.value.trim();
        if (!script) {
            append('\n' + t('machine.terminal.nothing_to_save') + '\n', 'meta');
            inputEl.focus();
            return;
        }
        FleetFavorites.openSave({
            type: 'run_script',
            params: { script, shell: shellEl.value }
        });
    }

    // ---------------- Init ----------------
    // Clear, Favorites and Save-as-favorite live in the SHARED toolbar above both terminals,
    // so exactly one of the two modules must answer a click on them. They are bound here,
    // once, and dispatch on who is in front -- rather than fleet-pty.js replacing the nodes
    // to steal them, which worked only for as long as a page could change hands once.
    clearBtn.addEventListener('click', () => {
        if (owner === 'pty') FleetPty.clearActive(); else clearScrollback();
    });
    favoritesBtn.addEventListener('click', () => {
        if (owner === 'pty') FleetPty.openFavorites();
        else FleetFavorites.open({ onPick: usePick });
    });
    saveFavBtn.addEventListener('click', () => {
        if (owner === 'pty') FleetPty.saveFavorite(); else saveCurrent();
    });

    runBtn.addEventListener('click', () => { if (interactive && active) sendInput(); else run(); });
    if (stopBtn) stopBtn.addEventListener('click', sendSignal);
    if (resetBtn) resetBtn.addEventListener('click', resetSession);
    if (shellEl) shellEl.addEventListener('change', updatePrompt);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && active) schedulePoll(0);
    });
    // The panel starts hidden, so focus only once it's actually shown (tabs.js fires this).
    // Only meaningful in legacy mode; the pseudoconsole focuses its own xterm.
    if (panelEl) {
        panelEl.addEventListener('tab:shown', () => { if (owner === 'legacy') inputEl.focus(); });
    }

    autoGrow();

    // Which terminal this machine gets is decided per machine, by its agent's version, so
    // it has to be decided again every time the machine changes -- and everything the last
    // one accumulated has to go first. See tool-panels.js for the sequencing.
    ToolPanels.register('terminal', {
        panelId: PANEL_ID,
        load,
        teardown,
        requires: (machine) => !!machine
    });

    function load() {
        // Back to the markup's defaults before the probe decides again: a legacy machine
        // picked after a pseudoconsole one would otherwise inherit a visible, empty strip.
        owner = 'legacy';
        if (panelEl) {
            const ptyPane = document.getElementById('terminal-pty');
            const legacyPane = document.getElementById('terminal-legacy');
            if (ptyPane) ptyPane.hidden = true;
            if (legacyPane) legacyPane.hidden = false;
        }
        history = loadHistory();
        historyIndex = history.length;
        draft = '';
        cwd = null;
        active = null;
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        setInteractive(true);
        updatePrompt();
        clearScrollback();
        refreshHint();
    }

    function teardown() {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        active = null;
        // Drops the local xterms without ending the sessions -- they live on the hub and
        // re-attach with their scrollback when this machine is selected again.
        if (window.FleetPty) FleetPty.deactivate();
        owner = 'legacy';
    }
})();
