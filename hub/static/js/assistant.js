// The console assistant (roadmap #26) -- the chat panel, in whichever place shell.js shows it.
//
// What this file must never do is put the model's words into the page as markup. Every
// answer is built from text nodes by renderMarkdown() below, which knows a handful of block
// shapes (paragraphs, lists, code) and exactly one kind of link: `[label](/relative/path)`,
// the shape assistant.render_links() emits for a link token the hub has already checked. A
// model that writes HTML gets its HTML shown as text; a model that writes a URL gets plain
// text, because the hub reduced it to its label before it got here.
//
// Action cards are built from the hub's STORED action row (method, path, body), not from
// anything the model said about it, so the card describes exactly what Confirm will run.
//
// Page context comes from the shell (window.parent.FleetShell.pageContext) and is sent with
// every message rather than stored, because "this PC" means whatever is on screen when the
// question is asked. In a classic, unframed page there is no shell and the context is this
// page itself, which is the honest answer.
//
// IIFE-wrapped, classic script, no bundler -- same as every other file in static/js.
(function () {
    'use strict';

    const root = document.getElementById('assistant');
    if (!root) return;

    const $ = (id) => document.getElementById(id);
    const log = $('assistant-log');
    const empty = $('assistant-empty');
    const form = $('assistant-form');
    const input = $('assistant-input');
    const sendBtn = $('assistant-send');
    const stopBtn = $('assistant-stop');
    const history = $('assistant-history');
    const pinBtn = $('assistant-pin');
    const contextEl = $('assistant-context');
    const offEl = $('assistant-off');

    const CHAT_KEY = 'tempmonitor:assistant:chat';
    const POLL_MS = 800;

    let chatId = null;
    let runId = null;
    let afterSeq = -1;
    let pollTimer = null;
    let thinking = null;

    // ---------------- The shell ----------------
    function shell() {
        try {
            return window.parent !== window && window.parent.FleetShell
                ? window.parent.FleetShell : null;
        } catch (e) {
            return null;
        }
    }

    function pageContext() {
        const s = shell();
        if (s) return s.pageContext();
        const url = new URL(location.href);
        return { path: url.pathname, query: url.search.slice(1), title: document.title };
    }

    function showContext() {
        const ctx = pageContext();
        if (!ctx || ctx.path === '/assistant') {
            contextEl.textContent = '';
            return;
        }
        contextEl.textContent = ctx.machine
            ? t('assistant.context_machine', { machine: ctx.machine })
            : t('assistant.context', { page: ctx.title || ctx.path });
    }

    function syncPin() {
        const s = shell();
        if (!s || !s.canPin()) {
            pinBtn.hidden = true;
            return;
        }
        const pinned = s.isPinned();
        pinBtn.hidden = false;
        pinBtn.textContent = pinned ? t('assistant.unpin') : t('assistant.pin');
        pinBtn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
    }

    pinBtn.addEventListener('click', () => {
        const s = shell();
        if (!s) return;
        if (s.isPinned()) s.unpin();
        else s.pin();
        syncPin();
        showContext();
    });

    window.addEventListener('fleet:pagechange', () => { showContext(); syncPin(); });
    window.addEventListener('resize', syncPin);

    // ---------------- Rendering ----------------
    /** Inline: `code`, **bold**, and [label](/path). Everything else is text. */
    function renderInline(parent, text) {
        const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[([^\]\n]+)\]\((\/[^)\s]*)\))/g;
        let last = 0;
        let match;
        while ((match = pattern.exec(text)) !== null) {
            if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
            if (match[1]) {
                parent.appendChild(el('code', null, match[1].slice(1, -1)));
            } else if (match[2]) {
                parent.appendChild(el('strong', null, match[2].slice(2, -2)));
            } else {
                const href = match[5];
                // Relative to this hub and nothing else. `//host` is protocol-relative and
                // would leave the hub, and browsers read `/\host` the same way, so both stay
                // text.
                if (/^\/(?![/\\])/.test(href)) {
                    const a = el('a', null, match[4]);
                    a.href = href;
                    parent.appendChild(a);
                } else {
                    parent.appendChild(document.createTextNode(match[4]));
                }
            }
            last = pattern.lastIndex;
        }
        if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }

    function renderMarkdown(text) {
        const out = document.createDocumentFragment();
        const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
        let i = 0;
        while (i < lines.length) {
            const line = lines[i];
            if (/^```/.test(line)) {
                const code = [];
                i += 1;
                while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
                i += 1;
                const pre = el('pre');
                pre.appendChild(el('code', null, code.join('\n')));
                out.appendChild(pre);
                continue;
            }
            const bullet = /^\s*[-*]\s+(.*)$/;
            const numbered = /^\s*\d+[.)]\s+(.*)$/;
            if (bullet.test(line) || numbered.test(line)) {
                const ordered = numbered.test(line) && !bullet.test(line);
                const list = el(ordered ? 'ol' : 'ul');
                const re = ordered ? numbered : bullet;
                while (i < lines.length && re.test(lines[i])) {
                    const li = el('li');
                    renderInline(li, lines[i].match(re)[1]);
                    list.appendChild(li);
                    i += 1;
                }
                out.appendChild(list);
                continue;
            }
            if (!line.trim()) { i += 1; continue; }
            const para = [];
            while (i < lines.length && lines[i].trim() && !/^```/.test(lines[i])
                   && !bullet.test(lines[i]) && !numbered.test(lines[i])) {
                para.push(lines[i].replace(/^#{1,6}\s+/, ''));
                i += 1;
            }
            const p = el('p');
            para.forEach((part, index) => {
                if (index) p.appendChild(el('br'));
                renderInline(p, part);
            });
            out.appendChild(p);
        }
        return out;
    }

    function scrollDown() {
        log.scrollTop = log.scrollHeight;
    }

    function addMessage(role, content, tools) {
        empty.hidden = true;
        const node = el('div', `assistant__msg assistant__msg--${role}`);
        if (role === 'assistant') node.appendChild(renderMarkdown(content));
        else node.textContent = content;
        if (tools && tools.length) {
            node.appendChild(el('div', 'assistant__tools-used',
                t('assistant.tools_used', { tools: tools.join(', ') })));
        }
        log.appendChild(node);
        scrollDown();
        return node;
    }

    function setThinking(text) {
        if (!text) {
            if (thinking) thinking.remove();
            thinking = null;
            return;
        }
        if (!thinking) {
            thinking = el('div', 'assistant__thinking');
            log.appendChild(thinking);
        }
        thinking.textContent = text;
        log.appendChild(thinking);   // keep it last
        scrollDown();
    }

    // ---------------- Action cards ----------------
    const STATE_KEYS = {
        pending: 'assistant.state.pending',
        running: 'assistant.state.running',
        done: 'assistant.state.done',
        failed: 'assistant.state.failed',
        rejected: 'assistant.state.rejected',
        expired: 'assistant.state.expired',
    };

    function actionDetail(action) {
        const lines = [`${action.method} ${action.path}`];
        if (action.query && Object.keys(action.query).length) lines.push(`?${new URLSearchParams(action.query)}`);
        if (action.body && Object.keys(action.body).length) lines.push(JSON.stringify(action.body, null, 2));
        return lines.join('\n');
    }

    function renderAction(action, existing) {
        empty.hidden = true;
        const card = existing || el('div');
        card.className = `assistant__action assistant__action--${action.state}`;
        card.dataset.actionId = action.id;
        card.replaceChildren();
        card.appendChild(el('div', 'assistant__action-title', action.machine
            ? t('assistant.action_title_machine', { machine: action.machine })
            : t('assistant.action_title')));
        card.appendChild(el('div', 'assistant__action-detail', actionDetail(action)));
        const state = el('div', 'stat-card__meta', t(STATE_KEYS[action.state] || 'assistant.state.pending'));
        if (action.result && action.result.status) {
            state.textContent += ` (HTTP ${action.result.status})`;
        }
        card.appendChild(state);

        if (action.state === 'pending') {
            const buttons = el('div', 'assistant__action-buttons');
            let typed = null;
            if (action.typed_name) {
                typed = el('input', 'input');
                typed.placeholder = t('assistant.typed_placeholder', { machine: action.machine });
                typed.setAttribute('aria-label', t('assistant.typed_placeholder', { machine: action.machine }));
                buttons.appendChild(typed);
            }
            const confirm = el('button', 'btn btn--danger', t('assistant.confirm'));
            confirm.type = 'button';
            const reject = el('button', 'btn btn--ghost', t('assistant.reject'));
            reject.type = 'button';
            confirm.addEventListener('click', () => decide(action, 'confirm', card, typed));
            reject.addEventListener('click', () => decide(action, 'reject', card, null));
            buttons.appendChild(confirm);
            buttons.appendChild(reject);
            card.appendChild(buttons);
        }
        if (!existing) {
            log.appendChild(card);
            scrollDown();
        }
        return card;
    }

    async function decide(action, verb, card, typed) {
        for (const button of card.querySelectorAll('button')) button.disabled = true;
        try {
            const response = await fetch(`/api/assistant/actions/${action.id}/${verb}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(typed ? { typed: typed.value } : {}),
            });
            const data = await response.json().catch(() => ({}));
            if (data.action) renderAction(data.action, card);
            if (!response.ok) {
                toast(data.error || t('assistant.error'), { kind: 'error' });
                if (!data.action) for (const button of card.querySelectorAll('button')) button.disabled = false;
            }
        } catch (e) {
            toast(t('assistant.error'), { kind: 'error' });
            for (const button of card.querySelectorAll('button')) button.disabled = false;
        }
    }

    // ---------------- Conversations ----------------
    function rememberChat(id) {
        try {
            if (id) localStorage.setItem(CHAT_KEY, id);
            else localStorage.removeItem(CHAT_KEY);
        } catch (e) { /* per-viewer convenience only */ }
    }

    function savedChat() {
        try { return localStorage.getItem(CHAT_KEY); } catch (e) { return null; }
    }

    async function loadHistory(selectId) {
        const response = await fetch('/api/assistant/chats');
        const data = await response.json().catch(() => ({ chats: [] }));
        history.replaceChildren();
        const fresh = el('option', null, t('assistant.new_chat'));
        fresh.value = '';
        history.appendChild(fresh);
        for (const chat of data.chats || []) {
            const option = el('option', null, chat.title || t('assistant.untitled'));
            option.value = chat.id;
            history.appendChild(option);
        }
        history.value = selectId && [...history.options].some((o) => o.value === selectId)
            ? selectId : '';
    }

    function clearLog() {
        for (const node of [...log.children]) if (node !== empty) node.remove();
        empty.hidden = false;
        thinking = null;
    }

    async function openChat(id) {
        stopPolling();
        clearLog();
        chatId = id || null;
        rememberChat(chatId);
        if (!chatId) return;
        const response = await fetch(`/api/assistant/chats/${encodeURIComponent(chatId)}`);
        if (!response.ok) {
            chatId = null;
            rememberChat(null);
            history.value = '';
            return;
        }
        const data = await response.json();
        const items = [
            ...data.messages.map((m) => ({ at: m.created_at, kind: 'message', m })),
            ...data.actions.map((a) => ({ at: a.created_at, kind: 'action', a })),
        ].sort((x, y) => x.at - y.at);
        for (const item of items) {
            if (item.kind === 'action') renderAction(item.a);
            else addMessage(item.m.role, item.m.content, item.m.tools);
        }
    }

    history.addEventListener('change', () => openChat(history.value));

    $('assistant-new').addEventListener('click', async () => {
        await openChat(null);
        history.value = '';
        input.focus();
    });

    $('assistant-delete').addEventListener('click', async () => {
        if (!chatId || !window.confirm(t('assistant.delete_confirm'))) return;
        await fetch(`/api/assistant/chats/${encodeURIComponent(chatId)}`, { method: 'DELETE' });
        await openChat(null);
        await loadHistory(null);
    });

    // ---------------- Sending and polling ----------------
    function busy(on) {
        sendBtn.disabled = on;
        stopBtn.hidden = !on;
        input.disabled = on;
    }

    function stopPolling() {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
        runId = null;
        busy(false);
        setThinking(null);
    }

    async function poll() {
        if (!runId) return;
        let data;
        try {
            const response = await fetch(`/api/assistant/runs/${runId}?after_seq=${afterSeq}`);
            data = await response.json();
            if (!response.ok) throw new Error(data.error || 'poll');
        } catch (e) {
            addMessage('error', t('assistant.error'));
            stopPolling();
            return;
        }
        for (const event of data.events || []) {
            afterSeq = Math.max(afterSeq, event.seq);
            if (event.type === 'text') {
                setThinking(null);
                addMessage('assistant', event.content);
                if (!event.final) setThinking(t('assistant.thinking'));
            } else if (event.type === 'tool') {
                if (event.action) renderAction(event.action);
                setThinking(event.state === 'started'
                    ? t('assistant.using_tool', { tool: event.name })
                    : t('assistant.thinking'));
            } else if (event.type === 'error') {
                addMessage('error', event.error === 'stopped' ? t('assistant.stopped') : event.error);
            }
        }
        if (data.done) {
            stopPolling();
            input.focus();
            return;
        }
        pollTimer = setTimeout(poll, POLL_MS);
    }

    async function send(text) {
        text = String(text || '').trim();
        if (!text || runId) return;
        busy(true);
        try {
            if (!chatId) {
                const created = await fetch('/api/assistant/chats', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
                });
                const chat = await created.json();
                if (!created.ok) throw new Error(chat.error || 'create');
                chatId = chat.id;
                rememberChat(chatId);
            }
            addMessage('user', text);
            input.value = '';
            const response = await fetch(`/api/assistant/chats/${encodeURIComponent(chatId)}/messages`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, context: pageContext() }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                addMessage('error', data.error || t('assistant.error'));
                busy(false);
                return;
            }
            runId = data.run_id;
            afterSeq = -1;
            setThinking(t('assistant.thinking'));
            pollTimer = setTimeout(poll, 300);
            loadHistory(chatId);
        } catch (e) {
            addMessage('error', t('assistant.error'));
            busy(false);
        }
    }

    form.addEventListener('submit', (e) => {
        e.preventDefault();
        send(input.value);
    });

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            send(input.value);
        }
    });

    stopBtn.addEventListener('click', () => {
        if (!runId) return;
        fetch(`/api/assistant/runs/${runId}/cancel`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        });
    });

    for (const button of document.querySelectorAll('.assistant__example')) {
        button.addEventListener('click', () => send(button.textContent));
    }

    // ---------------- Boot ----------------
    async function boot() {
        showContext();
        syncPin();
        try {
            const response = await fetch('/api/assistant/status');
            const status = await response.json();
            if (!status.ready) {
                offEl.hidden = false;
                offEl.textContent = t('assistant.off', { reason: status.error || '' });
                sendBtn.disabled = true;
                input.disabled = true;
            }
        } catch (e) { /* the first send will say what is wrong */ }
        const saved = savedChat();
        await loadHistory(saved);
        if (saved && history.value === saved) await openChat(saved);
    }

    boot();
})();
