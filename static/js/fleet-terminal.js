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

    const HISTORY_KEY = `tempmonitor:termhist:${MACHINE}`;
    const HISTORY_MAX = 100;
    const POLL_FAST_MS = 1000;
    const POLL_SLOW_MS = 2500;
    // After this long with no new output, ease off -- a 10-minute silent script shouldn't
    // cost 600 requests.
    const QUIET_BACKOFF_MS = 60_000;
    // The agent's own ceiling (RunScriptExecutor). Past this it kills the process, so a
    // command still "running" well beyond it means the agent died holding it.
    const SCRIPT_TIMEOUT_MS = 600_000;
    const GIVE_UP_MS = SCRIPT_TIMEOUT_MS + 120_000;
    // Agents older than this don't stream and refuse run_script outright.
    const MIN_STREAMING_AGENT = '3.1.0';

    let history = loadHistory();
    let historyIndex = history.length;   // one past the end == "typing a new command"
    let draft = '';
    let pollTimer = null;
    let active = null;   // { commandId, cursor, startedAt, lastChunkAt }

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
        append(`Connected to ${MACHINE}. Commands run as SYSTEM.\n`, 'meta');
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
        inputEl.disabled = busy;
        runBtn.disabled = busy;
        if (!busy) inputEl.focus();
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
            run();
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
    async function run() {
        const script = inputEl.value.trim();
        if (!script || active) return;

        const shell = shellEl.value;
        const prompt = shell === 'cmd' ? `${MACHINE}>` : `PS ${MACHINE}>`;
        append(`\n${prompt} ${script}\n`, 'echo');
        pushHistory(script);
        historyIndex = history.length;
        draft = '';
        inputEl.value = '';
        autoGrow();
        setBusy(true);
        setStatusPill(statusEl, 'warn', 'Running');

        try {
            const commandId = await FleetApi.issueCommand('run_script', { script, shell });
            active = { commandId, cursor: -1, startedAt: Date.now(), lastChunkAt: Date.now() };
            schedulePoll(POLL_FAST_MS);
        } catch (e) {
            append(`${e.message}\n`, 'err');
            setStatusPill(statusEl, 'danger', 'Failed');
            setBusy(false);
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
                     `Scripts run as SYSTEM and are killed after 10 minutes.`;
        hintEl.className = 'terminal__hint';
        hintEl.textContent = base;
        try {
            const info = await FleetApi.getJson(`/api/machines/${encodeURIComponent(MACHINE)}`);
            const version = info && info.companion_version;
            if (version && versionLess(version, MIN_STREAMING_AGENT)) {
                hintEl.className = 'terminal__hint terminal__hint--warn';
                hintEl.textContent =
                    `This machine reports agent v${version}. Live output and run_script need ` +
                    `v${MIN_STREAMING_AGENT} — it will refuse scripts until it self-updates. ` + base;
            }
        } catch (e) {
            /* hint only; the terminal works regardless */
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
    runBtn.addEventListener('click', run);
    favoritesBtn.addEventListener('click', () => FleetFavorites.open({ onPick: usePick }));
    saveFavBtn.addEventListener('click', saveCurrent);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && active) schedulePoll(0);
    });
    // The panel starts hidden, so focus only once it's actually shown (tabs.js fires this).
    if (panelEl) panelEl.addEventListener('tab:shown', () => inputEl.focus());

    clearScrollback();
    autoGrow();
    refreshHint();
})();
